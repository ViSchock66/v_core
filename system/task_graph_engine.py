"""
system/task_graph_engine.py
===========================
Task Graph Engine — V-CORE v1.1

Ejecuta los nodos del Task Graph que Orchestrator genera y valida.
Cada nodo se ejecuta en orden segun sus dependencias (DAG).

Flujo:
  1. Recibe un TaskGraph validado
  2. Inicia con el nodo Curator/context (hook obligatorio)
  3. Ejecuta cada nodo segun su tipo de agente
  4. Pausa en nodos con ask_approval=true
  5. Persiste todo en agent_execution
  6. Soporta reanudacion via resume_graph()

Uso:
    from system.task_graph_engine import TaskGraphEngine
    engine = TaskGraphEngine()
    result = await engine.execute_graph(task_id, graph_definition)
    # Si pauso en approval:
    result = await engine.resume_graph(task_id)
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel


# =============================================================================
# CONSTANTES
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent.parent  # V-CORE/
DB_PATH = BASE_DIR / "vcore.db"
WORKSPACE_DIR = BASE_DIR / "workspace"

NODE_TIMEOUT = 60  # timeout por nodo en segundos
NODE_TIMEOUT_MAP = {
    "plan_diff": 120,      # Planner plan_diff usa LLM → necesita más tiempo
    "visual_audit": 120,    # Playwright + NIM vision puede tardar
    "web_search": 30,      # Búsqueda web externa
    "execute_command": 30, # Comandos shell con timeout
    "patch_file": 10,      # Find-and-replace local
    "apply_diff": 60,
    "shadow_verify": 30,
    "validate_diffs": 30,
    "inject_project_context": 10,
    "search": 45,
}
CLEANUP_INTERVAL = 300  # intervalo de limpieza en segundos (5 min)


# =============================================================================
# SCHEMAS
# =============================================================================

class ExecutionNode(BaseModel):
    """Nodo individual en ejecucion. Compatible con TaskGraphNode de Orchestrator."""
    id: str
    agent: Optional[str] = None
    action: str
    token_budget: int = 1000
    next: list[str] = []
    ask_approval: bool = False
    decision_type: Optional[str] = None
    status: str = "PENDING"  # PENDING | RUNNING | SUCCESS | FAILED | SKIPPED | AWAITING_APPROVAL
    result: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


class ExecutionResult(BaseModel):
    """Resultado completo de la ejecucion de un grafo."""
    task_id: str
    success: bool
    nodes: list[ExecutionNode]
    context: Optional[dict] = None
    error: Optional[str] = None
    started_at: float = 0.0
    completed_at: float = 0.0
    awaiting_approval: bool = False
    approval_node_id: Optional[str] = None


# =============================================================================
# TASK GRAPH ENGINE
# =============================================================================

class TaskGraphEngine:
    """
    Motor de ejecucion de Task Graphs.

    Responsabilidades:
      - Ejecutar nodos en orden DAG
      - Inyectar contexto Curator primero (hook obligatorio)
      - Rutear a Planner/Orchestrator segun el nodo
      - Pausar en ask_approval
      - Persistir todo en agent_execution
      - startup_recovery en inicio
      - Reanudar ejecucion tras aprobacion humana
    """

    def __init__(self):
        self._curator = None
        self._planner = None
        self._retriever = None

    # ------------------------------------------------------------------
    # Lazy imports de agentes (evitar circular imports)
    # ------------------------------------------------------------------

    @property
    def curator(self):
        if self._curator is None:
            from agents.Curator.curator import Curator
            self._curator = Curator()
        return self._curator

    @property
    def planner(self):
        if self._planner is None:
            from agents.Planner.planner import Planner
            self._planner = Planner()
        return self._planner

    @property
    def retriever(self):
        if self._retriever is None:
            from agents.Retriever.retriever import Retriever
            self._retriever = Retriever()
        return self._retriever

    # ------------------------------------------------------------------
    # Persistencia del grafo
    # ------------------------------------------------------------------

    def _ensure_task_graphs_table(self) -> None:
        """Crea la tabla task_graphs si no existe."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_graphs (
                    id TEXT PRIMARY KEY,
                    status TEXT DEFAULT 'PENDING',
                    definition_json TEXT,
                    context_json TEXT,
                    created_at REAL,
                    updated_at REAL
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[TaskGraphEngine] Error creando tabla task_graphs: {e}")

    def _save_graph_definition(self, task_id: str, definition: dict, context: dict) -> None:
        """Inserta o actualiza la definicion del grafo en DB."""
        self._ensure_task_graphs_table()
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                INSERT INTO task_graphs (id, status, definition_json, context_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    definition_json = excluded.definition_json,
                    context_json = excluded.context_json,
                    updated_at = excluded.updated_at
            """, (
                task_id,
                "IN_PROGRESS",
                json.dumps(definition, ensure_ascii=False),
                json.dumps(context, ensure_ascii=False),
                time.time(),
                time.time(),
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[TaskGraphEngine] Error guardando grafo: {e}")

    def _load_graph_definition(self, task_id: str) -> tuple[Optional[dict], Optional[dict]]:
        """Recupera definicion y contexto del grafo desde DB."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT definition_json, context_json FROM task_graphs WHERE id = ?",
                (task_id,)
            )
            row = cur.fetchone()
            conn.close()
            if row:
                defn = json.loads(row["definition_json"]) if row["definition_json"] else {}
                ctx = json.loads(row["context_json"]) if row["context_json"] else {}
                return defn, ctx
        except Exception as e:
            print(f"[TaskGraphEngine] Error cargando grafo: {e}")
        return None, None

    # ------------------------------------------------------------------
    # Ejecucion principal
    # ------------------------------------------------------------------

    async def execute_graph(
        self,
        task_id: str,
        graph_definition: dict,
        auto_approve: bool = False,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ) -> ExecutionResult:
        """
        Ejecuta un Task Graph completo.

        Args:
            task_id: ID de la tarea
            graph_definition: Dict del grafo (task_type, description, nodes)
            auto_approve: Si True, salta approvals automaticamente
            progress_callback: Funcion opcional llamada en cada cambio de nodo

        Returns:
            ExecutionResult con el estado de cada nodo
        """
        result = ExecutionResult(
            task_id=task_id,
            success=False,
            nodes=[],
            started_at=time.time(),
        )

        # Contexto acumulativo compartido entre nodos
        shared_context: dict[str, Any] = {}
        shared_context["task_description"] = graph_definition.get("description", task_id)
        shared_context.setdefault("completed_node_ids", [])

        try:
            # Construir nodos desde la definicion
            nodes = []
            node_map: dict[str, ExecutionNode] = {}
            for n in graph_definition.get("nodes", []):
                node = ExecutionNode(
                    id=n.get("id", str(uuid.uuid4())[:8]),
                    agent=n.get("agent"),
                    action=n.get("action", ""),
                    token_budget=n.get("token_budget", 1000),
                    next=[x for x in n.get("next", []) if x != "__end__"],
                    ask_approval=n.get("ask_approval", False),
                    status="SUCCESS" if n.get("_resume_completed") else "PENDING",
                )
                nodes.append(node)
                node_map[node.id] = node

            result.nodes = nodes

            # Persistir grafo y contexto inicial
            self._save_graph_definition(task_id, graph_definition, shared_context)
            self._update_graph_status(task_id, "IN_PROGRESS")

            if progress_callback:
                progress_callback({"type": "start", "task_id": task_id, "node_count": len(nodes)})

            # Verificar hook Curator
            entry = self._find_entry_node(nodes)
            if entry and entry.agent == "Curator" and entry.action == "inject_project_context":
                await self._execute_node(entry, task_id, shared_context, progress_callback)
                if entry.status == "SUCCESS" and entry.result:
                    try:
                        shared_context["curator_context"] = entry.result
                    except Exception:
                        shared_context["curator_context"] = str(entry.result)

            # Ejecutar en topologico con ordenamiento DAG
            execution_order = self._topological_sort(nodes)
            for node_id in execution_order:
                node = node_map[node_id]

                # C-01: Saltar nodos ya completados (resume)
                if node.status == "SUCCESS":
                    if progress_callback:
                        progress_callback({"type": "node_skip", "node_id": node.id, "reason": "already_completed"})
                    continue

                # Saltar Curator ya ejecutado
                if node.agent == "Curator" and node.action == "inject_project_context":
                    if node.status != "SUCCESS":
                        node.status = "SUCCESS"
                        self._log_execution(node, task_id)
                    if progress_callback:
                        progress_callback({"type": "node_skip", "node_id": node.id, "reason": "already_executed"})
                    continue

                # Si requiere approval y no esta auto-aprobado, pausar
                if node.ask_approval and not auto_approve:
                    node.status = "AWAITING_APPROVAL"
                    self._log_execution(node, task_id)
                    self._update_graph_status(task_id, "AWAITING_APPROVAL")
                    self._save_graph_definition(task_id, graph_definition, shared_context)

                    result.awaiting_approval = True
                    result.approval_node_id = node.id

                    if progress_callback:
                        progress_callback({
                            "type": "approval_required",
                            "task_id": task_id,
                            "node_id": node.id,
                            "agent": node.agent,
                            "action": node.action,
                        })
                    break  # Pausar ejecucion

                # Ejecutar el nodo
                await self._execute_node(node, task_id, shared_context, progress_callback)

                # Si fallo, detener (fail-fast)
                if node.status == "FAILED":
                    if progress_callback:
                        progress_callback({"type": "node_failed", "node_id": node.id, "error": node.error})
                    break

            # Verificar resultado
            executed_nodes = [n for n in nodes if n.status != "PENDING"]
            all_success = all(
                n.status in ("SUCCESS", "AWAITING_APPROVAL")
                for n in executed_nodes
            )
            result.success = all_success and not result.awaiting_approval
            result.context = shared_context

            if result.awaiting_approval:
                final_status = "AWAITING_APPROVAL"
            elif result.success:
                final_status = "DONE"
            else:
                final_status = "FAILED"
            self._update_graph_status(task_id, final_status)

            if progress_callback:
                progress_callback({"type": "complete", "task_id": task_id, "status": final_status})

        except Exception as e:
            result.success = False
            result.error = f"{type(e).__name__}: {e}"
            self._update_graph_status(task_id, f"FAILED_{type(e).__name__}")
            if progress_callback:
                progress_callback({"type": "error", "task_id": task_id, "error": result.error})

        finally:
            result.completed_at = time.time()
            self._update_ultima_accion(
                f"Task Graph ejecutado | task_id={task_id} | "
                f"success={result.success} | awaiting_approval={result.awaiting_approval} | "
                f"nodes={len(result.nodes)}"
            )

        return result

    async def resume_graph(
        self,
        task_id: str,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ) -> ExecutionResult:
        """
        Reanuda un grafo que estaba en AWAITING_APPROVAL.
        Carga la definicion y contexto desde DB, respeta completed_node_ids.
        """
        definition, prev_context = self._load_graph_definition(task_id)
        if not definition:
            return ExecutionResult(
                task_id=task_id,
                success=False,
                nodes=[],
                error=f"No se encontro grafo para task_id={task_id}",
            )

        # C-01: Recuperar nodos ya completados del contexto anterior
        completed_ids = prev_context.get("completed_node_ids", []) if prev_context else []

        # Marcar nodos ya completados en la definicion
        for n in definition.get("nodes", []):
            if n.get("ask_approval"):
                n["ask_approval"] = False  # Aprobado
            if n.get("id") in completed_ids:
                n["_resume_completed"] = True  # Saltar en ejecucion

        return await self.execute_graph(
            task_id=task_id,
            graph_definition=definition,
            auto_approve=True,
            progress_callback=progress_callback,
        )

    async def _execute_node(
        self,
        node: ExecutionNode,
        task_id: str,
        shared_context: dict,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ) -> None:
        """
        Ejecuta un nodo individual segun su agente/accion.
        """
        node.status = "RUNNING"
        node.started_at = time.time()

        if progress_callback:
            progress_callback({
                "type": "node_start",
                "node_id": node.id,
                "agent": node.agent,
                "action": node.action,
            })

        try:
            agent = (node.agent or "").upper()
            action = node.action
            timeout = NODE_TIMEOUT_MAP.get(action, NODE_TIMEOUT)
            async with asyncio.timeout(timeout):
                curator_ctx = shared_context.get("curator_context", "")

                # --- Curator ---
                if agent == "Curator":
                    if action == "inject_project_context":
                        ctx = self.curator.inject_project_context()
                        node.result = ctx.model_dump_json() if hasattr(ctx, "model_dump_json") else json.dumps(ctx)
                    elif action == "get_architecture":
                        node.result = self.curator.get_architecture()
                    elif action == "get_recent_work":
                        work = self.curator.get_recent_work()
                        node.result = json.dumps(work)
                    elif action == "summarize_file":
                        node.result = self.curator.summarize_file(curator_ctx or ".")
                    elif action == "estimate_repo_size":
                        size = self.curator.estimate_repo_size()
                        node.result = json.dumps({"size_kb": size})
                    else:
                        node.result = f"Curator action desconocida: {action}"

                # --- Planner ---
                elif agent == "Planner":
                    if action == "plan_diff":
                        proposal = await self.planner.plan_diff(
                            context=curator_ctx or "Contexto de archivo",
                            task=shared_context.get("task_description", task_id),
                        )
                        node.result = proposal.model_dump_json() if hasattr(proposal, "model_dump_json") else json.dumps(proposal)
                        shared_context["diff_proposal"] = node.result
                    elif action == "apply_diff":
                        # Recuperar diff proposal del contexto y deserializar
                        diff_json = shared_context.get("diff_proposal")
                        if diff_json:
                            from agents.Planner.planner import DiffProposal
                            diff_data = json.loads(diff_json) if isinstance(diff_json, str) else diff_json
                            proposal = DiffProposal(**diff_data)
                            try:
                                self.planner.apply_diff(proposal)
                                node.result = json.dumps({"status": "applied", "file": proposal.file})
                                shared_context["apply_ok"] = True
                            except ValueError as ve:
                                node.result = json.dumps({"status": "invalid_proposal", "error": str(ve)})
                                node.status = "FAILED"
                                node.error = str(ve)
                        else:
                            node.result = json.dumps({"status": "skipped", "note": "Sin DiffProposal previo"})
                    elif action == "shadow_verify":
                        # Verificar el archivo del diff proposal, no el workspace entero
                        diff_json = shared_context.get("diff_proposal", "{}")
                        diff_data = json.loads(diff_json) if isinstance(diff_json, str) else diff_json
                        target_file = diff_data.get("file", ".")
                        vr = self.planner.shadow_verify(target_file)
                        node.result = vr.model_dump_json() if hasattr(vr, "model_dump_json") else json.dumps(vr)
                        if vr.passed:
                            shared_context["verify_ok"] = True
                    elif action == "patch_file":
                        # H-03: Edición precisa find-and-replace (como Hermes patch)
                        target = shared_context.get("patch_target", "")
                        old_str = shared_context.get("patch_old", "")
                        new_str = shared_context.get("patch_new", "")
                        node.result = self._patch_file(target, old_str, new_str)
                    else:
                        node.result = f"Planner action desconocida: {action}"

                # --- Orchestrator ---
                elif agent == "Orchestrator":
                    if action == "validate_diffs":
                        # E-03: Auditoría real — verifica diff, apply y verify
                        diff_json = shared_context.get("diff_proposal")
                        diff_present = diff_json is not None
                        apply_ok = shared_context.get("apply_ok", False)
                        verify_ok = shared_context.get("verify_ok", False)
                        issues = []

                        if not diff_present:
                            issues.append("No se encontro DiffProposal en el contexto")
                        if not apply_ok:
                            issues.append("apply_diff no fue exitoso")
                        if not verify_ok:
                            issues.append("shadow_verify no paso")

                        validated = diff_present and apply_ok and verify_ok
                        node.result = json.dumps({
                            "validated": validated,
                            "diff_present": diff_present,
                            "apply_ok": apply_ok,
                            "verify_ok": verify_ok,
                            "issues": issues,
                        })
                        if not validated:
                            node.status = "FAILED"
                            node.error = "; ".join(issues)
                    elif action == "visual_audit":
                        # F-01: Auditoría visual — Playwright sync en thread
                        import asyncio as _asyncio, traceback as _tb, sys as _sys
                        try:
                            node.result = await _asyncio.to_thread(self._run_visual_audit_sync, curator_ctx or "")
                        except Exception as _ex:
                            _tb.print_exc(file=_sys.stderr)
                            node.result = json.dumps({"error": str(_ex), "status": "failed"})
                            node.status = "FAILED"
                            node.error = str(_ex)[:200]
                    elif action == "screenshot_report":
                        import asyncio as _asyncio
                        node.result = await _asyncio.to_thread(self._run_visual_audit_sync, curator_ctx or "", False)
                    elif action == "web_search":
                        # H-01: Búsqueda web — DuckDuckGo HTML (sin API key)
                        node.result = await self._web_search(curator_ctx or "")
                    elif action == "execute_command":
                        # H-02: Ejecutar comando shell (con timeout y safety)
                        cmd = shared_context.get("shell_command", curator_ctx or "")
                        node.result = await self._execute_command(cmd)
                    else:
                        node.result = f"Orchestrator action desconocida: {action}"

                # --- Retriever ---
                elif agent == "Retriever":
                    if action == "search":
                        # E-04: Búsqueda real en ChromaDB via Retriever
                        query = curator_ctx[:500] if curator_ctx else shared_context.get("task_description", "")
                        try:
                            results = await self.retriever.search(query, n=5)
                            node.result = json.dumps({
                                "query": query[:200],
                                "results_count": len(results),
                                "results": results[:5],
                            })
                        except Exception as e:
                            node.result = json.dumps({
                                "query": query[:200],
                                "error": f"Retriever.search falló: {e}",
                                "fallback": True,
                            })
                    elif action == "get_impact_map":
                        impact = self.retriever.get_impact_map(".")
                        node.result = json.dumps(impact)
                    else:
                        node.result = f"Retriever action desconocida: {action}"

                # --- Sistema (return_to_user) ---
                elif action == "return_to_user":
                    node.result = json.dumps({"status": "returned_to_user", "context": curator_ctx[:500]})
                else:
                    node.result = f"Agente desconocido: {agent}"

                node.status = "SUCCESS"
                shared_context["completed_node_ids"].append(node.id)  # C-01
                if progress_callback:
                    progress_callback({
                        "type": "node_success",
                        "node_id": node.id,
                        "agent": node.agent,
                        "action": node.action,
                        "duration_ms": round((node.completed_at or time.time()) - node.started_at, 3) * 1000 if node.started_at else 0,
                    })

        except asyncio.TimeoutError:
            node.status = "FAILED"
            node.error = f"TIMEOUT ({NODE_TIMEOUT}s)"
            if progress_callback:
                progress_callback({"type": "node_timeout", "node_id": node.id, "timeout": NODE_TIMEOUT})
        except Exception as e:
            node.status = "FAILED"
            node.error = f"{type(e).__name__}: {str(e)[:200]}"
            if progress_callback:
                progress_callback({"type": "node_error", "node_id": node.id, "error": node.error})
        finally:
            node.completed_at = time.time()
            self._log_execution(node, task_id)

    # ------------------------------------------------------------------
    # startup_recovery
    # ------------------------------------------------------------------

    def startup_recovery(self) -> list[dict]:
        """
        Recovery al iniciar el sistema.
        Marca grafos IN_PROGRESS como FAILED_UNCLEAN_SHUTDOWN.
        Marca agent_execution incompletos como INTERRUPTED.

        Returns:
            Lista de tareas recuperadas
        """
        recovered = []
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # 1. Integrity check
            cur.execute("PRAGMA integrity_check")
            integrity = cur.fetchone()
            if integrity and integrity[0] != "ok":
                print(f"[TaskGraphEngine] Integrity check: {integrity[0]}")

            # 2. Marcar grafos colgados
            cur.execute(
                "SELECT id FROM task_graphs WHERE status = 'IN_PROGRESS'"
            )
            for row in cur.fetchall():
                gid = row["id"]
                cur.execute(
                    "UPDATE task_graphs SET status = 'FAILED_UNCLEAN_SHUTDOWN', "
                    "updated_at = ? WHERE id = ?",
                    (time.time(), gid),
                )
                recovered.append({
                    "task_id": gid,
                    "type": "graph",
                    "new_status": "FAILED_UNCLEAN_SHUTDOWN",
                })
                print(f"[TaskGraphEngine] Recovery: {gid} -> FAILED_UNCLEAN_SHUTDOWN")

            # 3. Marcar ejecuciones colgadas
            cur.execute(
                "SELECT id, task_id FROM agent_execution WHERE status IN ('RUNNING', 'PENDING')"
            )
            for row in cur.fetchall():
                cur.execute(
                    "UPDATE agent_execution SET status = 'INTERRUPTED', "
                    "error_msg = 'startup_recovery' WHERE id = ?",
                    (row["id"],),
                )
                recovered.append({
                    "task_id": row["task_id"],
                    "type": "execution",
                    "new_status": "INTERRUPTED",
                })

            conn.commit()
            conn.close()

        except Exception as e:
            print(f"[TaskGraphEngine] Error en startup_recovery: {e}")

        return recovered

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------

    async def _run_visual_audit(self, instructions: str = "", analyze: bool = True) -> str:
        """Auditoría visual — versión async para el loop de Orchestrator.

        Los comandos `/audit`, `/viz` y el polish loop llamaban a este método y no
        existía (AttributeError). Se delega a la versión síncrona en un hilo,
        porque Playwright sync no convive con el event loop de uvicorn.
        """
        import asyncio as _asyncio
        return await _asyncio.to_thread(self._run_visual_audit_sync, instructions, analyze)

    def _run_visual_audit_sync(self, instructions: str = "", analyze: bool = True) -> str:
        """Auditoría visual del frontend — versión síncrona (para `asyncio.to_thread`).

        Este método NO existía: el motor de grafos lo llamaba en las acciones
        `visual_audit` y `screenshot_report`, y cualquier grafo con esos nodos
        fallaba con AttributeError. La implementación real ya estaba escrita en
        `system/visual_auditor.py` pero nadie la importaba: era código huérfano
        mientras el motor invocaba un método fantasma.

        Se ejecuta en un hilo aparte porque Playwright sync no puede convivir con
        el event loop de uvicorn (ver el docstring de `visual_auditor`).
        """
        from system.visual_auditor import run_visual_audit

        try:
            report = run_visual_audit(
                analyze=analyze,
                instructions=instructions or None,
                task_id=getattr(self, "current_task_id", "") or "",
            )
        except Exception as e:
            return json.dumps({"status": "failed", "error": f"{type(e).__name__}: {e}"})

        # Salida legible para el agente: un JSON crudo de 200 líneas no le sirve
        # como observación. Se resume y se deja el reporte completo aparte.
        if isinstance(report, dict) and report.get("status") == "unavailable":
            return f"Auditoría visual no disponible: {report.get('error', 'Playwright ausente')}"
        return json.dumps(report, ensure_ascii=False)[:4000]

    async def _web_search(self, query: str) -> str:
        """H-01: Búsqueda web usando DuckDuckGo Instant Answer API (sin API key)."""
        import urllib.request, urllib.parse
        try:
            q = urllib.parse.quote(query[:200])
            url = f"https://api.duckduckgo.com/?q={q}&format=json&no_html=1&skip_disambig=1"
            req = urllib.request.Request(url, headers={"User-Agent": "V-Core/0.8 Orchestrator"})
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            
            results = []
            
            # Resultado principal (Abstract)
            if data.get("AbstractText"):
                results.append({
                    "title": data.get("Heading", query),
                    "url": data.get("AbstractURL", ""),
                    "snippet": data["AbstractText"][:500],
                    "source": data.get("AbstractSource", "duckduckgo"),
                })
            
            # Topics relacionados
            for topic in data.get("RelatedTopics", [])[:5]:
                if "Text" in topic and "FirstURL" in topic:
                    results.append({
                        "title": topic["Text"].split(" - ")[0][:120],
                        "url": topic["FirstURL"],
                        "snippet": topic["Text"][:300],
                    })
            
            # Infobox (datos estructurados) — puede ser string o dict
            infobox = data.get("Infobox", {})
            if isinstance(infobox, dict) and infobox.get("content"):
                for item in infobox["content"][:3]:
                    results.append({
                        "title": item.get("label", ""),
                        "snippet": item.get("value", ""),
                        "url": "",
                    })
            
            return json.dumps({
                "query": query[:200],
                "results": results[:8],
                "source": "duckduckgo_api",
            })
        except Exception as e:
            return json.dumps({"error": str(e), "results": [], "source": "error"})
    
    async def _execute_command(self, command: str) -> str:
        """H-02: Ejecutar comando shell con timeout y safety."""
        import subprocess, shlex
        if not command or not command.strip():
            return json.dumps({"error": "comando vacío", "exit_code": -1})
        # Safety: bloquear comandos peligrosos
        dangerous = ["rm -rf /", "mkfs", "dd if=", ":(){ :|:& };:", "> /dev/sda"]
        cmd_lower = command.lower()
        for d in dangerous:
            if d in cmd_lower:
                return json.dumps({"error": f"Comando bloqueado por safety: contiene '{d}'", "exit_code": -1})
        try:
            proc = asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(BASE_DIR),
            )
            stdout, stderr = asyncio.wait_for(proc.communicate(), timeout=30)
            return json.dumps({
                "exit_code": proc.returncode or 0,
                "stdout": stdout.decode("utf-8", errors="replace")[:2000],
                "stderr": stderr.decode("utf-8", errors="replace")[:1000],
            })
        except asyncio.TimeoutError:
            return json.dumps({"error": "timeout (30s)", "exit_code": -1})
        except Exception as e:
            return json.dumps({"error": str(e), "exit_code": -1})
    
    @staticmethod
    def _patch_file(target: str, old_str: str, new_str: str) -> str:
        """H-03: Find-and-replace preciso en archivo (como Hermes patch)."""
        from pathlib import Path as _Path
        try:
            p = _Path(target)
            if not p.exists():
                return json.dumps({"error": f"Archivo no encontrado: {target}"})
            content = p.read_text(encoding="utf-8")
            if old_str not in content:
                return json.dumps({"error": "old_string no encontrado en el archivo", "matched": False})
            new_content = content.replace(old_str, new_str, 1)
            p.write_text(new_content, encoding="utf-8")
            return json.dumps({"matched": True, "file": str(p), "replaced": True})
        except Exception as e:
            return json.dumps({"error": str(e), "matched": False})
    
    def _log_execution(self, node: ExecutionNode, task_id: str) -> None:
        """Persiste la ejecucion de un nodo en agent_execution."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                INSERT INTO agent_execution
                (task_id, agent_name, node_id, action, tokens_used,
                 duration_s, status, error_msg, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task_id,
                node.agent or "system",
                node.id,
                node.action,
                node.token_budget,
                round((node.completed_at or time.time()) - (node.started_at or time.time()), 2),
                node.status,
                node.error or "",
                time.time(),
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[TaskGraphEngine] Error logeando ejecucion: {e}")

    def _update_graph_status(self, task_id: str, status: str) -> None:
        """Actualiza el estado del grafo en task_graphs."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "UPDATE task_graphs SET status = ?, updated_at = ? WHERE id = ?",
                (status, time.time(), task_id),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass  # El grafo puede no existir aun en DB

    def _update_ultima_accion(self, desc: str) -> None:
        """Actualiza la ultima accion en VCORE_STATE.json."""
        try:
            state_path = BASE_DIR / "VCORE_STATE.json"
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["ultima_accion_real"] = desc
                state_path.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except Exception:
            pass

    @staticmethod
    def _find_entry_node(nodes: list[ExecutionNode]) -> Optional[ExecutionNode]:
        """Encuentra el nodo de entrada (el que nadie referencia en 'next')."""
        all_nexts = set()
        for n in nodes:
            all_nexts.update(n.next)
        for n in nodes:
            if n.id not in all_nexts:
                return n
        return nodes[0] if nodes else None

    @staticmethod
    def _topological_sort(nodes: list[ExecutionNode]) -> list[str]:
        """
        Orden topologico de nodos (Kahn's algorithm).
        Retorna lista de ids en orden de ejecucion.
        """
        adj = {n.id: n.next for n in nodes}
        in_degree = {n.id: 0 for n in nodes}
        for n in nodes:
            for nxt in n.next:
                if nxt in in_degree:
                    in_degree[nxt] += 1

        queue = deque(nid for nid, deg in in_degree.items() if deg == 0)
        result = []

        while queue:
            nid = queue.popleft()
            result.append(nid)
            for nxt in adj.get(nid, []):
                if nxt in in_degree:
                    in_degree[nxt] -= 1
                    if in_degree[nxt] == 0:
                        queue.append(nxt)

        return result