"""
gate.py
-------
Gate de Ejecucion determinístico para V-CORE (Capa 5).

Este modulo NO depende de ningun modelo. Recibe la intencion de
ejecutar una tool (nombre + parametros + agente que la pide) y
devuelve una decision: Nivel A (auto-aprobado) o Nivel B (requiere
aprobacion de Vicente en el frontend).

Principio de fail-safe: cualquier tool desconocida, o cualquier
path que no se pueda resolver dentro de un allowed_root, se trata
como Nivel B. Ante la duda, se pregunta.

No importa lo que Orchestrator "declare" o "crea" haber hecho -- esta
funcion es la unica fuente de verdad sobre el nivel de una accion.
Esto es deliberado: mitiga el riesgo de que un resumen/compactacion
de contexto le haga "olvidar" a un agente que algo requeria Nivel B.

Uso:
    from gate import Gate

    gate = Gate("gate_rules.yaml", db_path="vcore.db")
    decision = gate.evaluate("write_file", {"path": "C:\\...\\workspace\\foo.txt"}, agent_id="Planner")

    if decision.auto_approved:
        # ejecutar tool directamente, y loguear (ya lo hace evaluate())
        ...
    else:
        # crear entrada en cola de approvals (Nivel B)
        ...
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, List, Dict, Union

import yaml


# ------------------------------------------------------------------
# Data class for Gate decision
# ------------------------------------------------------------------
@dataclass
class GateDecision:
    """Resultado de una evaluación de política.

    Campos v2 (`effect`, `risk`, `rule_id`, `request_id`) son la decisión real.
    `nivel` y `auto_approved` se conservan porque `files_api.py`, `shell_api.py`,
    `search_api.py`, `planner.py` y `retriever.py` ya los consumen: se derivan del
    efecto para no romperlos.

    `effect`:
      - `permit` → ejecutar
      - `deny`   → no ejecutar, sin preguntar (irrevocable por el agente)
      - `ask`    → requiere aprobación humana

    `risk`: `low` | `medium` | `high` | `critical`.
    `rule_id`: la regla exacta que decidió (auditoría y explicabilidad).
    `request_id`: correlación con el evento SSE y la aprobación.
    """

    tool: str
    agent_id: str
    nivel: str  # "A" o "B" (derivado de effect, compatibilidad)
    auto_approved: bool
    reason: str
    paths_checked: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    # ── v2 ────────────────────────────────────────────────────────
    effect: str = "permit"          # permit | deny | ask
    risk: str = "low"               # low | medium | high | critical
    rule_id: str = "unspecified"
    request_id: str = ""
    matched: List[str] = field(default_factory=list)
    analyzable: bool = True
    context: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Deriva nivel/auto_approved del efecto si no se pasaron explícitos.

        Se mantiene la equivalencia histórica para no romper consumidores:
        `permit` es Nivel A; `ask` y `deny` son Nivel B (ninguno se ejecuta sin
        intervención).
        """
        self.nivel = "A" if self.effect == "permit" else "B"
        self.auto_approved = self.effect == "permit"
        if not self.request_id:
            import uuid
            self.request_id = f"req_{uuid.uuid4().hex[:12]}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "agent_id": self.agent_id,
            "nivel": self.nivel,
            "auto_approved": self.auto_approved,
            "reason": self.reason,
            "paths_checked": self.paths_checked,
            "timestamp": self.timestamp,
            "effect": self.effect,
            "risk": self.risk,
            "rule_id": self.rule_id,
            "request_id": self.request_id,
            "matched": self.matched,
            "analyzable": self.analyzable,
            "context": self.context,
        }


# ------------------------------------------------------------------
# Gate class
# ------------------------------------------------------------------
class Gate:
    def __init__(self, rules_path: Union[str, Path], db_path: Optional[Union[str, Path]] = None):
        """
        Inicializa el Gate con el archivo de reglas y opcionalmente
        la ruta a la base de datos para logging.

        Args:
            rules_path: Ruta al archivo YAML con las reglas.
            db_path: Ruta al archivo SQLite para el log. Si es None, no se loguea.
        """
        self.rules_path = Path(rules_path)
        self.db_path = Path(db_path) if db_path else None

        # Cargar reglas
        self._load_rules()

        # Preparar estructuras
        self.workspace_dir = self._normalize_path(
            self._expand_placeholders(self.rules["workspace_dir"])
        )
        self.allowed_roots = [
            self._normalize_path(p)
            for p in (self._expand_placeholders(r) for r in self.rules.get("allowed_roots", []))
            if p is not None
        ]
        self.tools: Dict[str, str] = self.rules.get("tools", {})
        self.path_params: Dict[str, List[str]] = self.rules.get("path_params", {})
        # v2: tools cuyo riesgo está en un argumento de línea de comandos.
        self.command_params: Dict[str, str] = self.rules.get("command_params", {})
        self.command_allowlist: Dict[str, Any] = self.rules.get("command_allowlist", {}) or {}

        # Cargar agent_path_allowlist (Gap 3 — M1)
        self.agent_allowlist: Dict[str, List[Path]] = {}
        raw_allowlist = self.rules.get("agent_path_allowlist", {})
        for agent, paths in raw_allowlist.items():
            self.agent_allowlist[agent] = [
                self._normalize_path(p)
                for p in (self._expand_placeholders(x) for x in paths)
                if p is not None
            ]

        # Validaciones
        if self.workspace_dir not in self.allowed_roots and not any(
            self._is_path_under(self.workspace_dir, root) for root in self.allowed_roots
        ):
            # Si workspace_dir no está bajo ningún allowed_root, se agrega como allowed_root implícito
            # para evitar comportamientos extraños.
            self.allowed_roots.append(self.workspace_dir)

        # Crear tabla de log si se especificó db_path
        if self.db_path:
            self._ensure_table()

    # ------------------------------------------------------------------
    # Carga y validación de reglas
    # ------------------------------------------------------------------
    def _load_rules(self) -> None:
        """Carga el archivo de reglas y valida su estructura básica."""
        if not self.rules_path.exists():
            raise FileNotFoundError(f"Archivo de reglas no encontrado: {self.rules_path}")
        with open(self.rules_path, "r", encoding="utf-8") as f:
            self.rules = yaml.safe_load(f)

        required_keys = {"workspace_dir", "tools", "path_params"}
        if not all(k in self.rules for k in required_keys):
            missing = required_keys - set(self.rules.keys())
            raise ValueError(f"Faltan claves requeridas en el archivo de reglas: {missing}")

        # Asegurar que 'allowed_roots' exista
        if "allowed_roots" not in self.rules:
            self.rules["allowed_roots"] = []

        # Verificar que las categorías de herramientas sean válidas
        valid_categories = {"read_only", "always_b", "write_scoped"}
        for tool, cat in self.rules["tools"].items():
            if cat not in valid_categories:
                raise ValueError(
                    f"Categoría inválida '{cat}' para la herramienta '{tool}'. "
                    f"Debe ser una de: {valid_categories}"
                )

    def reload(self) -> None:
        """Recarga las reglas desde el archivo sin reiniciar la instancia."""
        self._load_rules()
        # Actualizar estructuras
        self.workspace_dir = self._normalize_path(
            self._expand_placeholders(self.rules["workspace_dir"])
        )
        self.allowed_roots = [
            self._normalize_path(p)
            for p in (self._expand_placeholders(r) for r in self.rules.get("allowed_roots", []))
            if p is not None
        ]
        self.tools = self.rules.get("tools", {})
        self.path_params = self.rules.get("path_params", {})
        self.command_params = self.rules.get("command_params", {})
        self.command_allowlist = self.rules.get("command_allowlist", {}) or {}

        # Recargar agent_path_allowlist (Gap 3 — M1)
        self.agent_allowlist = {}
        raw_allowlist = self.rules.get("agent_path_allowlist", {})
        for agent, paths in raw_allowlist.items():
            self.agent_allowlist[agent] = [
                self._normalize_path(p)
                for p in (self._expand_placeholders(x) for x in paths)
                if p is not None
            ]

    # ------------------------------------------------------------------
    # Expansión de placeholders portables (${VCORE_ROOT} / ${VCORE_DATA})
    # ------------------------------------------------------------------
    @staticmethod
    def _expand_placeholders(value: str) -> Optional[str]:
        """Expande ${VCORE_ROOT} y ${VCORE_DATA} con defaults portables.

        gate_rules.yaml usa placeholders para no asumir rutas absolutas del
        host (el repo se puede clonar en cualquier directorio).

        - ${VCORE_ROOT}: variable de entorno VCORE_ROOT o, si no está
          definida, la raíz del repo calculada desde este archivo.
        - ${VCORE_DATA}: variable VCORE_DATA. Si no está definida, la entrada
          se omite (None): una instalación sin disco de datos no genera
          allowed_roots inválidos.
        """
        root = os.environ.get("VCORE_ROOT") or str(Path(__file__).resolve().parent)
        value = value.replace("${VCORE_ROOT}", root)
        if "${VCORE_DATA}" in value:
            data = os.environ.get("VCORE_DATA")
            if not data:
                return None
            value = value.replace("${VCORE_DATA}", data)
        return value

    # ------------------------------------------------------------------
    # Normalización y comparación de paths (cross-platform)
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_path(path: Union[str, Path]) -> Path:
        """
        Normaliza un path a una ruta absoluta, resolviendo '..' y enlaces simbólicos,
        sin requerir que el path exista en disco. Es cross-platform.

        - Convierte a Path.
        - Expande variables de entorno (si las hay).
        - Convierte a absoluto (usando el directorio actual como base si es relativo).
        - Resuelve '..' y '.' para obtener una ruta limpia.
        """
        p = Path(path)
        # Expandir variables de entorno (ej: %USERPROFILE% en Windows)
        expanded = os.path.expandvars(str(p))
        p = Path(expanded)
        # Convertir a absoluto (si es relativo, se resuelve respecto al CWD)
        p = p.resolve()
        return p

    @staticmethod
    def _is_path_under(child: Path, parent: Path) -> bool:
        """
        Verifica si 'child' está dentro de 'parent' (o es igual) de manera segura.
        Usa Path.relative_to para evitar ataques de path traversal.
        """
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False

    # ------------------------------------------------------------------
    # Extracción de paths de los parámetros
    # ------------------------------------------------------------------
    def _extract_paths_with_base(self, tool: str, params: Dict[str, Any],
                                 base: Path) -> List[Path]:
        """
        Extrae los paths relevantes de los parámetros según la configuración
        de path_params. Los paths se normalizan a absolutos y se resuelven
        '..' y enlaces simbólicos. Si un path es relativo, se interpreta
        como relativo a `base` (el workspace efectivo de la sesión).
        """
        param_names = self.path_params.get(tool, [])
        raw_paths: List[str] = []
        for name in param_names:
            value = params.get(name)
            if value is None:
                continue
            if isinstance(value, str):
                raw_paths.append(value)
            elif isinstance(value, (list, tuple)):
                raw_paths.extend(str(v) for v in value)

        resolved_paths: List[Path] = []
        for raw in raw_paths:
            p = Path(raw)
            # Si es relativo, unirlo con el base efectivo
            if not p.is_absolute():
                p = base / p
            # Normalizar (absoluto, resolver .., etc.)
            resolved = self._normalize_path(p)
            resolved_paths.append(resolved)
        return resolved_paths

    # ------------------------------------------------------------------
    # Evaluación principal
    # ------------------------------------------------------------------
    def evaluate(self, tool: str, params: Dict[str, Any], agent_id: str = "unknown",
                 context: Any = None) -> GateDecision:
        """
        Evalúa la herramienta y los parámetros y devuelve una decisión.

        Args:
            tool: Nombre de la herramienta.
            params: Diccionario de parámetros.
            agent_id: Identificador del agente que solicita la ejecución.
            context: `api.policy.PolicyContext` (o dict equivalente) con el
                workspace efectivo, el modo sandbox y la correlación. Si es None
                se usa el workspace configurado y sandbox desactivado.

        Returns:
            GateDecision con efecto (permit/deny/ask), riesgo y regla aplicada.
        """
        ctx = self._norm_context(context)

        # ── 0. Tools de comando: evaluación por ARGUMENTO ────────────
        # Va antes de las categorías porque una tool de shell no tiene un path
        # que validar: su riesgo está en la línea de comandos. La allowlist por
        # tokens es la única evaluación honesta posible.
        command_param = self.command_params.get(tool)
        if command_param:
            command = str(params.get(command_param) or "")
            decision = self._evaluate_command(tool, agent_id, command, ctx)
            self._log(decision)
            return decision

        category = self.tools.get(tool)

        # Tool desconocida -> fail-safe a Nivel B
        if category is None:
            decision = GateDecision(
                tool=tool,
                agent_id=agent_id,
                nivel="B",
                auto_approved=False,
                reason=f"Tool '{tool}' no está en gate_rules.yaml (fail-safe: Nivel B)",
                effect="ask",
                risk="medium",
                rule_id="tool.unknown",
                context=ctx,
            )
            self._log(decision)
            return decision

        # Categorías simples
        if category == "read_only":
            # Gap 3: validar agent_path_allowlist incluso para lectura
            paths = self._extract_paths(tool, params, ctx)
            if paths:
                agent_paths = self.agent_allowlist.get(agent_id)
                if agent_paths is not None:
                    for p in paths:
                        if not any(self._is_path_under(p, allowed) for allowed in agent_paths):
                            decision = GateDecision(
                                tool=tool,
                                agent_id=agent_id,
                                nivel="B",
                                auto_approved=False,
                                reason=f"Path '{p}' fuera del allowlist del agente '{agent_id}' (fail-safe: Nivel B)",
                                paths_checked=[str(p) for p in paths],
                                effect="ask",
                                risk="medium",
                                rule_id="path.outside_agent_allowlist",
                                context=ctx,
                            )
                            self._log(decision)
                            return decision
            decision = GateDecision(
                tool=tool,
                agent_id=agent_id,
                nivel="A",
                auto_approved=True,
                reason="read_only: siempre Nivel A",
                effect="permit",
                risk="low",
                rule_id="tool.read_only",
                paths_checked=[str(p) for p in paths],
                context=ctx,
            )
            self._log(decision)
            return decision

        if category == "always_b":
            decision = GateDecision(
                tool=tool,
                agent_id=agent_id,
                nivel="B",
                auto_approved=False,
                reason="always_b: requiere aprobación sin excepción",
                effect="ask",
                risk="high",
                rule_id="tool.always_b",
                context=ctx,
            )
            self._log(decision)
            return decision

        if category == "write_scoped":
            paths = self._extract_paths(tool, params, ctx)
            decision = self._evaluate_write_scoped(tool, agent_id, paths, ctx)
            self._log(decision)
            return decision

        # Categoría desconocida -> fail-safe
        decision = GateDecision(
            tool=tool,
            agent_id=agent_id,
            nivel="B",
            auto_approved=False,
            reason=f"Categoría '{category}' no reconocida (fail-safe: Nivel B)",
            effect="ask",
            risk="medium",
            rule_id="tool.unknown_category",
            context=ctx,
        )
        self._log(decision)
        return decision

    # ------------------------------------------------------------------
    # Contexto y evaluación de comandos
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_context(context: Any) -> Dict[str, Any]:
        """Normaliza el contexto a un dict plano, sin importar su origen."""
        if context is None:
            return {}
        if isinstance(context, dict):
            return dict(context)
        as_dict = getattr(context, "as_dict", None)
        if callable(as_dict):
            return as_dict()
        return {}

    def _extract_paths(self, tool: str, params: Dict[str, Any],
                       ctx: Optional[Dict[str, Any]] = None) -> List[Path]:
        """Extrae paths validando contra el workspace efectivo del contexto.

        Un path relativo se resuelve contra el workspace de la sesión si el
        contexto lo define; si no, contra `workspace_dir` (comportamiento
        histórico).
        """
        base = self.workspace_dir
        if ctx and ctx.get("workspace"):
            try:
                base = self._normalize_path(ctx["workspace"])
            except Exception:
                base = self.workspace_dir
        return self._extract_paths_with_base(tool, params, base)

    def _evaluate_command(self, tool: str, agent_id: str, command: str,
                          ctx: Dict[str, Any]) -> GateDecision:
        """Evalúa una tool de shell con el motor de política de comandos.

        El perímetro para comandos se toma de `allowed_roots` (el alcance real
        donde el agente puede operar) y no del workspace, porque los comandos
        legítimos del proyecto se ejecutan desde la raíz del repo: usar el
        workspace como frontera generaría un `ask` en cada lectura del repo.
        """
        from system.policy_commands import assess_command

        workspace_root = self._normalize_path(ctx["workspace"]) if ctx.get("workspace") else self.workspace_dir
        assessment = assess_command(
            command,
            workspace=workspace_root,
            allowed_roots=self.allowed_roots,
            allowlist=self.command_allowlist,
            sandbox=bool(ctx.get("sandbox")),
        )
        return GateDecision(
            tool=tool,
            agent_id=agent_id,
            nivel="A",
            auto_approved=False,
            reason=assessment.reason,
            paths_checked=assessment.paths_checked,
            effect=assessment.effect,
            risk=assessment.risk,
            rule_id=assessment.rule_id,
            matched=assessment.matched,
            analyzable=assessment.analyzable,
            context=ctx,
        )

    def _evaluate_write_scoped(self, tool: str, agent_id: str, paths: List[Path],
                               ctx: Optional[Dict[str, Any]] = None) -> GateDecision:
        """Evaluación para herramientas de categoría 'write_scoped' con agent_path_allowlist (Gap 3 — M1)."""
        ctx = ctx or {}
        if not paths:
            return GateDecision(
                tool=tool,
                agent_id=agent_id,
                nivel="B",
                auto_approved=False,
                reason="write_scoped: no se pudo extraer path objetivo (fail-safe: Nivel B)",
                paths_checked=[str(p) for p in paths],
                effect="ask",
                risk="medium",
                rule_id="path.missing",
                context=ctx,
            )

        # --- Gap 3: Validar agent_path_allowlist ---
        agent_paths = self.agent_allowlist.get(agent_id)
        if agent_paths is None:
            return GateDecision(
                tool=tool,
                agent_id=agent_id,
                nivel="B",
                auto_approved=False,
                reason=f"Agente '{agent_id}' no tiene path_allowlist definido (fail-safe: Nivel B)",
                paths_checked=[str(p) for p in paths],
                effect="ask",
                risk="medium",
                rule_id="path.agent_not_allowlisted",
                context=ctx,
            )

        # Escapes del perímetro se evalúan PRIMERO: es la frontera dura y su
        # clasificación no debe depender de que el agente tenga allowlist (si el
        # orden se invierte, un path fuera de todo sale como `ask` en vez de
        # `deny` sólo porque el agente no está en el YAML).
        for p in paths:
            if not any(self._is_path_under(p, root) for root in self.allowed_roots):
                return GateDecision(
                    tool=tool,
                    agent_id=agent_id,
                    nivel="B",
                    auto_approved=False,
                    reason=f"Path fuera de allowed_roots: {p}",
                    paths_checked=[str(p) for p in paths],
                    effect="deny",
                    risk="high",
                    rule_id="path.outside_perimeter",
                    context=ctx,
                )

        for p in paths:
            # Verificar que el path esté dentro del allowlist del agente
            if not any(self._is_path_under(p, allowed) for allowed in agent_paths):
                return GateDecision(
                    tool=tool,
                    agent_id=agent_id,
                    nivel="B",
                    auto_approved=False,
                    reason=f"Path '{p}' fuera del allowlist del agente '{agent_id}' (fail-safe: Nivel B)",
                    paths_checked=[str(p) for p in paths],
                    effect="ask",
                    risk="medium",
                    rule_id="path.outside_agent_allowlist",
                    context=ctx,
                )
            # Verificar que esté dentro del workspace efectivo O de allowed_roots
            workspace = self.workspace_dir
            if ctx.get("workspace"):
                try:
                    workspace = self._normalize_path(ctx["workspace"])
                except Exception:
                    pass
            if not self._is_path_under(p, workspace):
                # Fuera del workspace de la sesión: escribe en el repo o en otro
                # root permitido. No es un escape del perímetro, pero sí una
                # acción con alcance mayor: se pide aprobación.
                return GateDecision(
                    tool=tool,
                    agent_id=agent_id,
                    nivel="B",
                    auto_approved=False,
                    reason=f"Path fuera del workspace de la sesión: {p}",
                    paths_checked=[str(p) for p in paths],
                    effect="ask",
                    risk="medium",
                    rule_id="path.outside_workspace",
                    context=ctx,
                )

        # Todos los paths pasaron las verificaciones
        return GateDecision(
            tool=tool,
            agent_id=agent_id,
            nivel="A",
            auto_approved=True,
            reason="write_scoped: todos los paths dentro de allowlist, allowed_roots y workspace/",
            paths_checked=[str(p) for p in paths],
            effect="permit",
            risk="low",
            rule_id="path.inside_workspace",
            context=ctx,
        )

    # ------------------------------------------------------------------
    # Logging a vcore.db (Capa 3)
    # ------------------------------------------------------------------
    def _ensure_table(self) -> None:
        """Crea la tabla de log si no existe y migra las columnas de v2.

        La migración es idempotente (mismo patrón que `main.py::_ensure_sessions_title_column`):
        SQLite no soporta `ADD COLUMN IF NOT EXISTS`, así que se intenta y se
        ignora el error de columna duplicada. Las filas viejas quedan con los
        campos v2 en NULL, que es la verdad: no fueron evaluadas por el motor v2.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gate_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    agent_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    nivel TEXT NOT NULL,
                    auto_approved INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    paths_checked TEXT
                )
                """
            )
            for ddl in (
                "ALTER TABLE gate_log ADD COLUMN effect TEXT",
                "ALTER TABLE gate_log ADD COLUMN risk TEXT",
                "ALTER TABLE gate_log ADD COLUMN rule_id TEXT",
                "ALTER TABLE gate_log ADD COLUMN request_id TEXT",
                "ALTER TABLE gate_log ADD COLUMN matched TEXT",
                "ALTER TABLE gate_log ADD COLUMN analyzable INTEGER",
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            conn.commit()
        finally:
            conn.close()

    def _log(self, decision: GateDecision) -> None:
        """Inserta un registro de decisión en la base de datos."""
        if not self.db_path:
            return
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO gate_log
                    (timestamp, agent_id, tool, nivel, auto_approved, reason,
                     paths_checked, effect, risk, rule_id, request_id, matched, analyzable)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.timestamp,
                    decision.agent_id,
                    decision.tool,
                    decision.nivel,
                    int(decision.auto_approved),
                    decision.reason,
                    ";".join(decision.paths_checked),
                    decision.effect,
                    decision.risk,
                    decision.rule_id,
                    decision.request_id,
                    ";".join(decision.matched),
                    int(decision.analyzable),
                ),
            )
            conn.commit()
        finally:
            conn.close()


# ------------------------------------------------------------------
# Ejemplo de uso / test
# ------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile
    import json

    # Crear un archivo de reglas de ejemplo
    rules_example = {
        "workspace_dir": "C:/workspace",
        "allowed_roots": ["C:/workspace", "D:/external"],
        "tools": {
            "read_file": "read_only",
            "write_file": "write_scoped",
            "delete_file": "always_b",
            "unknown_tool": "read_only",  # esto se usará para probar
        },
        "path_params": {
            "write_file": ["path", "targets"],
            "delete_file": ["file_path"],
            "read_file": ["path"],
        },
        "agent_path_allowlist": {
            "Planner": ["C:/workspace", "C:/workspace/agents"],
            "Retriever": ["C:/workspace"],
            "UNKNOWN": ["C:/workspace"],
        }
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(rules_example, f)
        rules_path = f.name

    db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name

    gate = Gate(rules_path, db_path)

    # Test 1: read_only -> Nivel A
    decision = gate.evaluate("read_file", {"path": "C:/workspace/data.txt"}, agent_id="Planner")
    print(decision)

    # Test 2: write_scoped dentro de workspace -> Nivel A
    decision = gate.evaluate("write_file", {"path": "C:/workspace/output/result.txt"}, agent_id="Planner")
    print(decision)

    # Test 3: write_scoped con path fuera de workspace -> Nivel B
    decision = gate.evaluate("write_file", {"path": "C:/temp/out.txt"}, agent_id="Planner")
    print(decision)

    # Test 4: write_scoped con path relativo -> se resuelve respecto a workspace
    decision = gate.evaluate("write_file", {"path": "relative/inside.txt"}, agent_id="Planner")
    print(decision)

    # Test 5: always_b -> Nivel B
    decision = gate.evaluate("delete_file", {"file_path": "C:/workspace/old.txt"}, agent_id="Planner")
    print(decision)

    # Test 6: tool desconocida -> Nivel B
    decision = gate.evaluate("non_existent", {}, agent_id="Planner")
    print(decision)

    # Test 7: read_only fuera de allowlist -> Nivel B
    decision = gate.evaluate("read_file", {"path": "C:/temp/secret.txt"}, agent_id="Retriever")
    print(decision)

    # Test 8: agente sin allowlist -> Nivel B
    decision = gate.evaluate("write_file", {"path": "C:/workspace/test.txt"}, agent_id="HACKER")
    print(decision)

    # Limpiar
    os.unlink(rules_path)
    os.unlink(db_path)
