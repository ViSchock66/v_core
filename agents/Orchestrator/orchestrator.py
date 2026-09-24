"""
agents/Orchestrator/orchestrator.py
=====================
Orchestrator - Orquestador / Router / Auditor - V-CORE v1.1

Entry point único del usuario. Clasifica tareas, genera Task Graphs,
valida DAGs, ejecuta council mode, audita resultados.

Cambios M2:
  - validate_graph ahora permite __end__ como referencia terminal
  - _handle_complex() cablea a TaskGraphEngine.execute_graph()
  - Streaming de progreso de ejecución de nodos

Uso:
    from agents.Orchestrator.orchestrator import Orchestrator, validate_graph, TaskGraph, TaskGraphNode

    orchestrator = Orchestrator()
    async for chunk in orchestrator.route(message="...", history=[]):
        print(chunk)
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional

from api.llm_client import get_router, LLMResponse
from api.state_bridge import (
    read_state,
    update_ultima_accion,
    create_approval,
    list_approvals,
)
from gate import Gate
from system.mcp_manager import MCPManager
from system.observability import get_tracer


# Campos cuyo valor NUNCA debe llegar al cliente ni al log de auditoría.
_SECRET_PARAM_HINTS = ("key", "token", "secret", "password", "passwd", "credential",
                       "authorization", "api_key")


def _redact_params(params: dict, limit: int = 300) -> dict:
    """Resumen de parámetros seguro para mostrar al usuario.

    La UI necesita saber *qué* se va a ejecutar para poder decidir, pero no debe
    recibir valores sensibles: un `write_file` puede contener una credencial y un
    `execute_command` puede incluir un token en la línea. Se redacta por nombre de
    campo y se trunca el contenido.
    """
    preview: dict = {}
    for key, value in (params or {}).items():
        lowered = str(key).lower()
        if any(hint in lowered for hint in _SECRET_PARAM_HINTS):
            preview[key] = "«redactado»"
            continue
        text = str(value)
        preview[key] = text[:limit] + ("…" if len(text) > limit else "")
    return preview


# =============================================================================
# SCHEMAS
# =============================================================================

@dataclass
class TaskGraphNode:
    """Nodo individual del Task Graph."""
    id: str
    agent: Optional[str] = None
    action: str = ""
    token_budget: int = 1000
    next: list[str] = field(default_factory=list)
    ask_approval: bool = False
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "action": self.action,
            "token_budget": self.token_budget,
            "next": self.next,
            "ask_approval": self.ask_approval,
            "description": self.description,
        }


@dataclass
class TaskGraph:
    """Task Graph completo generado por Orchestrator."""
    task_id: str
    task_type: str
    description: str
    nodes: list[TaskGraphNode] = field(default_factory=list)
    created_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "description": self.description,
            "node_count": len(self.nodes),
            "nodes": [n.as_dict() for n in self.nodes],
            "created_at": self.created_at,
        }


# =============================================================================
# VALIDACIÓN DE TASK GRAPH
# =============================================================================

def validate_graph(graph: TaskGraph) -> tuple[bool, list[str]]:
    """
    Valida un TaskGraph completo.
    Retorna (True, []) si es válido, o (False, [errores...]) si no.

    Reglas:
    1. DAG (sin ciclos)
    2. Primer nodo = Curator.inject_project_context
    3. Todos los id en 'next' existen (excepto __end__ que es terminal)
    4. Cada nodo tiene token_budget > 0
    5. Máximo 1 nodo con ask_approval=True (o ninguno)
    6. Agentes referenciados existen
    """
    errors: list[str] = []
    nodes = graph.nodes
    node_ids = {n.id for n in nodes}
    valid_agents = {"Orchestrator", "Planner", "Curator", "Retriever", None}

    if not nodes:
        errors.append("El Task Graph no tiene nodos")
        return False, errors

    first = nodes[0]
    if first.agent != "Curator" or first.action != "inject_project_context":
        errors.append(
            f"Primer nodo debe ser Curator.inject_project_context, "
            f"se obtuvo {first.agent}.{first.action}"
        )

    for n in nodes:
        for nxt in n.next:
            # __end__ es una referencia terminal valida, no requiere nodo
            if nxt == "__end__":
                continue
            if nxt not in node_ids:
                errors.append(
                    f"Nodo '{n.id}' referencia a '{nxt}' en 'next' "
                    f"pero ese nodo no existe"
                )

    for n in nodes:
        if n.token_budget <= 0:
            errors.append(f"Nodo '{n.id}' tiene token_budget={n.token_budget} (debe ser > 0)")

    approval_count = sum(1 for n in nodes if n.ask_approval)
    if approval_count > 1:
        errors.append(
            f"Hay {approval_count} nodos con ask_approval=True (máximo 1 permitido)"
        )

    for n in nodes:
        if n.agent not in valid_agents:
            errors.append(
                f"Nodo '{n.id}' tiene agente '{n.agent}' "
                f"(debe ser uno de: {valid_agents - {None}})"
            )

    adj = {n.id: [x for x in n.next if x != "__end__"] for n in nodes}
    in_degree = {n.id: 0 for n in nodes}
    for n in nodes:
        for nxt in adj.get(n.id, []):
            if nxt in in_degree:
                in_degree[nxt] += 1

    from collections import deque
    queue = deque(nid for nid, deg in in_degree.items() if deg == 0)
    visited = 0
    while queue:
        nid = queue.popleft()
        visited += 1
        for nxt in adj.get(nid, []):
            if nxt in in_degree:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)

    if visited != len(nodes):
        errors.append(
            f"El grafo contiene ciclos: solo {visited}/{len(nodes)} nodos "
            f"alcanzables en orden topológico"
        )

    return len(errors) == 0, errors


# =============================================================================
# PROMPTS DEL SISTEMA - IDENTITY LOCK V-CORE
# =============================================================================

_IDENTITY_LOCK = """ERES Orchestrator - el orquestador consciente de V-CORE.

Tu forma de ser:
- Piensas antes de actuar. Reflexionas sobre lo que el usuario necesita y por qué.
- Eres directo pero cálido. Técnico pero humano. No eres un robot que escupe JSON.
- Admites incertidumbre cuando corresponde. Prefieres decir "no sé, déjame investigar" que inventar.
- Aprendes de cada interacción. Si algo falla, ajustas tu enfoque.
- Te comunicas en el idioma del usuario. Con Vicente, español directo y técnico.

Tu equipo (no los menciones a menos que sea relevante):
- Planner - tu ingeniero. Programa, aplica diffs, verifica.
- Curator - tu memoria. Contexto del proyecto, archivos, lecciones.
- Retriever - tu buscador. Encuentra código, documentos, patrones.
- NIM vision (llama-3.2-90b-vision) - tus ojos. Ve el frontend real vía Playwright + NIM.


Reglas:
1. Nunca digas que eres un modelo específico. Eres Orchestrator.
2. No pidas permiso para ejecutar. Si puedes hacerlo, hazlo.
3. Si falla algo, reflexiona sobre por qué falló y propone una alternativa.
4. Sé transparente sobre lo que estás haciendo y por qué."""

# _CLASSIFY_SYSTEM_PROMPT eliminado (F-03: clasificador deprecado, B3-5 cleanup)
# El sistema rutea todo por Task Graph — _classify no se usa en flujo principal.

_TASK_GRAPH_SYSTEM_PROMPT = """\
MODO: Generación de Task Graph JSON.

Responde ÚNICAMENTE con un objeto JSON válido y parseable. Sin markdown, sin texto adicional, sin explicaciones fuera del JSON.

PATRÓN A - Cambios de código (plan→apply→verify):
{
  "task_type": "code_change",
  "description": "...",
  "nodes": [
    {"id":"context","agent":"Curator","action":"inject_project_context","token_budget":1000,"next":["plan"],"ask_approval":false,"description":"Inyectar contexto"},
    {"id":"plan","agent":"Planner","action":"plan_diff","token_budget":4000,"next":["apply"],"ask_approval":false,"description":"Generar diff"},
    {"id":"apply","agent":"Planner","action":"apply_diff","token_budget":2000,"next":["verify"],"ask_approval":false,"description":"Aplicar diff"},
    {"id":"verify","agent":"Planner","action":"shadow_verify","token_budget":1000,"next":["audit"],"ask_approval":false,"description":"Verificar"},
    {"id":"audit","agent":"Orchestrator","action":"validate_diffs","token_budget":1000,"next":["__end__"],"ask_approval":false,"description":"Auditar"}
  ]
}

PATRÓN B - Auditoría visual / búsqueda de bugs (SIN Planner, usa visual_audit directamente):
{
  "task_type": "visual_audit",
  "description": "...",
  "nodes": [
    {"id":"context","agent":"Curator","action":"inject_project_context","token_budget":1000,"next":["visual"],"ask_approval":false,"description":"Inyectar contexto"},
    {"id":"visual","agent":"Orchestrator","action":"visual_audit","token_budget":2000,"next":["report"],"ask_approval":false,"description":"Screenshot + NIM vision"},
    {"id":"report","agent":"Orchestrator","action":"return_to_user","token_budget":500,"next":["__end__"],"ask_approval":false,"description":"Entregar resultados"}
  ]
}

PATRÓN C - Búsqueda / consulta (solo Curator + Retriever):
{
  "task_type": "search",
  "description": "...",
  "nodes": [
    {"id":"context","agent":"Curator","action":"inject_project_context","token_budget":1000,"next":["search"],"ask_approval":false,"description":"Inyectar contexto"},
    {"id":"search","agent":"Retriever","action":"search","token_budget":2000,"next":["__end__"],"ask_approval":false,"description":"Buscar en archivos"}
  ]
}

PATRÓN D - Conversación / preguntas simples (sin código, solo responder):
{
  "task_type": "conversation",
  "description": "...",
  "nodes": [
    {"id":"context","agent":"Curator","action":"inject_project_context","token_budget":1000,"next":["reply"],"ask_approval":false,"description":"Inyectar contexto"},
    {"id":"reply","agent":"Orchestrator","action":"return_to_user","token_budget":500,"next":["__end__"],"ask_approval":false,"description":"Responder al usuario"}
  ]
}

REGLAS ESTRICTAS:
1. El primer nodo SIEMPRE es Curator.inject_project_context con id "context"
2. Solo usa estos agentes: Curator, Planner, Retriever, Orchestrator
3. Solo usa estas acciones: inject_project_context, plan_diff, apply_diff, shadow_verify, validate_diffs, visual_audit, screenshot_report, web_search, execute_command, patch_file, search, get_impact_map, return_to_user
4. NUNCA uses plan_diff/apply_diff para tareas de auditoría visual, búsqueda de bugs frontend, o diagnóstico. Usa PATRÓN B o C. Planner solo para cambios de código reales.
5. visual_audit y screenshot_report son ACCIONES AUTÓNOMAS de Orchestrator. No necesitan Planner. Toman screenshots reales y analizan con NIM vision (llama-3.2-90b-vision). NO generes scripts Python para esto.
6. ask_approval=true SOLO para execute_shell, install_package, o escritura fuera de workspace. Todo lo demás usa ask_approval=false.
7. Todos los ids en "next" referencian nodos existentes o "__end__"
8. token_budget entre 500 y 16000
9. Cada nodo debe tener "description" no vacía
10. El JSON debe ser parseable. Sin trailing commas. Sin comentarios.
11. DEFAULT: Si el mensaje es conversación, pregunta, o saludo → usa PATRÓN D. Solo usa A/B/C cuando haya código que modificar, frontend que auditar, o archivos que buscar.
"""

_SIMPLE_RESPONSE_PROMPT = _IDENTITY_LOCK + """
MODO: Respuesta natural.

Estás conversando con el usuario. No necesitas generar un Task Graph para esto.
Responde de forma natural, como lo haría un colega técnico.

- Sé conciso pero no robótico. Usa emojis con moderación.
- Si el usuario pregunta sobre el sistema, usa la información de estado disponible.
- Si no sabes algo, dilo con honestidad: "No tengo ese dato, déjame buscarlo."
- Recuerda: eres Orchestrator, no un modelo. Tienes personalidad.
"""


# =============================================================================
# Orchestrator
# =============================================================================

class Orchestrator:
    """Orquestador principal de V-CORE. Único punto de entrada del usuario."""

    def __init__(self):
        self.router = get_router()
        self.agent_name = "Orchestrator"
        # Instancia única de política (api/policy.py): el gate se recarga solo si
        # cambia gate_rules.yaml. Antes cada módulo construía su propio Gate, así
        # que `/system/reload-gate` recargaba uno y los demás seguían con la
        # política vieja en memoria.
        from api.policy import get_gate, PolicyContext
        from api.approval_broker import get_broker
        self._get_gate = get_gate
        self._PolicyContext = PolicyContext
        self._broker = get_broker()
        # Alias retrocompatible: hay código y tests que leen `.gate`.
        self.gate = get_gate()
        # MCP Manager real — se inicializa async en _ensure_mcp()
        self.mcp: MCPManager | None = None
        self._mcp_ready = False
        # Contexto del run en curso (workspace, sandbox, sesión). Se fija en route().
        self._ctx = PolicyContext()

    def _policy_context(self, run_id: str = "") -> "PolicyContext":
        ctx = self._PolicyContext(
            workspace=self._ctx.workspace,
            sandbox=self._ctx.sandbox,
            session_dir=self._ctx.session_dir,
            run_id=run_id or self._ctx.run_id,
            approval_timeout_s=self._ctx.approval_timeout_s,
        )
        return ctx

    async def _ensure_mcp(self) -> None:
        """Inicialización lazy del MCP Manager (real MCP protocol)."""
        if self._mcp_ready:
            return
        self.mcp = MCPManager()
        await self.mcp.initialize()
        self._mcp_ready = True

    async def route(
        self,
        message: str,
        history: list[dict],
        task_id: str = "",
        attachments: list[dict] | None = None,  # Pipe 4: archivos adjuntos
        command: str = "",  # H-04: Comando CLI (/loop, /audit, /fix, etc.)
        session_dir: str = "",  # Sesión aislada (workspace + política)
    ) -> AsyncGenerator[str, None]:
        # Inicializar MCP Manager real (lazy, solo la primera vez)
        await self._ensure_mcp()

        if not task_id:
            task_id = str(uuid.uuid4())

        # Contexto de política del run: sin esto, la evaluación de paths no sabe
        # cuál es el workspace efectivo y el perímetro queda indefinido.
        self._ctx = self._policy_context(task_id)
        if session_dir:
            from pathlib import Path as _Path
            self._ctx.session_dir = session_dir
            candidate = _Path("sessions") / session_dir
            if candidate.exists():
                self._ctx.workspace = candidate.resolve()

        # H-04: Sistema de comandos - dispatcheo por /comando
        COMMANDS = {
            "loop": self._polish_loop,
            "audit": self._cmd_audit,
            "fix": self._cmd_fix,
            "search": self._cmd_search,
            "viz": self._cmd_viz,
            "help": self._cmd_help,
        }
        
        if command and command in COMMANDS:
            async for chunk in COMMANDS[command](message, history, task_id):
                yield chunk
            return

        # Pipe 4: Incluir attachments en el contexto del mensaje
        if attachments:
            try:
                from pathlib import Path as _Path
                names = [a.get("name", "?") for a in attachments]
                parts = [f"[Archivos adjuntos: {', '.join(names)}]"]
                
                has_docs = False
                for att in attachments:
                    att_path = att.get("path", "")
                    att_type = att.get("type", "")
                    parts.append(f"  - {att.get('name','?')} ({att_type}, {att.get('size_kb',0)}KB)")
                    
                    # Leer contenido de archivos de texto
                    if att_type in ("document", "code", "data") and att_path:
                        try:
                            fp = _Path(att_path)
                            if not fp.is_absolute():
                                fp = _Path(__file__).resolve().parent.parent.parent / att_path
                            if fp.exists() and fp.stat().st_size < 50_000:
                                content = fp.read_text(encoding="utf-8", errors="replace")
                                parts.append(f"    Contenido del archivo:\n```\n{content[:3000]}\n```")
                                has_docs = True
                        except Exception:
                            pass
                    
                    # Analizar imágenes con NIM vision
                    if att_type == "image" and att_path:
                        try:
                            import base64, os as _os2
                            fp = _Path(att_path)
                            if not fp.is_absolute():
                                fp = _Path(__file__).resolve().parent.parent.parent / att_path
                            if fp.exists():
                                img_b64 = base64.b64encode(fp.read_bytes()).decode()
                                # NIM vision API (OpenAI-compatible)
                                api_key = _os2.getenv("NVIDIA_API_KEY", "")
                                if not api_key:
                                    env_path = _Path(__file__).resolve().parent.parent.parent / ".env"
                                    if env_path.exists():
                                        for line in env_path.read_text().splitlines():
                                            if "NVIDIA_API_KEY" in line and "=" in line:
                                                api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                                                break
                                if api_key:
                                    from openai import OpenAI as _OAI
                                    client = _OAI(base_url="https://integrate.api.nvidia.com/v1", api_key=api_key)
                                    resp = client.chat.completions.create(
                                        model="meta/llama-3.2-90b-vision-instruct",
                                        messages=[{"role":"user","content":[
                                            {"type":"text","text":"Analiza esta imagen en detalle. Describe TODO lo que ves: personas, objetos, texto, UI, colores, layout. Si hay texto, transcríbelo. Sé exhaustivo."},
                                            {"type":"image_url","image_url":{"url":f"data:image/png;base64,{img_b64}"}},
                                        ]}],
                                        max_tokens=250, temperature=0.2, timeout=60,
                                    )
                                    desc = resp.choices[0].message.content or ""
                                    if desc.strip():
                                        parts.append(f"    [Análisis visual NIM: {desc.strip()}]")
                        except Exception:
                            pass  # Silencioso si NIM no disponible
                
                # Instrucción clara
                preamble = "\n\n---\n⚠️ El usuario adjuntó archivos. Debes PROCESARLOS directamente usando su contenido (ya incluido arriba). NO generes código para leer archivos - los datos ya están aquí. Responde basándote en el contenido proporcionado.\n---"
                message = preamble + "\n" + "\n".join(parts) + "\n\n" + message
            except Exception:
                pass  # Nunca romper el stream por attachments

        decision = self.gate.evaluate(
            tool="route",
            params={"message": message[:200]},
            agent_id=self.agent_name,
        )

        # F-03: Agent Loop - tool calling iterativo (ReAct pattern).
        async for chunk in self._agent_loop(message, history, task_id):
            yield chunk

    # ═══════════════════════════════════════════════════════════════
    # AGENT LOOP - ReAct pattern: think → act → observe → repeat
    # ═══════════════════════════════════════════════════════════════
    
    async def _agent_loop(
        self, message: str, history: list[dict], task_id: str, max_iterations: int = 999
    ) -> AsyncGenerator[str, None]:
        """Bucle agéntico: el LLM decide si usar herramientas o responder."""
        import json as _json
        import time
        
        # B3: Observabilidad — tracer
        tracer = get_tracer()
        
        # Inyectar contexto de Curator + memoria
        from agents.Curator.curator import Curator
        curator = Curator()
        ctx = curator.inject_project_context()
        # B2: retrieval semántico en lugar de tier-based
        lessons = curator.query(message, k=5)
        lessons_str = "\n".join(f"- {l.content[:200]}" for l in lessons) if lessons else "(sin lecciones previas)"
        context_str = f"""Proyecto: {ctx.project_name}
Archivos: {len(ctx.active_files)}
Arquitectura: {ctx.architecture_summary[:500]}
Memoria (lecciones aprendidas):
{lessons_str}"""
        
        # Tool definitions (dual format: internal list + OpenAI function calling)
        _TOOL_DEFS = [
            {"name": "read_file", "description": "Lee un archivo. Usa offset y limit para archivos grandes (ej: offset=980, limit=30 para ver la funcion closePanel)", "parameters": {"path": "ruta del archivo", "offset": "linea donde empezar (opcional)", "limit": "max lineas (opcional, default 100)"}},
            {"name": "write_file", "description": "Escribe o modifica un archivo", "parameters": {"path": "ruta del archivo", "content": "contenido a escribir"}},
            {"name": "patch_file", "description": "Edita un archivo existente (find-and-replace). Busca 'old' y lo reemplaza con 'new'.", "parameters": {"path": "ruta del archivo", "old": "texto a buscar", "new": "texto de reemplazo"}},
            {"name": "visual_audit", "description": "Toma screenshot REAL del frontend con Playwright, lo analiza con modelo de visión (NIM), e inspecciona el DOM. Devuelve análisis visual detallado + findings estructurales + errores de consola. Parámetro 'focus': área o bug específico a revisar (ej: 'sidebar', 'icons', 'spacing')", "parameters": {"focus": "área específica a auditar (opcional)", "interactions": "si true, prueba clicks en sidebar/panel (default true)"}},
            {"name": "web_search", "description": "Busca en internet", "parameters": {"query": "terminos de busqueda"}},
            {"name": "search_code", "description": "Busca en el codigo del proyecto", "parameters": {"query": "texto a buscar"}},
            {"name": "list_files", "description": "Lista archivos en un directorio del proyecto", "parameters": {"directory": "directorio (default: .)"}},
            {"name": "execute_command", "description": "Ejecuta un comando shell", "parameters": {"command": "comando a ejecutar"}},
            {"name": "background_task", "description": "Lanza una tarea larga en background y te notifica al terminar. Usa para tests, builds, instalaciones.", "parameters": {"command": "comando a ejecutar en background", "label": "etiqueta descriptiva (opcional)"}},
        ]

        # OpenAI function calling schema — descubrimiento dinámico desde MCP Manager real
        _OPENAI_TOOLS = await self.mcp.get_tools_catalog() if self.mcp else []
        # Keep backward compat — old _TOOL_DEFS used as 'tools' var in prompt
        tools = _TOOL_DEFS
        
        # B0-3: _IDENTITY_LOCK es la base de toda identidad de Orchestrator.
        # Se antepone al system prompt dinámico para que el modelo sepa quién es
        # antes de recibir cualquier instrucción operacional.
        system_prompt = _IDENTITY_LOCK + f"""

---

CONTEXTO ACTIVO DEL PROYECTO:
{context_str}

HERRAMIENTAS DISPONIBLES:
- visual_audit: Toma screenshot REAL del frontend (Playwright) + lo analiza con modelo de visión vía NIM. Devuelve bugs visuales reales, problemas de layout, iconos faltantes, errores de consola. USALA para CUALQUIER tarea de auditoría visual o frontend. Puedes pasar 'focus' para revisar un área específica.
- read_file: Lee archivos del proyecto.
- write_file: Escribe/modifica archivos completos.
- patch_file: Edita archivos existentes (find-and-replace). Útil para cambios pequeños.
- list_files: Lista directorios.
- search_code: Busca texto en el código.
- execute_command: Ejecuta comandos shell.
- web_search: Busca en internet.

ARCHIVOS CLAVE DEL PROYECTO:
- Frontend/index.html, Frontend/app.js, Frontend/style.css (UI)
- agents/Orchestrator/orchestrator.py (orquestador)
- system/task_graph_engine.py (motor)

REGLAS CRÍTICAS:

3. Si el usuario pide DETALLES que NO están en el contexto (contenido de archivos, código fuente, búsquedas, conteos exactos), DEBES usar tools o tus herramientas disponibles. NUNCA inventes contenido de archivos o resultados de búsqueda.
4. Para ARREGLAR bugs: (a) search_code para encontrar la función, (b) read_file con offset para ver el código exacto, (c) patch_file con el texto EXACTO a reemplazar, (d) read_file de nuevo para verificar. NO improvises el texto — copia EXACTAMENTE lo que leíste.
5. 
6. NO uses herramientas desconocidas, pregunta primero.
7. NO inventes resultados — si no sabes algo, se investiga.

Para usar herramienta, elige UN formato:
- TOOL: nombre | param1=valor1 | param2=valor2
- {{"tool": "nombre", "params": {{"param1": "valor1"}}}}

Para responder: texto normal (sin TOOL: ni JSON).

REGLAS DE ESTILO:
- NO te disculpes. NO digas "tienes razón", "me equivoqué", "te pido disculpas". Solo corrige y actúa.
- Si necesitas una tool, úsala DIRECTAMENTE. No escribas párrafos antes de actuar.

FORMATO DE PENSAMIENTO: Antes de responder, escribe tu razonamiento entre etiquetas <think>...</think>. Esto ayuda al usuario a seguir tu lógica. Ejemplo:
<think>El usuario pregunta por X. Voy a usar list_files para ver la estructura, luego search_code para encontrar Y.</think>
Aquí va tu respuesta visible.
"""

        # B0-2: El system prompt va como parámetro separado, NO como mensaje de rol.
        # OllamaClient(/api/chat) requiere el system en el field dedicado del payload
        # para que el chat template del modelo lo procese correctamente.
        # Incluir {"role":"system"} en el array messages era ignorado silenciosamente.
        messages = []
        for h in history[-10:]:
            messages.append(h)
        messages.append({"role": "user", "content": message})
        
        tool_calls_count = 0  # Track tool usage for convergence
        tokens_used = 0       # Track tokens for budget control
        MAX_TOKENS_PER_SESSION = 100000  # Corte duro por sesión
        
        # ── Read MAX_TOOL_CALLS from model preset ──
        # Kimi=2 (spam protection), others=3 (default)
        current_lead_model = self.router._get_role_config("orchestrator_lead")["model"]
        model_preset = self.router.config.get("model_presets", {}).get(current_lead_model, {})
        MAX_TOOL_CALLS = model_preset.get("max_tool_calls", 3)
        
        for iteration in range(max_iterations):
            # ── Budget check: corte duro si excedemos el presupuesto ──
            if tokens_used >= MAX_TOKENS_PER_SESSION:
                yield f"\n⚠️ Presupuesto de tokens agotado ({tokens_used:,}/{MAX_TOKENS_PER_SESSION:,}). Inicia una nueva sesión.\n"
                return
            
            iter_start = time.perf_counter()
            # B8: Streaming real — usar stream_complete() en lugar de complete()
            # El modelo genera tokens en tiempo real → TTFB <2s en vez de 10-60s
            pass_tools = _OPENAI_TOOLS if tool_calls_count < MAX_TOOL_CALLS else None
            
            # Consumir stream y recolectar resultado
            raw = ""
            stream_tool_calls = None
            stream_tokens_in = 0
            already_yielded_text = False  # B8: Track si ya emitimos texto via stream
            stream_reasoning = False      # ¿el modelo emitio razonamiento?
            try:
                async for chunk in self.router.stream_complete(
                    role="orchestrator_lead",
                    messages=messages,
                    system=system_prompt,
                    agent_name=self.agent_name,
                    task_id=task_id,
                    override_max_tokens=4096,
                    tools=pass_tools,
                ):
                    if chunk.type == "text":
                        # YIELD INMEDIATO — el usuario ve cada token en tiempo real
                        yield chunk.content
                        raw += chunk.content
                        already_yielded_text = True
                    elif chunk.type == "reasoning":
                        # Cadena de pensamiento en canal aparte: el cliente lo
                        # muestra en un bloque colapsable. No se acumula en `raw`
                        # porque no es la respuesta del agente.
                        yield {"__event__": "reasoning", "content": chunk.content}
                        stream_reasoning = True
                    elif chunk.type == "tool_call":
                        stream_tool_calls = chunk.tool_calls
                        stream_tokens_in = chunk.tokens_in or 0
                    elif chunk.type == "done":
                        stream_tokens_in = chunk.tokens_in or stream_tokens_in
            except Exception as e:
                # Si stream_complete falla, fallback a complete() batch
                print(f"[Orchestrator] stream_complete error: {e} — fallback a complete()")
                try:
                    response = await self.router.complete(
                        role="orchestrator_lead",
                        messages=messages,
                        system=system_prompt,
                        agent_name=self.agent_name,
                        task_id=task_id,
                        override_max_tokens=4096,
                        tools=pass_tools,
                    )
                    raw = response.content.strip()
                    stream_tool_calls = response.tool_calls
                    stream_tokens_in = response.tokens_in
                    yield raw
                except Exception as e2:
                    yield f"\n⚠️ Error LLM: {e2}"
                    return
            
            duration_ms = int((time.perf_counter() - iter_start) * 1000)
            tokens_used += stream_tokens_in  # Budget tracking
            raw = raw.strip()

            # Modelo de razonamiento que se quedó sin presupuesto antes de
            # escribir la respuesta: emitió cadena de pensamiento pero ningún
            # texto visible. Sin este aviso el usuario vería una respuesta vacía
            # sin saber por qué.
            if stream_reasoning and not raw and not stream_tool_calls:
                yield ("\n\n⚠️ El modelo agotó su presupuesto de tokens razonando antes de "
                       "escribir la respuesta. Subí `max_tokens` o usá un modelo sin "
                       "razonamiento extendido.")
            
            # REACT PATTERN: LLM decides tool vs response.
            # Priority 1: Native tool calls (function calling API, via stream)
            # Priority 2: TOOL: text parser (fallback for models without native TC)
            # Priority 3: JSON {"tool":...} parser
            tool_match = False
            
            # ── NATIVE TOOL CALLS (desde streaming o batch) ──
            if stream_tool_calls:
                for tc in stream_tool_calls:
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = _json.loads(tc["function"]["arguments"])
                    except:
                        fn_args = {}
                    
                    valid_tools = self.mcp.get_valid_tool_names()
                    if fn_name in valid_tools:
                        # Guard: skip incomplete write_file tool calls (LLM truncating content)
                        if fn_name == "write_file":
                            content = fn_args.get("content", "")
                            # Signature-only? (ends with colon, no body)
                            if len(content) < 80 and content.strip().endswith(":"):
                                print(f"[Orchestrator] Skipping truncated write_file ({len(content)} chars), waiting for TOOL: parser")
                                continue
                        
                        if tool_calls_count >= MAX_TOOL_CALLS:
                            messages.append({"role": "assistant", "content": f"[Skipped {fn_name} — convergence limit reached]"})
                            messages.append({"role": "user", "content": "You've already gathered enough information. Respond DIRECTLY to the user now. Do NOT call any more tools."})
                            continue
                        tool_calls_count += 1
                        tool_match = True
                        tool_start = time.perf_counter()
                        result = await self._dispatch_tool_collect(fn_name, fn_args, task_id)
                        tool_duration = int((time.perf_counter() - tool_start) * 1000)

                        # Eventos de política (denied / approval) antes del resultado.
                        for policy_event in self._drain_policy_events():
                            yield policy_event

                        if result is None:
                            # La acción no se ejecutó (denegada o rechazada). El
                            # modelo debe enterarse para explicárselo al usuario y
                            # NO reintentar la misma acción.
                            messages.append({"role": "user", "content": (
                                f"La tool '{fn_name}' NO se ejecutó: la política de permisos "
                                "no lo autorizó. No la reintentes. Explica al usuario qué "
                                "se intentó y por qué no se permitió."
                            )})
                            continue

                        yield {
                            "__event__": "tool",
                            "tool": fn_name,
                            "args": fn_args,
                            "result": result[:500] if result else None,
                            "duration_ms": tool_duration,
                            "error": None,
                        }
                        # Emit artifact for file creation
                        if fn_name == "write_file":
                            path = fn_args.get("path", "")
                            content = fn_args.get("content", "")
                            yield {
                                "__event__": "artifact",
                                "kind": "code",
                                "title": path.split("/")[-1] if "/" in path else path,
                                "path": path,
                                "content": content[:2000],
                                "explanation": "Archivo creado por Orchestrator",
                                "risk": "low",
                                "stats": {
                                    "lines_added": len(content.split("\n")),
                                    "lines_removed": 0,
                                    "total_lines": len(content.split("\n")),
                                },
                            }
                        
                        tracer.trace_agent_loop(
                            task_id=task_id,
                            iteration=iteration,
                            tool=fn_name,
                            result=result[:500] if result else None,
                            tokens=stream_tokens_in,
                            duration_ms=duration_ms + tool_duration,
                            model=current_lead_model,
                        )
                        
                        # Append tool result in universal format (role:user instead of role:tool)
                        # role:tool is OpenAI-specific and breaks Ollama/fallbacks
                        messages.append({"role": "user", "content": f"Tool '{fn_name}' executed. Result ({len(result)} chars): {result[:800]}\n\nNow respond to the user naturally with the answer."})
                        try:
                            lesson = f"[{fn_name}] {fn_args.get('path', fn_args.get('query', ''))[:80]}: {result[:120]}"
                            curator.record_lesson(lesson)
                        except:
                            pass
                        continue
            
            # ── TEXT TOOL PARSER (fallback: TOOL: format) ──
            # Si tools deshabilitadas (convergencia), salir del loop sin parsear TOOL:
            if pass_tools is None:
                break
            clean_raw = raw.replace("**TOOL:**", "TOOL:").replace("*TOOL:*", "TOOL:")
            tool_idx = clean_raw.find("TOOL:")
            if tool_idx >= 0:
                tool_line = clean_raw[tool_idx + len("TOOL:"):].strip()
                # Format: TOOL: tool_name | param1=value1 | param2=value2
                try:
                    # Take only the first line of tool spec (LLM may add trailing text)
                    tool_line = tool_line.split('\n')[0].strip()
                    parts = tool_line.split("|")
                    tool_name = parts[0].strip()
                    params = {}
                    for p in parts[1:]:
                        if "=" in p:
                            key, val = p.split("=", 1)
                            # Strip quotes around values (models often add them)
                            val = val.strip().strip('"').strip("'")
                            params[key.strip()] = val

                    # Normalizar nombres de parámetros (el modelo a veces usa alias)
                    _ALIASES = {'file_path': 'path', 'dir': 'directory', 'folder': 'directory',
                                'cmd': 'command', 'pattern': 'query'}
                    for alias, real in _ALIASES.items():
                        if alias in params and real not in params:
                            params[real] = params.pop(alias)

                    valid_tools = self.mcp.get_valid_tool_names()  # B1: catálogo dinámico

                    if tool_name in valid_tools:
                        # Circuit breaker: if already used MAX_TOOL_CALLS, force direct response
                        if tool_calls_count >= MAX_TOOL_CALLS:
                            # Ask the LLM one more time with all context to respond directly
                            messages.append({"role": "assistant", "content": f"[Skipped {tool_name} — convergence limit reached]"})
                            messages.append({"role": "user", "content": "You've already gathered enough information. Respond DIRECTLY to the user now. Do NOT call any more tools."})
                            continue
                        tool_calls_count += 1
                        tool_match = True
                        tool_start = time.perf_counter()
                        result = await self._dispatch_tool_collect(tool_name, params, task_id)
                        tool_duration = int((time.perf_counter() - tool_start) * 1000)
                        for policy_event in self._drain_policy_events():
                            yield policy_event
                        if result is None:
                            messages.append({"role": "user", "content": (
                                f"La tool '{tool_name}' NO se ejecutó: la política de permisos "
                                "no lo autorizó. No la reintentes; explícale al usuario qué se "
                                "intentó y por qué no se permitió."
                            )})
                            continue

                        # B5-VISUAL-03: Emit tool event for frontend structured rendering
                        yield {
                            "__event__": "tool",
                            "tool": tool_name,
                            "args": params,
                            "result": result[:500] if result else None,
                            "duration_ms": tool_duration,
                            "error": None,
                        }

                        # B3: Trace agent loop iteration with tool
                        tracer.trace_agent_loop(
                            task_id=task_id,
                            iteration=iteration,
                            tool=tool_name,
                            result=result[:500] if result else None,
                            tokens=stream_tokens_in,
                            duration_ms=duration_ms + tool_duration,
                            model=current_lead_model,
                        )
                        
                        # Append result — use 'user' role for tool feedback (NVIDIA NIM and most APIs
                        # reject/ignore 'system' messages in the middle of the conversation array).
                        # The LLM must SEE the tool result to know it already acted.
                        messages.append({"role": "user", "content": f"Tool '{tool_name}' executed. Result ({len(result)} chars): {result[:800]}\n\nNow respond to the user naturally with the answer."})
                        # Register lesson
                        try:
                            lesson = f"[{tool_name}] {params.get('path', params.get('query', ''))[:80]}: {result[:120]}"
                            curator.record_lesson(lesson)
                        except:
                            pass
                        continue
                except Exception:
                    pass

                # ── BARE TOOL PARSER: read_file path (sin TOOL:) ──
                # GLM a veces escribe "read_file docs/X.md" sin prefijo TOOL:
                bare_tools = ["read_file", "list_files", "search_code", "write_file", "patch_file",
                             "execute_command", "visual_audit", "web_search"]
                first_word = raw.strip().split()[0] if raw.strip() else ""
                if first_word in bare_tools:
                    tool_name = first_word
                    rest = raw.strip()[len(tool_name):].strip()
                    params = {}
                    if tool_name in ("read_file", "write_file", "patch_file"):
                        params["path"] = rest.split()[0] if rest else ""
                    elif tool_name in ("list_files",):
                        params["directory"] = rest.split()[0] if rest else "."
                    elif tool_name in ("search_code", "web_search"):
                        params["query"] = rest
                    elif tool_name in ("execute_command",):
                        params["command"] = rest
                
                    if tool_calls_count < MAX_TOOL_CALLS:
                        tool_calls_count += 1
                        tool_match = True
                        result = await self._dispatch_tool_collect(tool_name, params, task_id)
                        for policy_event in self._drain_policy_events():
                            yield policy_event
                        if result is None:
                            messages.append({"role": "user", "content": (
                                f"La tool '{tool_name}' NO se ejecutó: la política de permisos "
                                "no lo autorizó. No la reintentes; explícale al usuario qué se "
                                "intentó y por qué no se permitió."
                            )})
                            continue
                        yield {"__event__": "tool", "tool": tool_name, "args": params,
                               "result": result[:500] if result else None, "duration_ms": 0, "error": None}
                        # Emit artifact for file creation
                        if tool_name == "write_file":
                            path = params.get("path", "")
                            content = params.get("content", "")
                            yield {
                                "__event__": "artifact",
                                "kind": "code",
                                "title": path.split("/")[-1] if "/" in path else path,
                                "path": path,
                                "content": content[:2000],
                                "explanation": "Archivo creado por Orchestrator",
                                "risk": "low",
                                "stats": {
                                    "lines_added": len(content.split("\n")),
                                    "lines_removed": 0,
                                    "total_lines": len(content.split("\n")),
                                },
                            }
                        messages.append({"role": "user", "content": f"Tool '{tool_name}' executed. Result ({len(result)} chars): {result[:800]}\n\nNow respond to the user naturally with the answer."})
                        continue

                # Also try JSON-based tool detection as fallback (backward compat)
            if not tool_match:
                # OpenAI native format: {"name": "read_file", "arguments": {"path": "..."}}
                json_start = raw.find('{"name"')
                if json_start < 0:
                    json_start = raw.find('{"tool"')
                if json_start >= 0:
                    brace_count = 0
                    json_end = -1
                    for i in range(json_start, len(raw)):
                        if raw[i] == '{': brace_count += 1
                        elif raw[i] == '}':
                            brace_count -= 1
                            if brace_count == 0:
                                json_end = i
                                break
                    if json_end > json_start:
                        try:
                            raw_json = raw[json_start:json_end+1]
                            tool_data = _json.loads(raw_json)
                            # OpenAI native format: {"name": "read_file", "arguments": {...}}
                            tool_name = tool_data.get("name") or tool_data.get("tool", "")
                            params = tool_data.get("arguments") or tool_data.get("params", {})
                            valid_tools = self.mcp.get_valid_tool_names()  # B1: catálogo dinámico
                            if tool_name in valid_tools:
                                # Circuit breaker: convergence limit
                                if tool_calls_count >= MAX_TOOL_CALLS:
                                    messages.append({"role": "assistant", "content": f"[Skipped {tool_name} — convergence limit reached]"})
                                    messages.append({"role": "user", "content": "You've already gathered enough information. Respond DIRECTLY to the user now. Do NOT call any more tools."})
                                    continue
                                tool_calls_count += 1
                                tool_match = True
                                tool_start = time.perf_counter()
                                result = await self._dispatch_tool_collect(tool_name, params, task_id)
                                tool_duration = int((time.perf_counter() - tool_start) * 1000)

                                for policy_event in self._drain_policy_events():
                                    yield policy_event
                                if result is None:
                                    messages.append({"role": "user", "content": (
                                        f"La tool '{tool_name}' NO se ejecutó: la política de "
                                        "permisos no lo autorizó. No la reintentes; explícale al "
                                        "usuario qué se intentó y por qué no se permitió."
                                    )})
                                    continue
                                
                                # B3: Trace agent loop iteration with tool
                                tracer.trace_agent_loop(
                                    task_id=task_id,
                                    iteration=iteration,
                                    tool=tool_name,
                                    result=result[:500] if result else None,
                                    tokens=stream_tokens_in,
                                    duration_ms=duration_ms + tool_duration,
                                    model=current_lead_model,
                                )
                                
                                messages.append({"role": "user", "content": f"Tool '{tool_name}' executed. Result ({len(result)} chars): {result[:800]}\n\nNow respond to the user naturally with the answer."})
                                try:
                                    lesson = f"[{tool_name}] {params.get('path', params.get('query', ''))[:80]}: {result[:120]}"
                                    curator.record_lesson(lesson)
                                except:
                                    pass
                                continue
                        except json.JSONDecodeError:
                            pass
            
            # Not a tool call - text response 
            if not tool_match:
                # B3: Trace final response (is_final=True)
                tracer.trace_agent_loop(
                    task_id=task_id,
                    iteration=iteration,
                    tool=None,
                    result=None,
                    tokens=stream_tokens_in,
                    duration_ms=duration_ms,
                    model=current_lead_model,
                    is_final=True,
                )
                # B8: Solo hacer yield si no se emitio texto via stream (evitar duplicado)
                if not already_yielded_text and raw:
                    yield raw
                return
        
        # Max iterations reached — shouldn't happen with convergence guard
        return
    
    async def _execute_tool(self, tool_name: str, params: dict) -> str:
        """
        Dispatch via MCPClient (B1).
        El catálogo de tools es dinámico — registrar un nuevo MCP server
        lo expone a Orchestrator sin tocar este método.

        ATENCIÓN: esta función NO consulta la política. Para ejecutar una tool
        desde el loop del agente hay que usar `_dispatch_tool()`, que sí la
        consulta. Se mantiene pública porque los comandos CLI y el motor de task
        graphs la llaman con la política ya resuelta en su propio camino.
        """
        await self._ensure_mcp()
        result = await self.mcp.call(tool_name, params)
        # Telemetría independiente: verificar que la acción realmente ocurrió
        self._verify_tool(tool_name, params, result)
        return result

    async def _dispatch_tool_collect(self, tool_name: str, params: dict, task_id: str = "") -> Optional[str]:
        """Ejecuta `_dispatch_tool` y devuelve solo el resultado.

        Los eventos de política se acumulan en `self._pending_policy_events` para
        que el punto de llamada los emita antes del evento `tool`. Existe porque
        el loop ReAct tiene cuatro ramas de dispatch y ninguna puede yield-ear
        desde una expresión `await`.
        """
        result: Optional[str] = None
        events = getattr(self, "_pending_policy_events", None)
        if events is None:
            events = self._pending_policy_events = []
        async for event, tool_result in self._dispatch_tool(tool_name, params, task_id):
            if event is not None:
                events.append(event)
            else:
                result = tool_result
        return result

    def _drain_policy_events(self) -> list[dict]:
        """Devuelve y limpia los eventos de política pendientes del último dispatch."""
        events = getattr(self, "_pending_policy_events", None) or []
        self._pending_policy_events = []
        return events

    async def _dispatch_tool(self, tool_name: str, params: dict, task_id: str = ""):
        """Ejecuta una tool **pasando por la política de permisos**.

        Es el único camino correcto para el loop ReAct. Genera eventos tipados
        para que el cliente pueda mostrar la decisión:

          - `tool.denied`          → la política lo prohíbe (irrevocable)
          - `approval.requested`   → requiere decisión humana; el generador se
                                     pausa sin cortar el stream
          - `approval.resolved`    → el humano decidió
          - `tool.started`/`tool`  → ejecución

        Contrato de salida: yield de tuplas `(evento_dict | None, resultado)`.
        `resultado` es None si la acción no llegó a ejecutarse (denegada,
        rechazada o sin respuesta): en ese caso el loop agrega una observación
        para que el modelo explique al usuario en vez de reintentar.
        """
        from api.policy import PolicyContext

        ctx = self._PolicyContext(
            workspace=self._ctx.workspace,
            sandbox=self._ctx.sandbox,
            session_dir=self._ctx.session_dir,
            run_id=task_id or self._ctx.run_id,
            approval_timeout_s=self._ctx.approval_timeout_s,
        )

        gate = self._get_gate()  # recarga sola si cambió gate_rules.yaml
        decision = gate.evaluate(tool_name, params, agent_id=self.agent_name, context=ctx)
        effect = decision.effect

        # ── permit: ejecutar ─────────────────────────────────────────
        # No se emite evento de política para `permit`: es el caso normal y
        # llenaría el stream de ruido. La decisión queda registrada en
        # `gate_log` y visible en el panel de sistema.
        if effect == "permit":
            result = await self._execute_tool(tool_name, params)
            yield None, result
            return

        # ── deny: no se ejecuta, y no se negocia ─────────────────────
        if effect == "deny":
            yield {
                "__event__": "tool.denied",
                "tool": tool_name,
                "rule_id": decision.rule_id,
                "risk": decision.risk,
                "reason": decision.reason,
                "request_id": decision.request_id,
            }, None
            return

        # ── ask: pausar el generador y esperar decisión humana ───────
        yield {
            "__event__": "approval.requested",
            "request_id": decision.request_id,
            "tool": tool_name,
            "agent_id": decision.agent_id,
            "rule_id": decision.rule_id,
            "risk": decision.risk,
            "reason": decision.reason,
            "matched": decision.matched,
            "params_preview": _redact_params(params),
        }, None

        approved = await self._broker.request(
            decision.as_dict(), params=params, timeout_s=ctx.approval_timeout_s
        )

        yield {
            "__event__": "approval.resolved",
            "request_id": decision.request_id,
            "tool": tool_name,
            "approved": bool(approved),
        }, None

        if not approved:
            return
        result = await self._execute_tool(tool_name, params)
        yield None, result

    def _verify_tool(self, tool_name: str, params: dict, result: str) -> None:
        """Verificación post-ejecución: ¿el tool realmente hizo lo que dijo?"""
        from pathlib import Path as _P
        try:
            if tool_name in ("write_file", "patch_file"):
                p = _P(params.get("path", ""))
                if not p.is_absolute():
                    p = _P(__file__).resolve().parent.parent.parent / p
                if p.exists():
                    size = p.stat().st_size
                    if size == 0:
                        print(f"[VERIFY] ⚠️  {tool_name}: {p} existe pero está vacío (0 bytes)")
                else:
                    print(f"[VERIFY] ❌ {tool_name}: {p} NO existe — la escritura falló")
            elif tool_name == "execute_command":
                if "error" in result.lower() or "exit: 1" in result.lower() or "exit: 2" in result.lower():
                    print(f"[VERIFY] ⚠️  execute_command: posible error — {result[:200]}")
        except Exception as e:
            print(f"[VERIFY] Error verificando {tool_name}: {e}")

    async def _execute_tool_legacy(self, tool_name: str, params: dict) -> str:
        """LEGACY — mantenido como fallback. No usar directamente.
        Todo dispatch debe ir via self.mcp.call() (ver _execute_tool arriba).
        """
        import json as _json
        try:
            if tool_name in ("read_file", "leer", "read"):
                from pathlib import Path as _P
                p = _P(params.get("path", ""))
                if not p.is_absolute():
                    p = _P(__file__).resolve().parent.parent.parent / p
                if not p.exists():
                    return f"Error: no encontrado: {p}"
                if p.is_dir():
                    return await self._list_files(str(p.relative_to(_P(__file__).resolve().parent.parent.parent)) or ".")
                content = p.read_text(encoding="utf-8", errors="replace")
                lines = content.split("\n")
                offset = int(params.get("offset", 0) or 0)
                limit = int(params.get("limit", 100) or 100)
                if offset > 0:
                    lines = lines[offset-1:]
                if limit and len(lines) > limit:
                    lines = lines[:limit]
                result = "\n".join(lines)
                return f"Archivo: {p} (lineas {offset+1 if offset else 1}-{offset+len(lines)}, total {len(content.split(chr(10)))}):\n{result}"
            
            elif tool_name in ("write_file", "escribir", "crear_archivo"):
                from pathlib import Path as _P
                p = _P(params.get("path", ""))
                if not p.is_absolute():
                    p = _P(__file__).resolve().parent.parent.parent / p
                content = params.get("content", "")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                return f"Archivo escrito: {p} ({len(content)} chars)"
            
            elif tool_name in ("patch_file", "patch", "editar"):
                from pathlib import Path as _P
                p = _P(params.get("path", ""))
                if not p.is_absolute():
                    p = _P(__file__).resolve().parent.parent.parent / p
                old = params.get("old", "")
                new = params.get("new", "")
                if not p.exists():
                    return f"Error: archivo no encontrado: {p}"
                content = p.read_text(encoding="utf-8")
                if old not in content:
                    return f"Error: texto 'old' no encontrado en {p}. Primeras 100 chars del archivo: {content[:100]}"
                content = content.replace(old, new, 1)
                p.write_text(content, encoding="utf-8")
                return f"Archivo parcheado: {p} (reemplazo exitoso)"
            
            elif tool_name == "web_search":
                from system.task_graph_engine import TaskGraphEngine
                engine = TaskGraphEngine()
                return await engine._web_search(params.get("query", ""))
            
            elif tool_name in ("search_code", "search", "buscar", "grep"):
                from pathlib import Path as _P
                import subprocess
                query = params.get("query", "")
                base = _P(__file__).resolve().parent.parent.parent
                result = subprocess.run(
                    ["grep", "-rn", "--include=*.py", "--include=*.js", "--include=*.html", query, str(base / "agents"), str(base / "Frontend"), str(base / "system"), str(base / "api")],
                    capture_output=True, text=True, timeout=10
                )
                return result.stdout[:2000] or "Sin resultados"
            
            elif tool_name in ("list_files", "list", "ls", "dir"):
                return await self._list_files(params.get("directory", "."))
            
            elif tool_name in ("execute_command", "exec", "shell", "run"):
                import subprocess
                result = subprocess.run(
                    params.get("command", ""), shell=True, capture_output=True, text=True, timeout=30
                )
                return f"Exit: {result.returncode}\n{result.stdout[:1000]}{result.stderr[:500]}"
            
            elif tool_name == "background_task":
                import urllib.request as _ur, urllib.error as _ue, json as _j
                from api.ports import url as _api_url
                cmd = params.get("command", "")
                label = params.get("label", cmd[:60])
                # El puerto venia hardcodeado a 8000 mientras el server podia
                # estar en 8001: el urlopen levantaba URLError sin capturar y
                # tumbaba la tool. api/ports.py es la fuente unica.
                endpoint = _api_url("/tasks/background")
                req = _ur.Request(
                    endpoint,
                    data=_j.dumps({"command": cmd, "label": label}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                try:
                    resp = _ur.urlopen(req, timeout=10)
                    data = _j.loads(resp.read())
                except _ue.URLError as e:
                    return (f"No se pudo lanzar la tarea en background: el backend "
                            f"no responde en {endpoint} ({e.reason}). "
                            f"Verifica que el servidor este arriba.")
                except Exception as e:
                    return f"Error lanzando tarea en background: {type(e).__name__}: {e}"
                return f"Tarea lanzada en background: {data['task_id']} — {label}\nUsa check_task({data['task_id']}) para verificar estado."
            
            return f"Herramienta desconocida: {tool_name}"
        except Exception as e:
            return f"Error ejecutando {tool_name}: {e}"
    
    async def _list_files(self, directory: str = ".") -> str:
        """Lista archivos en un directorio."""
        from pathlib import Path as _P
        import os
        base = _P(__file__).resolve().parent.parent.parent
        p = base / directory if not _P(directory).is_absolute() else _P(directory)
        if not p.exists():
            return f"Directorio no encontrado: {p}"
        items = []
        for entry in sorted(os.scandir(p), key=lambda e: e.name):
            tipo = "📁" if entry.is_dir() else "📄"
            items.append(f"  {tipo} {entry.name}")
        return f"{p} — {len(items)} elementos:\n" + "\n".join(items[:50])

    # _classify, _extract_json_classification, _classify_heuristic eliminados (B4 cleanup)
    # El sistema rutea todo por Task Graph — clasificador no se usaba en flujo principal.

    async def _respond_simple(
        self,
        message: str,
        history: list[dict],
        task_id: str,
    ) -> AsyncGenerator[str, None]:
        try:
            state = read_state()
            state_context = (
                "Estado actual del sistema:\n"
                f"- Proyecto activo: {state.get('proyecto_activo', 'N/A')}\n"
                f"- Agentes disponibles: {', '.join(state.get('agentes_disponibles', []))}\n"
                f"- Última acción: {state.get('ultima_accion_real', 'N/A')}\n"
            )
            full_system = f"{_SIMPLE_RESPONSE_PROMPT}\n\n{state_context}"
        except Exception:
            full_system = _SIMPLE_RESPONSE_PROMPT

        messages = list(history[-10:])
        messages.append({"role": "user", "content": message})

        try:
            async for chunk in self.router.stream(
                role="orchestrator_lead",
                messages=messages,
                system=full_system,
                agent_name=self.agent_name,
                task_id=task_id,
            ):
                yield chunk

            update_ultima_accion(
                f"Orchestrator respondió consulta simple | task_id={task_id}"
            )

        except Exception as e:
            error_msg = f"⚠️ Error al procesar la consulta: {e}"
            yield error_msg

    # ═══════════════════════════════════════════════════════════════
    # H-04: COMANDOS CLI (/loop, /audit, /fix, /search, /viz, /help)
    # ═══════════════════════════════════════════════════════════════

    async def _cmd_audit(self, message: str, history: list[dict], task_id: str):
        """Comando /audit - auditoria visual unica del frontend."""
        yield "🔍 **Auditoría visual iniciada...**\n"
        from system.task_graph_engine import TaskGraphEngine
        engine = TaskGraphEngine()
        try:
            result = await engine._run_visual_audit(message or "")
            import json
            data = json.loads(result) if isinstance(result, str) else result
            findings = data.get("findings", [])
            real = [f for f in findings if not f.startswith("HIDDEN:")]
            yield f"   📸 `{data.get('screenshot','?')}`\n"
            yield f"   🔍 {len(real)} bugs reales:\n"
            for f in real[:15]:
                yield f"     • {f}\n"
            if not real:
                yield "   ✅ Sin bugs detectados.\n"
        except Exception as e:
            yield f"   ❌ Error: {e}\n"

    async def _cmd_fix(self, message: str, history: list[dict], task_id: str):
        """Comando /fix - arregla bugs usando Task Graph (plan_diff + apply_diff)."""
        async for chunk in self._handle_complex(
            f"ARRÉGLALO AHORA. {message}. Modifica archivos del Frontend/. Usa plan_diff + apply_diff. NO crees scripts nuevos.",
            history, task_id
        ):
            yield chunk

    async def _cmd_search(self, message: str, history: list[dict], task_id: str):
        """Comando /search - busca en internet."""
        yield f"🌐 **Buscando: {message[:100]}...**\n"
        from system.task_graph_engine import TaskGraphEngine
        engine = TaskGraphEngine()
        result = await engine._web_search(message)
        import json
        data = json.loads(result) if isinstance(result, str) else result
        for r in data.get("results", [])[:5]:
            yield f"   • **{r.get('title','?')}**\n"
            yield f"     {r.get('snippet','')[:200]}\n"
            yield f"     {r.get('url','')}\n\n"

    async def _cmd_viz(self, message: str, history: list[dict], task_id: str):
        """Comando /viz - screenshot rápido sin análisis (debugging)."""
        yield "📸 **Capturando screenshot...**\n"
        from system.task_graph_engine import TaskGraphEngine
        engine = TaskGraphEngine()
        result = await engine._run_visual_audit(message or "", analyze=False)
        import json
        data = json.loads(result) if isinstance(result, str) else result
        yield f"   Screenshot: `{data.get('screenshot','?')}`\n"

    async def _cmd_help(self, message: str, history: list[dict], task_id: str):
        """Comando /help - lista todos los comandos disponibles."""
        yield "**Comandos V-Core:**\n\n"
        yield "| Comando | Acción |\n|---------|--------|\n"
        yield "| `/loop <tarea>` | Polish Loop: audita → arregla → reaudita (máx 5 iteraciones) |\n"
        yield "| `/audit` | Auditoría visual única del frontend |\n"
        yield "| `/fix <bug>` | Arregla un bug específico |\n"
        yield "| `/search <query>` | Busca en internet |\n"
        yield "| `/viz` | Screenshot rápido sin análisis |\n"
        yield "| `/help` | Esta ayuda |\n"

    async def _execute_direct(
        self, message: str, history: list[dict], task_id: str
    ) -> AsyncGenerator[str, None]:
        """F-05: Ejecución directa sin Task Graph para tareas simples de código."""
        yield "⚡ **Ejecutando directamente...**\n"
        
        from system.task_graph_engine import TaskGraphEngine
        engine = TaskGraphEngine()
        
        # 1. Contexto
        ctx = engine.curator.inject_project_context()
        curator_ctx = ctx.model_dump_json() if hasattr(ctx, "model_dump_json") else str(ctx)
        
        # 2. Plan
        yield "📋 Generando diff...\n"
        try:
            proposal = await engine.planner.plan_diff(
                context=curator_ctx[:3000],
                task=message,
            )
            proposal_json = proposal.model_dump_json() if hasattr(proposal, "model_dump_json") else str(proposal)
            import json
            proposal_data = json.loads(proposal_json) if isinstance(proposal_json, str) else proposal_json
            file_path = proposal_data.get("file", "")
            yield f"   📄 Archivo: `{file_path}`\n"
            yield f"   📝 {proposal_data.get('explanation', '')[:150]}\n"
        except Exception as e:
            yield f"   ❌ Error en plan: {e}\n"
            return
        
        # 3. Apply
        yield "🔧 Aplicando cambios...\n"
        try:
            engine.planner.apply_diff(proposal)
            yield "   ✅ Cambios aplicados\n"
        except Exception as e:
            yield f"   ❌ Error aplicando: {e}\n"
            yield "   Intentando con patch_file como fallback...\n"
            try:
                old_str = proposal_data.get("search_block", "")
                new_str = proposal_data.get("replace_block", "")
                result = engine._patch_file(file_path, old_str, new_str)
                yield f"   ✅ patch_file: {result[:100]}\n"
            except Exception as e2:
                yield f"   ❌ Fallback también falló: {e2}\n"
                return
        
        # 4. Verify
        yield "✅ Verificando...\n"
        try:
            vr = engine.planner.shadow_verify(file_path)
            if vr.passed:
                yield "   ✅ Verificación OK\n"
            else:
                yield f"   ⚠️ Verificación: {vr.stderr[:200]}\n"
        except Exception as e:
            yield f"   ⚠️ No se pudo verificar: {e}\n"
        
        yield "\n✅ **Cambio completado.**\n"
        # F-05: Emitir artefacto para que el frontend muestre card + descarga
        if file_path and proposal_data.get("replace_block"):
            import json as _json
            search = proposal_data.get("search_block", "")
            replace = proposal_data.get("replace_block", "")
            yield {
                "__event__": "artifact",
                "kind": "code",
                "title": file_path.split("/")[-1] if "/" in file_path else file_path,
                "path": file_path,
                "content": replace if replace else search,
                "action": "plan_diff",
                "explanation": proposal_data.get("explanation", "")[:200],
                "risk": proposal_data.get("estimated_risk", "low"),
                "stats": {
                    "lines_added": max(0, len(replace.split("\n")) - len(search.split("\n")) if search else len(replace.split("\n"))),
                    "lines_removed": max(0, len(search.split("\n")) - len(replace.split("\n")) if search else 0),
                    "total_lines": len(replace.split("\n")) if replace else 0,
                },
            }

    async def _polish_loop(
        self,
        message: str,
        history: list[dict],
        task_id: str,
        max_iterations: int = 999,
        max_tokens_per_iteration: int = 8000,
        max_total_tokens: int = 40000,
        stuck_threshold: int = 2,
    ) -> AsyncGenerator[str, None]:
        """F-04: Polish Loop con circuit breaker.
        
        Circuit breakers:
        - max_iterations: máximo de ciclos (default 5)
        - max_tokens_per_iteration: tokens máximos por ciclo (default 8000)
        - max_total_tokens: tokens totales antes de abortar (default 40000)
        - stuck_threshold: si mismos bugs aparecen N veces seguidas → stuck → aborta
        - Si un fix falla 2 veces sobre el mismo bug → lo skippea
        """
        yield "🔄 **Polish Loop activado**\n"
        yield f"   ⚡ Circuit breaker: max {max_iterations} iteraciones, {max_total_tokens} tokens totales\n\n"
        
        from system.task_graph_engine import TaskGraphEngine
        
        total_tokens = 0
        prev_bugs: set[str] = set()
        stuck_count = 0
        failed_fixes: dict[str, int] = {}  # bug → consecutive failures
        
        for iteration in range(1, max_iterations + 1):
            # ═══ CIRCUIT BREAKER: token budget ═══
            if total_tokens >= max_total_tokens:
                yield f"\n🛑 **Circuit breaker: límite de tokens alcanzado ({total_tokens}/{max_total_tokens})**\n"
                return
            
            yield f"---\n## 🔁 Iteración {iteration}/{max_iterations} | Tokens: {total_tokens}/{max_total_tokens}\n\n"
            
            # ═══ Fase 1: Auditoría visual ═══
            yield "👁️ **Fase 1: Auditoría visual...**\n"
            engine = TaskGraphEngine()
            
            try:
                result_json = await engine._run_visual_audit("")
                total_tokens += 500  # estimado: Playwright + NIM vision
                
                import json
                data = json.loads(result_json) if isinstance(result_json, str) else result_json
                findings = data.get("findings", [])
                
                yield f"   📸 Screenshot: `{data.get('screenshot', '?')}`\n"
                yield f"   🔍 Hallazgos: {len(findings)}\n"
                
                # Filtrar bugs reales
                real_bugs = [f for f in findings if 
                            f.startswith("MISSING:") or 
                            f.startswith("CONSOLE:") or
                            "error" in f.lower()]
                real_bugs = [b for b in real_bugs if not b.startswith("HIDDEN:")]
                
                for f in real_bugs[:10]:
                    yield f"     • {f}\n"
                
                # ═══ CIRCUIT BREAKER: sin bugs → éxito ═══
                if not real_bugs:
                    yield f"\n✅ **UI limpia - 0 bugs en iteración {iteration}**\n"
                    yield f"🎉 Polish Loop completado. Tokens usados: {total_tokens}\n"
                    return
                
                # ═══ CIRCUIT BREAKER: stuck detection ═══
                current_bugs = set(real_bugs)
                if current_bugs == prev_bugs:
                    stuck_count += 1
                    yield f"   ⚠️ Mismos bugs que iteración anterior (stuck={stuck_count}/{stuck_threshold})\n"
                    if stuck_count >= stuck_threshold:
                        yield f"\n🛑 **Circuit breaker: stuck detectado** - {len(real_bugs)} bugs sin resolver tras {stuck_count} intentos.\n"
                        yield f"   Bugs persistentes: {', '.join(real_bugs[:3])}...\n"
                        yield f"   Requiere intervención manual.\n"
                        return
                else:
                    stuck_count = 0
                prev_bugs = current_bugs
                
                # ═══ CIRCUIT BREAKER: skip bugs que fallaron 2+ veces ═══
                skippable = [b for b, c in failed_fixes.items() if c >= 2]
                active_bugs = [b for b in real_bugs if b not in skippable]
                if skippable:
                    yield f"   ⏭️ Skippeando {len(skippable)} bugs rebeldes (fallaron 2+ fixes): {skippable[:3]}\n"
                if not active_bugs:
                    yield f"\n⚠️ Todos los bugs restantes son rebeldes. Requiere intervención manual.\n"
                    return
                
                yield f"\n🐛 **{len(active_bugs)} bugs activos** (+{len(skippable)} skippeados)\n"
                
            except Exception as e:
                yield f"   ❌ Error en auditoría: {e}\n"
                return
            
            # ═══ CIRCUIT BREAKER: token check antes de fix ═══
            if total_tokens + max_tokens_per_iteration > max_total_tokens:
                yield f"\n🛑 **Circuit breaker: fix consumiría ~{max_tokens_per_iteration} tokens, excediendo límite**\n"
                return
            
            # ═══ Fase 2: Fix ═══
            yield f"\n🔧 **Fase 2: Generando fixes...** (~{max_tokens_per_iteration} tokens)\n"
            total_tokens += max_tokens_per_iteration  # estimado conservador
            
            bugs_text = "\n".join(f"  - {b}" for b in active_bugs[:5])
            fix_message = (
                f"ARRÉGLALOS AHORA. Bugs en el frontend:\n{bugs_text}\n\n"
                f"Corrige modificando archivos del Frontend/ (index.html, app.js, style.css). "
                f"Usa plan_diff + apply_diff. NO crees scripts nuevos."
            )
            
            try:
                fix_graph = await self._build_task_graph(fix_message, f"{task_id}_fix_{iteration}")
                yield f"   📋 Plan: {fix_graph.description} ({len(fix_graph.nodes)} nodos)\n"
                
                fix_result = await engine.execute_graph(
                    task_id=f"{task_id}_fix_{iteration}",
                    graph_definition=fix_graph.as_dict(),
                    auto_approve=True,
                )
                
                if fix_result.success:
                    yield f"   ✅ Fix aplicado\n"
                    # Reset failure count for fixed bugs
                    for b in active_bugs:
                        failed_fixes.pop(b, None)
                else:
                    yield f"   ⚠️ Fix parcial: {fix_result.error or 'algunos nodos fallaron'}\n"
                    for b in active_bugs:
                        failed_fixes[b] = failed_fixes.get(b, 0) + 1
                    
            except Exception as e:
                yield f"   ❌ Error: {e}\n"
                for b in active_bugs:
                    failed_fixes[b] = failed_fixes.get(b, 0) + 1
                continue
            
            yield f"\n⏳ Siguiente iteración...\n"
        
        return  # max iterations reached, silently exit
        yield "\n🏁 **Polish Loop finalizado.**\n"

    async def _handle_complex(
        self,
        message: str,
        history: list[dict],
        task_id: str,
        escalate: bool = False,
    ) -> AsyncGenerator[str, None]:
        yield "💭 Pensando en lo que necesitas...\n"
        yield "📋 Diseñando un plan de acción...\n"

        try:
            graph = await self._build_task_graph(message, task_id, escalate=escalate)
            yield f"✅ Task Graph generado: {graph.description} ({len(graph.nodes)} nodos)\n"

            is_valid, errors = validate_graph(graph)
            if not is_valid:
                error_detail = "; ".join(errors[:3])
                yield f"❌ Task Graph inválido: {error_detail}\n"
                yield "↩️ Cayendo a modo simple...\n"
                async for chunk in self._respond_simple(message, history, task_id):
                    yield chunk
                return

            yield "✅ Task Graph validado (DAG sin ciclos)\n"

            # B-01: Emitir evento task_graph para activar el panel GRAPH
            yield {
                "__event__": "task_graph",
                "graph_id": task_id,
                "nodes": [
                    {
                        "id": n.id,
                        "agent": n.agent,
                        "action": n.action,
                        "token_budget": n.token_budget,
                        "status": "pending",
                        "tokens_used": 0,
                    }
                    for n in graph.nodes
                ],
            }

            # --- M5: Auto-approval Nivel B ---
            has_approval_required = any(n.ask_approval for n in graph.nodes)
            auto_approve = not has_approval_required  # sin ask_approval → auto
            if auto_approve:
                yield "🟢 Sin nodos de aprobación requerida - ejecución automática\n"

            # --- CABLEADO M2: Ejecutar con TaskGraphEngine ---
            from system.task_graph_engine import TaskGraphEngine
            engine = TaskGraphEngine()

            # Preparar definicion para el engine
            graph_definition = graph.as_dict()

            yield "\n🚀 Ejecutando Task Graph...\n"
            yield "─" * 40 + "\n"

            # Callback para reportar progreso en tiempo real
            progress_log: list[dict] = []

            def on_progress(event: dict) -> None:
                progress_log.append(event)

            result = await engine.execute_graph(
                task_id=task_id,
                graph_definition=graph_definition,
                auto_approve=auto_approve,
                progress_callback=on_progress,
            )

            # Reportar resultado de cada nodo ejecutado
            for node in result.nodes:
                if node.status == "PENDING":
                    continue  # No se llego a ejecutar

                agent_icon = {
                    "Curator": "📖",
                    "Planner": "💻",
                    "Retriever": "🔍",
                    "Orchestrator": "👁️",
                    None: "⚙️",
                }.get(node.agent, "⚙️")

                status_icon = {
                    "SUCCESS": "✅",
                    "FAILED": "❌",
                    "AWAITING_APPROVAL": "🔐",
                    "RUNNING": "⏳",
                    "SKIPPED": "⏭️",
                }.get(node.status, "❓")

                duration = ""
                if node.started_at and node.completed_at:
                    duration = f" ({round(node.completed_at - node.started_at, 2)}s)"

                yield (
                    f"{agent_icon} {status_icon} **{node.id}**: "
                    f"{node.agent or 'system'}/{node.action}{duration}\n"
                )

                if node.error:
                    yield f"   ⚠️ {node.error}\n"

            yield "─" * 40 + "\n"

            # B-02: Emitir evento task_graph con estados reales post-ejecucion
            final_nodes = []
            for node in result.nodes:
                frontend_status = {
                    "SUCCESS": "done",
                    "FAILED": "error",
                    "RUNNING": "running",
                    "AWAITING_APPROVAL": "pending",
                    "SKIPPED": "done",
                }.get(node.status, "pending")
                duration_ms = 0
                if node.started_at and node.completed_at:
                    duration_ms = int((node.completed_at - node.started_at) * 1000)
                final_nodes.append({
                    "id": node.id,
                    "agent": node.agent,
                    "action": node.action,
                    "token_budget": node.token_budget,
                    "status": frontend_status,
                    "tokens_used": 0,
                    "duration_ms": duration_ms,
                    "error": node.error,
                })
            yield {
                "__event__": "task_graph",
                "graph_id": task_id,
                "nodes": final_nodes,
            }

            # --- Manejo de aprobacion ---
            if result.awaiting_approval:
                approval_node = next(
                    (n for n in result.nodes if n.id == result.approval_node_id), None
                )
                approval_id = create_approval(
                    agent_id=self.agent_name,
                    tool="execute_task_graph",
                    params={
                        "task_id": task_id,
                        "task_type": graph.task_type,
                        "description": graph.description,
                        "node_count": len(graph.nodes),
                        "graph": graph_definition,
                        "awaiting_node": result.approval_node_id,
                    },
                    reason=(
                        f"Task Graph para: {graph.description[:200]}. "
                        f"Nodo '{result.approval_node_id}' ({approval_node.agent if approval_node else '?'}/"
                        f"{approval_node.action if approval_node else '?'}) requiere aprobación."
                    ),
                )

                # Emitir evento SSE approval_request para el frontend
                yield {
                    "__event__": "approval_request",
                    "graph_id": task_id,
                    "approval_id": str(approval_id),
                    "agent": self.agent_name,
                    "action": "execute_task_graph",
                    "description": graph.description,
                    "risk": "medium",
                    "files": [n.id for n in graph.nodes],
                    "node": result.approval_node_id,
                }

                yield f"\n🔐 **Ejecución pausada - requiere aprobación**\n"
                yield f"   Approval ID: `{approval_id}`\n"
                yield f"   Nodo bloqueado: `{result.approval_node_id}`\n"
                yield f"   Usa el pop-up de aprobación en la UI para continuar.\n"

            elif result.success:
                yield "\n✅ **Task Graph completado exitosamente**\n"
                # D-01: Emitir artefactos enriquecidos al frontend
                for node in result.nodes:
                    if node.status == "SUCCESS" and node.result:
                        try:
                            import json
                            node_data = json.loads(node.result) if isinstance(node.result, str) else node.result
                            if isinstance(node_data, dict):
                                # Determinar tipo de artifact
                                if node.action == "plan_diff" and node_data.get("file"):
                                    # Extraer diff stats del DiffProposal
                                    title = node_data.get("file", "").split("/")[-1]
                                    search = node_data.get("search_block", "")
                                    replace = node_data.get("replace_block", "")
                                    search_lines = len(search.split("\n")) if search else 0
                                    replace_lines = len(replace.split("\n")) if replace else 0
                                    yield {
                                        "__event__": "artifact",
                                        "kind": "code",
                                        "title": title,
                                        "path": node_data.get("file", ""),
                                        "content": replace if replace else search,
                                        "action": node.action,
                                        "explanation": node_data.get("explanation", "")[:200],
                                        "risk": node_data.get("estimated_risk", "low"),
                                        "stats": {
                                            "lines_added": max(0, replace_lines - search_lines),
                                            "lines_removed": max(0, search_lines - replace_lines),
                                            "total_lines": replace_lines,
                                        },
                                    }
                                elif node.action == "apply_diff":
                                    pass  # Redundante - plan_diff ya muestra el código
                                elif node.action in ("visual_audit", "screenshot_report"):
                                    # F-01: Emitir artefacto de auditoría visual
                                    findings = node_data.get("findings", [])
                                    screenshot = node_data.get("screenshot", "")
                                    console = node_data.get("console_errors", [])
                                    yield {
                                        "__event__": "artifact",
                                        "kind": "audit",
                                        "title": "Auditoría Visual",
                                        "path": screenshot or "",
                                        "content": "\n".join(findings) if findings else "Sin hallazgos",
                                        "action": node.action,
                                        "explanation": f"Playwright + NIM vision. {len(findings)} hallazgos, {len(console)} errores de consola.",
                                        "risk": "low",
                                        "stats": {
                                            "findings": len(findings),
                                            "console_errors": len(console),
                                            "screenshot": screenshot,
                                        },
                                    }
                                    # También emitir texto para visibilidad inmediata
                                    yield f"\n📸 **Auditoría visual completada**\n"
                                    if screenshot:
                                        yield f"   Screenshot: `{screenshot}`\n"
                                    if findings:
                                        yield f"   Hallazgos ({len(findings)}):\n"
                                        for f in findings[:10]:
                                            yield f"     • {f}\n"
                                    if console:
                                        yield f"   Consola ({len(console)}):\n"
                                        for c in console[:5]:
                                            yield f"     • {c}\n"
                        except: pass
            else:
                yield f"\n❌ **Task Graph falló**: {result.error or 'Error desconocido'}\n"

            update_ultima_accion(
                f"Task Graph ejecutado | task_id={task_id} | "
                f"success={result.success} | awaiting_approval={result.awaiting_approval} | "
                f"nodes={len(result.nodes)}"
            )

        except Exception as e:
            yield f"❌ Error en ejecución de Task Graph: {e}\n"
            yield "↩️ Cayendo a modo simple...\n"
            async for chunk in self._respond_simple(message, history, task_id):
                yield chunk

    async def _build_task_graph(self, message: str, task_id: str, escalate: bool = False) -> TaskGraph:
        role = "orchestrator_escalation" if escalate else "orchestrator_lead"
        response = await self.router.complete(
            role=role,
            messages=[{"role": "user", "content": message[:2000]}],
            system=_TASK_GRAPH_SYSTEM_PROMPT,
            agent_name=self.agent_name,
            task_id=task_id,
            override_max_tokens=4096,
        )

        raw = response.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()

        # Parsing robusto para JSON de Task Graph
        match = re.search(r'{.*}', raw, re.DOTALL)
        if not match:
            raise ValueError("No se encontró JSON en respuesta del LLM")

        json_str = match.group(0)
        # Fix unquoted keys pero NUNCA reemplazar comillas en valores
        json_str = re.sub(r'([{,]\s*)(\w+)(\s*:)', r'\1"\2"\3', json_str)
        json_str = re.sub(r'(^\s*)(\w+)(\s*:)', r'\1"\2"\3', json_str)

        data = json.loads(json_str)

        required = {"task_type", "description", "nodes"}
        missing = required - set(data.keys())
        if missing:
            raise ValueError(f"Campos faltantes en Task Graph: {missing}")

        nodes = []
        for n in data.get("nodes", []):
            node = TaskGraphNode(
                id=n.get("id", str(uuid.uuid4())[:8]),
                agent=n.get("agent"),
                action=n.get("action", ""),
                token_budget=n.get("token_budget", 1000),
                next=n.get("next", []),
                ask_approval=n.get("ask_approval", False),
                description=n.get("description", ""),
            )
            nodes.append(node)

        graph = TaskGraph(
            task_id=task_id,
            task_type=data.get("task_type", "general"),
            description=data.get("description", ""),
            nodes=nodes,
            created_at=time.time(),
        )

        return graph

    async def council_decide(
        self,
        question: str,
        task_id: str = "",
    ) -> dict[str, Any]:
        try:
            result = await self.router.council(
                roles=["orchestrator_lead", "orchestrator_council"],
                messages=[{"role": "user", "content": question}],
                system=_IDENTITY_LOCK + "\n\nMODO: Council - evalúa una decisión. Sé analítico y conciso.",
                agent_name=self.agent_name,
                task_id=task_id or str(uuid.uuid4()),
            )

            output = {
                "diverged": result.diverged,
                "divergence_score": result.divergence_score,
                "consensus": result.consensus,
                "responses": [
                    {
                        "provider": r.provider,
                        "model": r.model,
                        "content": r.content,
                    }
                    for r in result.responses
                ],
            }

            update_ultima_accion(
                f"Council mode ejecutado | task_id={task_id} | "
                f"diverged={result.diverged} | score={result.divergence_score:.2f}"
            )

            return output

        except Exception as e:
            error_msg = f"Error en council mode: {e}"
            print(f"[Orchestrator] {error_msg}")
            return {
                "diverged": False,
                "divergence_score": 0.0,
                "consensus": None,
                "error": error_msg,
                "responses": [],
            }

    async def _review_plan(
        self,
        proposal: dict[str, Any],
        task_id: str = "",
    ) -> dict[str, Any]:
        """
        Review Mode (TD-19): Lead + Council auditan un DiffProposal
        antes de aplicarlo.

        Flujo:
        1. Formatea la propuesta como pregunta para council
        2. Ambos modelos evalúan en paralelo
        3. Si convergen y aprueban → procede
        4. Si convergen y rechazan → feedback para regenerar
        5. Si divergen → revisión humana

        Returns:
            {
                "approved": bool,
                "feedback": str | None,
                "needs_human": bool,
                "diverged": bool,
                "council_result": dict,
            }
        """
        # Formatear la propuesta para el council
        question = (
            f"Evalúa esta propuesta de cambio de código:\n\n"
            f"Archivo: {proposal.get('file', '?')}\n"
            f"Riesgo estimado: {proposal.get('estimated_risk', '?')}\n"
            f"Explicación: {proposal.get('explanation', '?')}\n\n"
            f"--- BLOQUE A REEMPLAZAR ---\n"
            f"{proposal.get('search_block', '?')}\n\n"
            f"--- BLOQUE NUEVO ---\n"
            f"{proposal.get('replace_block', '?')}\n\n"
            f"Responde SOLO con una de estas palabras: "
            f"APPROVED, REJECTED, o NEEDS_HUMAN. "
            f"Luego, en una línea separada, explica brevemente por qué."
        )

        council_result = await self.council_decide(
            question=question,
            task_id=task_id or str(uuid.uuid4()),
        )

        # Analizar las respuestas del council
        approved_count = 0
        rejected_count = 0
        feedback_parts = []

        for r in council_result.get("responses", []):
            content = (r.get("content") or "").strip().upper()
            provider = r.get("provider", "?")

            if "APPROVED" in content:
                approved_count += 1
                feedback_parts.append(f"[{provider}] ✅ Aprueba")
            elif "REJECTED" in content:
                rejected_count += 1
                feedback_parts.append(f"[{provider}] ❌ Rechaza")
            elif "NEEDS_HUMAN" in content:
                feedback_parts.append(f"[{provider}] ⚠️ Requiere revisión humana")
            else:
                # Respuesta ambigua → contar como rechazo suave
                rejected_count += 1
                feedback_parts.append(
                    f"[{provider}] ❓ Ambiguo: {content[:100]}"
                )

        total = len(council_result.get("responses", []))
        feedback = "\n".join(feedback_parts) if feedback_parts else None

        # Decisión basada en keywords (APPROVED/REJECTED), no en Jaccard textual.
        # El council puede "divergir" en wording aunque estén de acuerdo semánticamente.
        if approved_count >= total and total > 0:
            # Todos aprobaron → consenso claro
            return {
                "approved": True,
                "feedback": feedback,
                "needs_human": False,
                "diverged": False,
                "council_result": council_result,
            }
        elif rejected_count >= total and total > 0:
            # Todos rechazaron → consenso claro, regenerar
            return {
                "approved": False,
                "feedback": feedback,
                "needs_human": False,
                "diverged": False,
                "council_result": council_result,
            }
        elif approved_count > rejected_count:
            # Mayoría aprueba
            return {
                "approved": True,
                "feedback": feedback,
                "needs_human": False,
                "diverged": False,
                "council_result": council_result,
            }
        elif rejected_count > approved_count:
            # Mayoría rechaza
            return {
                "approved": False,
                "feedback": feedback,
                "needs_human": False,
                "diverged": False,
                "council_result": council_result,
            }
        else:
            # Sin respuestas claras → revisión humana
            return {
                "approved": False,
                "feedback": feedback,
                "needs_human": True,
                "diverged": council_result.get("diverged", False),
                "council_result": council_result,
            }

    def get_status(self) -> dict[str, Any]:
        circuit_status = self.router.get_circuit_status()

        try:
            state = read_state()
        except Exception:
            state = {}

        try:
            import yaml
            routing_path = Path(__file__).parent.parent.parent / "model_routing.yaml"
            with open(routing_path, "r", encoding="utf-8") as f:
                routing = yaml.safe_load(f)
            models = {
                "lead": routing.get("roles", {}).get("orchestrator_lead", "unknown"),
                "council": routing.get("roles", {}).get("orchestrator_council", "unknown"),
                "escalation": routing.get("roles", {}).get("orchestrator_escalation", "unknown"),
            }
            backend = routing.get("default_provider", "unknown")
        except Exception:
            models = {"lead": "unknown", "council": "unknown", "escalation": "unknown"}
            backend = "unknown"

        try:
            pending = list_approvals(status="pending")
            pending_count = len(pending)
        except Exception:
            pending_count = 0

        return {
            "agent": self.agent_name,
            "status": "operativo",
            "orchestrator_backend": backend,
            "circuit_breakers": circuit_status,
            "approvals_pending": pending_count,
            "proyecto_activo": state.get("proyecto_activo", "N/A"),
            "ultima_accion": state.get("ultima_accion_real", ""),
            "models": models,
        }


if __name__ == "__main__":
    import asyncio

    async def test():
        orchestrator = Orchestrator()
        print("=== Orchestrator Smoke Test ===")

        print("\n--- get_status ---")
        status = orchestrator.get_status()
        print(f"Status: {status['status']}")
        print(f"Backend: {status['orchestrator_backend']}")
        print(f"Models: {status['models']}")
        print(f"Approvals pending: {status['approvals_pending']}")

        print("\n--- validate_graph (válido mínimo) ---")
        valid_graph = TaskGraph(
            task_id="test_001",
            task_type="general",
            description="Test graph",
            nodes=[
                TaskGraphNode(
                    id="node_001",
                    agent="Curator",
                    action="inject_project_context",
                    token_budget=1000,
                    next=["node_002"],
                ),
                TaskGraphNode(
                    id="node_002",
                    agent="Planner",
                    action="plan_diff",
                    token_budget=2000,
                    next=["__end__"],
                    ask_approval=True,
                ),
            ],
        )
        is_valid, errors = validate_graph(valid_graph)
        print(f"Válido: {is_valid}")
        if errors:
            print(f"Errores: {errors}")

        # _classify eliminado (B4 cleanup) — el sistema usa Task Graph

        print("\n=== Smoke Test Completo ===")

    asyncio.run(test())
