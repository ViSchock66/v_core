#!/usr/bin/env python3
"""
vcore.py — CLI de V-CORE v1.2
================================
14 comandos, ~50 subcomandos. Respaldado por la API REST + acceso directo a DB/archivos.

Uso:
    vcore chat                  Sesión interactiva con ENLIL
    vcore chat -q "msg"         One-shot, imprime y sale
    vcore chat -c               Continuar última sesión
    vcore chat -r <id>          Resumir sesión por ID

    vcore sessions list         Listar sesiones recientes
    vcore sessions resume <id>  Resumir sesión
    vcore sessions delete <id>  Eliminar sesión
    vcore sessions export       Exportar a JSONL

    vcore agents list           Listar agentes
    vcore agents status         Estado de los agentes
    vcore agents invoke <n> <m> Invocar agente específico
    vcore agents council "msg"  Council mode (todos opinan)

    vcore config show           Ver configuración actual
    vcore config edit           Abrir en $EDITOR
    vcore config validate       Validar YAMLs
    vcore config path           Mostrar rutas de archivos

    vcore models list           Modelos disponibles
    vcore models switch <r> <m> Hot-swap de modelo
    vcore models presets        Ver presets con context windows

    vcore tools list            Listar herramientas disponibles

    vcore mcp list              Listar servidores MCP
    vcore mcp add <name>        Agregar servidor
    vcore mcp remove <name>     Remover servidor

    vcore gate rules            Mostrar reglas del gate
    vcore gate log              Últimas decisiones del gate

    vcore approvals list        Aprobaciones pendientes
    vcore approvals approve <id> Aprobar
    vcore approvals reject <id> Rechazar

    vcore memory status         Estado de ChromaDB
    vcore memory search "q"     Buscar en memoria semántica
    vcore memory stats          Estadísticas de colecciones

    vcore logs gate             Log del gate
    vcore logs llm              Log de uso de LLM
    vcore logs traces           Trazas recientes
    vcore logs circuit          Estado de circuit breakers

    vcore db stats              Estadísticas de la DB
    vcore db tables             Listar tablas

    vcore serve                 Levantar servidor API
    vcore doctor                Diagnóstico completo del sistema
    vcore init                  Inicializar DB + workspace
    vcore status                Estado rápido
    vcore audit [--visual]      Ejecutar auditoría
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Fuente unica del puerto (api/ports.py). Antes estaba hardcodeado a 8000
# mientras el README y AGENTS.md decian 8001, y el CLI no alcanzaba al
# servidor real. VCORE_API sigue funcionando como override completo.
from api.ports import api_base as _api_base, port as _port  # noqa: E402
# Fuente unica de la version (VCORE_STATE.json -> api/version.py). El banner y
# el doctor tenian "v1.5.0" hardcodeado y quedaban stale en cada bump.
from api.version import VERSION as _VERSION  # noqa: E402

API_BASE = os.environ.get("VCORE_API", _api_base())

# ── Habilitar procesamiento ANSI/VT en Windows ──────────────────
if sys.platform == "win32":
    import ctypes
    kernel32 = ctypes.windll.kernel32
    for handle in [kernel32.GetStdHandle(-11), kernel32.GetStdHandle(-12)]:
        mode = ctypes.c_uint32()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING

# ── ANSI styling ───────────────────────────────────────────────
class S:
    """Paleta monocroma profesional. Sin emojis."""
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RED    = "\033[31m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    BLUE   = "\033[34m"
    CYAN   = "\033[36m"
    WHITE  = "\033[37m"
    GRAY   = "\033[90m"

    # Indicadores ASCII
    OK     = f"{GREEN}[OK]{RESET}"
    WARN   = f"{YELLOW}[--]{RESET}"
    FAIL   = f"{RED}[!!]{RESET}"
    INFO   = f"{BLUE}[i]{RESET}"
    ARROW  = f"{DIM}>{RESET}"
    BULLET = f"{DIM}-{RESET}"

    @staticmethod
    def label(text: str) -> str:
        return f"{S.CYAN}{text}{S.RESET}"

    @staticmethod
    def dim(text: str) -> str:
        return f"{S.GRAY}{text}{S.RESET}"

    @staticmethod
    def header(text: str) -> str:
        return f"\n{S.BOLD}{S.WHITE}{text}{S.RESET}"

    @staticmethod
    def err(text: str) -> str:
        return f"{S.RED}{text}{S.RESET}"

    @staticmethod
    def ok(text: str) -> str:
        return f"{S.GREEN}{text}{S.RESET}"

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _api(path: str, method: str = "GET", data: dict | None = None) -> dict:
    """Llama a la API REST de V-CORE. Retorna dict o lanza."""
    import urllib.request
    import urllib.error

    url = f"{API_BASE}{path}"
    body = json.dumps(data).encode() if data else None

    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = json.loads(e.read().decode()).get("detail", str(e))
        raise SystemExit(f"API error {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"No se pudo conectar a {API_BASE}. ¿Servidor corriendo? ({e.reason})")


def _api_ok(path: str, method: str = "GET", data: dict | None = None) -> bool:
    """True si la API responde 2xx."""
    try:
        _api(path, method, data)
        return True
    except SystemExit:
        return False


def _db_query(sql: str, params: tuple = ()) -> list:
    """Ejecuta query en vcore.db y retorna filas."""
    import sqlite3
    conn = sqlite3.connect(str(ROOT / "vcore.db"))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


def _print_table(rows: list, cols: list[str] | None = None):
    """Tabla con bordes ASCII box-drawing."""
    if not rows:
        print(f"  {S.dim('(sin datos)')}")
        return
    if cols is None and rows:
        cols = list(rows[0].keys())
    widths = {c: len(c) for c in cols}
    for r in rows:
        for c in cols:
            val = str(r[c] if isinstance(r, dict) else (r[c] if hasattr(r, "keys") else ""))
            widths[c] = max(widths[c], min(len(val), 60))
    # top border
    top = S.dim("\u250c" + "\u252c".join("\u2500" * (widths[c] + 2) for c in cols) + "\u2510")
    sep  = S.dim("\u251c" + "\u253c".join("\u2500" * (widths[c] + 2) for c in cols) + "\u2524")
    bot = S.dim("\u2514" + "\u2534".join("\u2500" * (widths[c] + 2) for c in cols) + "\u2518")
    print(top)
    # header
    hdr = S.dim("\u2502") + S.dim("\u2502").join(f" {S.BOLD}{c.ljust(widths[c])}{S.RESET} " for c in cols) + S.dim("\u2502")
    print(hdr)
    print(sep)
    # rows
    for r in rows:
        vals = []
        for c in cols:
            val = r[c] if isinstance(r, dict) else (r[c] if hasattr(r, "keys") else "")
            vals.append(str(val)[:60].ljust(widths[c]))
        row = S.dim("\u2502") + S.dim("\u2502").join(f" {v} " for v in vals) + S.dim("\u2502")
        print(row)
    print(bot)


def _yesno(prompt: str) -> bool:
    """Pregunta sí/no. Default no."""
    ans = input(f"{prompt} [s/N] ").strip().lower()
    return ans in ("s", "si", "sí", "y", "yes")


# ─────────────────────────────────────────────────────────────
# CHAT
# ─────────────────────────────────────────────────────────────

def cmd_chat(args):
    """Interactivo o one-shot con ENLIL."""
    if args.query:
        _chat_oneshot(args.query)
    elif args.continue_:
        _chat_continue()
    elif args.resume:
        _chat_resume(args.resume)
    else:
        _chat_interactive()


def _chat_oneshot(message: str):
    """Envía mensaje y streamea respuesta."""
    import urllib.request
    url = f"{API_BASE}/agents/route"
    body = json.dumps({
        "message": message,
        "session_id": f"cli-{int(time.time())}",
        "history": [],
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            buffer = b""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.decode("utf-8", errors="replace").strip()
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            print()
                            return
                        try:
                            obj = json.loads(data)
                            ctype = obj.get("__event__") or obj.get("type", "")
                            if ctype == "chunk":
                                print(obj.get("content", ""), end="", flush=True)
                            elif ctype == "artifact":
                                print(f"\n  {S.INFO} artifact: {obj.get('title','')} {S.dim('->')} {obj.get('path','')}")
                            elif ctype == "tool_call":
                                tool = obj.get("tool", "")
                                print(f"\n  {S.dim('::')} {S.label(tool)}", end="", flush=True)
                        except json.JSONDecodeError:
                            pass
    except urllib.error.URLError as e:
        raise SystemExit(f"No se pudo conectar a {API_BASE}. ¿Servidor corriendo?")


def _chat_continue():
    """Continúa última sesión."""
    sessions = _api("/sessions")
    if not sessions:
        raise SystemExit("No hay sesiones previas.")
    last = sessions[0]
    print(f"Continuando sesión {last.get('id','?')}: {last.get('title','sin título')[:60]}")
    _chat_resume(str(last["id"]))


def _chat_resume(session_id: str):
    """Resume sesión por ID."""
    msgs = _api(f"/sessions/{session_id}/messages")
    print(f"Sesión {session_id}: {len(msgs)} mensajes. Escribe tu mensaje:")
    msg = input("> ")
    if not msg.strip():
        return
    _chat_oneshot(msg)


def _chat_interactive():
    """REPL interactivo con prompt_toolkit: autocompletado, hint inline, selector."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.styles import Style

    SLASH_CMDS = sorted([
        "/quit", "/exit", "/q", "/clear", "/new", "/reset",
        "/doctor", "/status", "/models", "/model", "/sessions",
        "/agents", "/config", "/tools", "/mcp", "/gate",
        "/approvals", "/memory", "/logs", "/db", "/audit", "/help",
    ])

    class SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor.lower()
            if text.startswith('/'):
                for cmd in SLASH_CMDS:
                    if cmd.startswith(text):
                        yield Completion(cmd, start_position=-len(text),
                                         display_meta=cmd,
                                         style='class:completion')

    style = Style.from_dict({
        '':          '#cccccc',
        'completion-menu': 'bg:#1a1a1a #888888',
        'completion-menu.completion': 'bg:#2a2a2a #cccccc',
        'completion-menu.completion.current': 'bg:#3a3a3a #ffffff',
        'bottom-toolbar': 'bg:#111111 #666666',
        'bottom-toolbar.text': '#666666',
        'prompt': '#888888',
    })

    session = PromptSession(
        message=[('class:prompt', '> ')],
        completer=SlashCompleter(),
        style=style,
        complete_while_typing=True,
        bottom_toolbar=f" Tab=completar  /help=comandos  /quit=salir ",
    )

    banner = f"""
{S.BOLD}{S.CYAN}  V-CORE CLI{S.RESET} {S.DIM}v{_VERSION}{S.RESET}
{S.DIM}  Tab = autocompletar. /help = comandos.{S.RESET}
"""
    print(banner)
    try:
        model_info = _api("/system/model")
        lead = model_info.get("enlil_lead", {}).get("model", "?")
        print(f"  {S.label('lead')} {S.dim('=')} {lead}\n")
    except Exception:
        pass
    session_id = f"cli-{int(time.time())}"
    while True:
        try:
            msg = session.prompt().strip()
            if not msg:
                continue
            if msg.startswith("/"):
                handled = _handle_slash(msg, session_id)
                if handled is False:
                    break
                continue
            _chat_oneshot_followup(session_id, msg)
        except KeyboardInterrupt:
            print()
            break
        except EOFError:
            print()
            break
        except Exception as e:
            print(f"  {S.err(str(e))}")


def _common_prefix(strings: list[str]) -> str:
    """Prefijo común más largo de una lista de strings."""
    if not strings:
        return ""
    prefix = strings[0]
    for s in strings[1:]:
        while not s.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix


def _interactive_pick(items: list[dict], title: str = "") -> dict | None:
    """Selector con prompt_toolkit radiolist — tema oscuro."""
    from prompt_toolkit.shortcuts import radiolist_dialog
    from prompt_toolkit.styles import Style

    values = [(item, item.get("label", "?")) for item in items]
    style = Style.from_dict({
        'dialog': 'bg:#0d0d0d #cccccc',
        'dialog.body': 'bg:#0d0d0d #cccccc',
        'dialog frame.label': 'bg:#0d0d0d #ffffff bold',
        'radiolist': 'bg:#0d0d0d #cccccc',
        'button': 'bg:#2a2a2a #cccccc',
        'button.focused': 'bg:#3a3a3a #ffffff',
        'text-area': 'bg:#0d0d0d #cccccc',
    })
    result = radiolist_dialog(
        title=title,
        text="",
        values=values,
        style=style,
    ).run()
    if result:
        label = result.get("label", "?")
        sys.stdout.write(f"\n  {S.OK} {label}\n")
    else:
        sys.stdout.write(f"\n  {S.dim('cancelado')}\n")
    return result


def _handle_slash(raw: str, session_id: str) -> bool | None:
    """Despacha slash commands. Retorna False para salir, True si manejado, None si desconocido."""
    parts = raw.split(maxsplit=1)
    cmd = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    # Session control
    if cmd in ("/quit", "/exit", "/q"):
        print(S.dim("  sesion terminada."))
        return False
    if cmd == "/clear":
        print("\033[2J\033[H")
        return True
    if cmd in ("/new", "/reset"):
        nonlocal_session = f"cli-{int(time.time())}"
        print(f"{S.dim('nueva sesion:')} {nonlocal_session}")
        return True

    # Info
    if cmd == "/help":
        _print_slash_help()
        return True
    if cmd == "/doctor":
        cmd_doctor(None)
        return True
    if cmd == "/status":
        cmd_status(None)
        return True

    # Models — con selector interactivo
    if cmd == "/models":
        data = _api("/system/model/available")
        models = data.get("models", []) if isinstance(data, dict) else data
        items = []
        for m in models:
            name = m.get("name") or m.get("id", "?")
            model_id = m.get("id", "")
            ctx = m.get("context", "?")
            pri = m.get("priority", "")
            active = " [activo]" if pri == 1 else ""
            items.append({
                "label": f"{name}{active}",
                "detail": f"ctx={ctx}  {model_id}",
                "value": m,
            })
        items.append({"label": "[cancelar]", "detail": "", "value": None})
        picked = _interactive_pick(items, "modelos — selecciona para cambiar lead")
        if picked and picked.get("value") is not None:
            model = picked["value"]
            _api("/system/model", method="POST", data={"role": "enlil_lead", "model": model["id"]})
            print(f"  {S.OK} lead {S.dim('->')} {S.label(model.get('name', model['id']))}")
        return True

    if cmd == "/model" and rest:
        parts2 = rest.split(maxsplit=1)
        if len(parts2) == 2:
            cmd_models(argparse.Namespace(action="switch", role=parts2[0], model=parts2[1]))
        return True

    # Data
    if cmd == "/sessions":
        cmd_sessions(argparse.Namespace(action="list"))
        return True
    if cmd == "/agents":
        cmd_agents(argparse.Namespace(action="list"))
        return True
    if cmd == "/config":
        cmd_config(argparse.Namespace(action="show"))
        return True
    if cmd == "/tools":
        cmd_tools(argparse.Namespace(action="list"))
        return True
    if cmd == "/mcp":
        cmd_mcp(argparse.Namespace(action="list"))
        return True
    if cmd == "/gate":
        cmd_gate(argparse.Namespace(action="log", limit=10))
        return True
    if cmd == "/approvals":
        cmd_approvals(argparse.Namespace(action="list"))
        return True
    if cmd == "/memory":
        cmd_memory(argparse.Namespace(action="status"))
        return True
    if cmd == "/logs":
        src = rest.strip() or "llm"
        cmd_logs(argparse.Namespace(source=src, limit=10))
        return True
    if cmd == "/db":
        cmd_db(argparse.Namespace(action="stats"))
        return True
    print(f"  {S.WARN} comando desconocido: {cmd}. Usa /help")
    return True


def _print_slash_help():
    """Ayuda de slash commands."""
    cmds = [
        ("/quit, /exit, /q", "salir"),
        ("/clear", "limpiar pantalla"),
        ("/new, /reset", "nueva sesion"),
        ("/doctor", "diagnostico completo"),
        ("/status", "estado rapido"),
        ("/models", "listar modelos"),
        ("/model <rol> <modelo>", "cambiar modelo"),
        ("/sessions", "listar sesiones"),
        ("/agents", "listar agentes"),
        ("/config", "ver configuracion"),
        ("/tools", "listar herramientas"),
        ("/mcp", "servidores MCP"),
        ("/gate", "log del gate"),
        ("/approvals", "cola de aprobacion"),
        ("/memory", "estado ChromaDB"),
        ("/logs [llm|gate|traces|circuit]", "ver logs"),
        ("/db", "estadisticas DB"),
        ("/help", "esta ayuda"),
    ]
    print(f"\n{S.BOLD}  Slash commands:{S.RESET}\n")
    for c, d in cmds:
        print(f"  {S.label(c):36} {S.dim(d)}")
    print()


def _chat_oneshot_followup(session_id: str, message: str):
    """One-shot con session_id para mantener contexto."""
    import urllib.request
    url = f"{API_BASE}/agents/route"
    body = json.dumps({
        "message": message,
        "session_id": session_id,
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            buffer = b""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.decode("utf-8", errors="replace").strip()
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            print()
                            return
                        try:
                            obj = json.loads(data)
                            ctype = obj.get("__event__") or obj.get("type", "")
                            if ctype == "chunk":
                                print(obj.get("content", ""), end="", flush=True)
                        except json.JSONDecodeError:
                            pass
    except Exception as e:
        print(f"\n  {S.err(str(e))}")


# ─────────────────────────────────────────────────────────────
# SESSIONS
# ─────────────────────────────────────────────────────────────

def cmd_sessions(args):
    if args.action == "list":
        sessions = _api("/sessions")
        if not sessions:
            print("No hay sesiones.")
            return
        _print_table(sessions, ["id", "project", "status", "timestamp"])
    elif args.action == "resume":
        _chat_resume(args.session_id)
    elif args.action == "delete":
        if _yesno(f"¿Eliminar sesión {args.session_id}?"):
            _api(f"/sessions/{args.session_id}", method="DELETE")
            print(f"Sesión {args.session_id} eliminada.")
    elif args.action == "export":
        sessions = _api("/sessions")
        outfile = args.file or f"vcore_sessions_{int(time.time())}.jsonl"
        with open(outfile, "w", encoding="utf-8") as f:
            for s in sessions[:args.limit]:
                msgs = _api(f"/sessions/{s['id']}/messages")
                for m in msgs:
                    m["_session_id"] = s["id"]
                    m["_session_title"] = s.get("title", "")
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
        print(f"Exportadas {len(sessions[:args.limit])} sesiones → {outfile}")


# ─────────────────────────────────────────────────────────────
# AGENTS
# ─────────────────────────────────────────────────────────────

def cmd_agents(args):
    if args.action == "list":
        agents = _api("/agents")
        _print_table(agents, ["name", "status", "capabilities"])
    elif args.action == "status":
        status = _api("/agents/status")
        for k, v in status.items():
            print(f"  {k}: {v}")
    elif args.action == "invoke":
        print(f"Invocando {args.agent_name}...")
        _chat_oneshot(args.message)
    elif args.action == "council":
        print("Council mode — todos los agentes opinan:")
        result = _api("/agents/council", method="POST",
                       data={"message": args.message, "models": args.models.split(",") if args.models else None})
        for opinion in result.get("opinions", []):
            print(f"\n── {opinion.get('agent','?')} ({opinion.get('model','?')}) ──")
            print(opinion.get("content", "")[:500])


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

def cmd_config(args):
    if args.action == "show":
        files = {
            "model_routing.yaml": ROOT / "model_routing.yaml",
            "gate_rules.yaml": ROOT / "gate_rules.yaml",
            "mcp_config.json": ROOT / "mcp_config.json",
        }
        for name, path in files.items():
            icon = S.OK if path.exists() else S.FAIL
            size = path.stat().st_size if path.exists() else 0
            print(f"  {icon} {S.label(name)} {S.dim(f'({size:,} bytes)')}")
    elif args.action == "edit":
        editor = os.environ.get("EDITOR", "notepad")
        target = args.file or "model_routing.yaml"
        path = ROOT / target
        if not path.exists():
            raise SystemExit(f"No existe: {path}")
        subprocess.run([editor, str(path)])
    elif args.action == "validate":
        import yaml
        yamls = ["model_routing.yaml", "gate_rules.yaml"]
        ok = True
        for f in yamls:
            path = ROOT / f
            try:
                with open(path) as fh:
                    yaml.safe_load(fh)
                print(f"  {S.OK} {f}")
            except Exception as e:
                print(f"  {S.FAIL} {f}: {e}")
                ok = False
        if ok:
            print(f"\n  {S.OK} todos los YAMLs validos.")
    elif args.action == "path":
        paths = {
            "root": ROOT,
            "model_routing": ROOT / "model_routing.yaml",
            "gate_rules": ROOT / "gate_rules.yaml",
            "mcp_config": ROOT / "mcp_config.json",
            "vcore.db": ROOT / "vcore.db",
            "chroma_db": ROOT / "chroma_db",
        }
        for label, p in paths.items():
            print(f"  {S.label(label):20} {S.dim(str(p))}")

# ─────────────────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────────────────

def cmd_models(args):
    if args.action == "list":
        data = _api("/system/model/available")
        models = data.get("models", []) if isinstance(data, dict) else data
        print(f"{S.header('modelos disponibles')} {S.dim(f'({len(models)})')}")
        for m in models:
            name = m.get("name") or m.get("id") or str(m)
            model_id = m.get("id", "")
            ctx = m.get("context", "?")
            pri = m.get("priority", "")
            active = f" {S.dim('(activo)')}" if pri == 1 else ""
            print(f"  {S.label(name)} {S.dim(f'ctx={ctx}')}{active} {S.dim(model_id)}")
        lead = _api("/system/model").get("enlil_lead", {})
        print(f"\n  {S.dim('lead activo:')} {S.label(lead.get('model','?'))}")
    elif args.action == "switch":
        result = _api("/system/model", method="POST",
                       data={"role": args.role, "model": args.model})
        print(f"{S.OK} {S.label(args.role)} {S.dim('->')} {S.label(args.model)}")
        if result.get("circuit_breaker_reset"):
            print(f"  {S.dim('circuit breaker reseteado')}")
    elif args.action == "presets":
        import yaml
        with open(ROOT / "model_routing.yaml") as f:
            config = yaml.safe_load(f)
        presets = config.get("model_presets", {})
        for model, preset in presets.items():
            print(f"\n  {model}")
            print(f"    Context: {preset.get('context_window','?'):,} → {preset.get('max_output','?'):,}")
            print(f"    Params: {preset.get('params_total','?')} ({preset.get('params_active','?')} active)")
            print(f"    Tools: {preset.get('tool_strategy','?')}, max_tc={preset.get('max_tool_calls','?')}")
            print(f"    Temp: {preset.get('temperature','?')}, timeout: {preset.get('timeout','?')}s")


# ─────────────────────────────────────────────────────────────
# TOOLS
# ─────────────────────────────────────────────────────────────

def cmd_tools(args):
    import yaml
    with open(ROOT / "model_routing.yaml") as f:
        config = yaml.safe_load(f)
    roles = config.get("roles", {})
    print("Herramientas por rol:")
    for role, cfg in roles.items():
        model = cfg.get("model", "?")
        provider = cfg.get("provider", "?")
        print(f"  {role}: {model} ({provider})")

    # MCP tools
    mcp_config = ROOT / "mcp_config.json"
    if mcp_config.exists():
        with open(mcp_config) as f:
            mcp = json.load(f)
        servers = mcp.get("servers", {}) if isinstance(mcp, dict) else {}
        if servers:
            print(f"\n  MCP servers ({len(servers)}):")
            for name, cfg in servers.items():
                print(f"    {name}: {cfg.get('command', cfg.get('url', '?'))}")


# ─────────────────────────────────────────────────────────────
# MCP
# ─────────────────────────────────────────────────────────────

def cmd_mcp(args):
    mcp_config = ROOT / "mcp_config.json"
    if args.action == "list":
        if not mcp_config.exists():
            print("No hay mcp_config.json")
            return
        with open(mcp_config) as f:
            mcp = json.load(f)
        servers = mcp.get("servers", {}) if isinstance(mcp, dict) else {}
        if not servers:
            print("No hay servidores MCP configurados.")
            return
        for name, cfg in servers.items():
            transport = "stdio" if "command" in cfg else "http" if "url" in cfg else "?"
            print(f"  {name} ({transport})")
            print(f"    → {cfg.get('command', cfg.get('url', '?'))}")
    elif args.action in ("add", "remove"):
        print("Edita mcp_config.json directamente:")
        print(f"  vcore config edit --file mcp_config.json")


# ─────────────────────────────────────────────────────────────
# GATE
# ─────────────────────────────────────────────────────────────

def cmd_gate(args):
    if args.action == "rules":
        with open(ROOT / "gate_rules.yaml") as f:
            print(f.read())
    elif args.action == "log":
        limit = args.limit or 20
        rows = _db_query(
            "SELECT timestamp, agent_id, tool, nivel, auto_approved, reason "
            "FROM gate_log ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        for r in rows:
            auto = "AUTO" if r["auto_approved"] else "REQ"
            ts = time.strftime("%H:%M:%S", time.localtime(r["timestamp"]))
            print(f"  [{ts}] {auto} {r['agent_id']}.{r['tool']} → {r['nivel']} | {r['reason'][:80]}")


# ─────────────────────────────────────────────────────────────
# APPROVALS
# ─────────────────────────────────────────────────────────────

def cmd_approvals(args):
    if args.action == "list":
        approvals = _api("/approvals")
        if not approvals:
            print("No hay aprobaciones pendientes.")
            return
        for a in approvals:
            print(f"  [{a['id']}] {S.label(a.get('agent_id','?'))} {S.dim('->')} {a.get('tool','?')}")
            print(f"       {a.get('reason','?')[:100]}")
            print(f"       {S.dim('status:')} {a.get('status','?')}")
    elif args.action == "approve":
        _api(f"/approvals/{args.approval_id}/approve", method="POST")
        print(f"{S.OK} approval {args.approval_id} {S.dim('aprobada')}")
    elif args.action == "reject":
        _api(f"/approvals/{args.approval_id}/reject", method="POST")
        print(f"{S.OK} approval {args.approval_id} {S.dim('rechazada')}")


# ─────────────────────────────────────────────────────────────
# MEMORY
# ─────────────────────────────────────────────────────────────

def cmd_memory(args):
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(ROOT / "chroma_db"))
        collections = client.list_collections()

        if args.action == "status":
            print(f"ChromaDB: {len(collections)} colecciones")
            for c in collections:
                print(f"  {c.name}: {c.count()} documentos")
        elif args.action == "search":
            if not collections:
                print("No hay colecciones en ChromaDB.")
                return
            col = collections[0]
            results = col.query(query_texts=[args.query], n_results=5)
            for i, (doc_id, doc) in enumerate(zip(
                results.get("ids", [[]])[0],
                results.get("documents", [[]])[0]
            )):
                print(f"\n  [{i+1}] {doc_id}")
                print(f"  {doc[:200]}")
        elif args.action == "stats":
            import os
            size = 0
            for root_dir, _, files in os.walk(ROOT / "chroma_db"):
                for f in files:
                    size += os.path.getsize(os.path.join(root_dir, f))
            print(f"  Colecciones: {len(collections)}")
            for c in collections:
                print(f"    {c.name}: {c.count()} docs")
            print(f"  Tamaño total: {size:,} bytes")
    except ImportError:
        print("chromadb no instalado en este entorno.")
    except Exception as e:
        print(f"Error accediendo a ChromaDB: {e}")


# ─────────────────────────────────────────────────────────────
# LOGS
# ─────────────────────────────────────────────────────────────

def cmd_logs(args):
    limit = args.limit or 20
    if args.source == "gate":
        cmd_gate(argparse.Namespace(action="log", limit=limit))
    elif args.source == "llm":
        rows = _db_query(
            "SELECT id, provider, model, tokens_in, tokens_out, estimated_cost, "
            "agent_name, circuit_state, timestamp "
            "FROM llm_usage_log ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        _print_table(rows, ["id", "provider", "model", "tokens_in", "tokens_out", "estimated_cost", "circuit_state"])
    elif args.source == "traces":
        traces_file = ROOT / "traces.jsonl"
        if not traces_file.exists():
            print("No hay traces.jsonl")
            return
        with open(traces_file) as f:
            lines = f.readlines()
        for line in lines[-limit:]:
            try:
                obj = json.loads(line)
                ts = obj.get("timestamp", "?")
                agent = obj.get("agent", "?")
                action = obj.get("action", "?")
                print(f"  [{ts}] {agent}.{action}")
            except json.JSONDecodeError:
                pass
    elif args.source == "circuit":
        status = _api("/llm/circuit-status")
        _print_table(status, ["provider", "state", "failure_count", "last_failure"])


# ─────────────────────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────────────────────

def cmd_db(args):
    if args.action == "stats":
        rows = _db_query(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        print(f"{'Tabla':30} {'Rows':>8}")
        print(f"{'-'*30} {'-'*8}")
        total = 0
        for r in rows:
            t = r["name"]
            count = _db_query(f"SELECT COUNT(*) as n FROM \"{t}\"")[0]["n"]
            total += count if t != "sqlite_sequence" else 0
            print(f"  {t:30} {count:>8,}")
        print(f"  {'─'*38}")
        print(f"  {'Total (sin sqlite_sequence)':30} {total:>8,}")
    elif args.action == "tables":
        rows = _db_query(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        for r in rows:
            t = r["name"]
            cols = _db_query(f"PRAGMA table_info(\"{t}\")")
            col_names = [c["name"] for c in cols]
            count = _db_query(f"SELECT COUNT(*) as n FROM \"{t}\"")[0]["n"]
            print(f"\n  {t} ({count} rows)")
            print(f"    {', '.join(col_names)}")


# ─────────────────────────────────────────────────────────────
# SERVE
# ─────────────────────────────────────────────────────────────

def cmd_serve(args):
    import uvicorn
    print(f"[V-CORE] {S.dim('iniciando servidor en')} {args.host}:{args.port}")
    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


# ─────────────────────────────────────────────────────────────
# DOCTOR
# ─────────────────────────────────────────────────────────────

def cmd_doctor(args):
    """Diagnostico completo del sistema."""
    print(f"""
{S.BOLD}{S.CYAN}  vcore doctor{S.RESET} {S.DIM}v{_VERSION}{S.RESET}
{S.DIM}  diagnostico completo del sistema{S.RESET}
""")
    sections = []

    # 1. API
    try:
        health = _api("/health")
        sections.append((f"{S.label('api')}", f"{S.OK} {health.get('status')} (v{health.get('version','?')})"))
    except SystemExit as e:
        sections.append((f"{S.label('api')}", f"{S.FAIL} {e}"))

    # 2. DB
    db_path = ROOT / "vcore.db"
    if db_path.exists():
        size = db_path.stat().st_size
        tables = _db_query("SELECT name FROM sqlite_master WHERE type='table'")
        empty = []
        for t in tables:
            n = _db_query(f"SELECT COUNT(*) as n FROM \"{t['name']}\"")[0]["n"]
            if n == 0 and t["name"] != "sqlite_sequence":
                empty.append(t["name"])
        db_status = f"{S.OK} {size:,} bytes, {len(tables)} tablas"
        if empty:
            db_status += f"\n     {S.WARN} tablas vacias: {', '.join(empty)}"
        sections.append((f"{S.label('vcore.db')}", db_status))
    else:
        sections.append((f"{S.label('vcore.db')}", f"{S.FAIL} no encontrado"))

    # 3. ChromaDB
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(ROOT / "chroma_db"))
        cols = client.list_collections()
        total = sum(c.count() for c in cols)
        sections.append((f"{S.label('chromadb')}", f"{S.OK} {len(cols)} colecciones, {total} documentos"))
    except Exception as e:
        sections.append((f"{S.label('chromadb')}", f"{S.FAIL} {e}"))

    # 4. Modelos
    try:
        mi = _api("/system/model")
        lead = mi.get("enlil_lead", {})
        council = mi.get("enlil_council", {})
        sections.append((f"{S.label('modelos')}",
            f"{S.dim('lead:')} {lead.get('model','?')} {S.dim('via')} {lead.get('provider','?')} {S.dim('cb:')} {lead.get('circuit_breaker','?')}\n"
            f"     {S.dim('council:')} {council.get('model','?')} {S.dim('via')} {council.get('provider','?')} {S.dim('cb:')} {council.get('circuit_breaker','?')}"))
    except Exception as e:
        sections.append((f"{S.label('modelos')}", f"{S.FAIL} {e}"))

    # 5. Circuit breakers
    try:
        cb = _api("/llm/circuit-status")
        lines = []
        for provider, info in cb.items():
            state = info.get("state", "?")
            icon = S.OK if state == "CLOSED" else (S.WARN if state == "HALF_OPEN" else S.FAIL)
            lines.append(f"  {icon} {provider}: {state}")
        sections.append((f"{S.label('circuit breakers')}", "\n".join(lines)))
    except Exception:
        sections.append((f"{S.label('circuit breakers')}", f"{S.FAIL} no disponible"))

    # 6. Archivos clave
    checks = [
        (".env", "variables de entorno"),
        ("model_routing.yaml", "routing de modelos"),
        ("gate_rules.yaml", "reglas del gate"),
        ("mcp_config.json", "config MCP"),
        ("AGENTS.md", "registro de agentes"),
    ]
    lines = []
    for fname, desc in checks:
        path = ROOT / fname
        icon = S.OK if path.exists() else S.FAIL
        lines.append(f"  {icon} {S.label(fname)} {S.dim(f'({desc})')}")
    sections.append((f"{S.label('archivos')}", "\n".join(lines)))

    # 7. Entorno
    pp = os.environ.get("PYTHONPATH", "")
    if pp and "hermes" in pp.lower():
        env_status = f"{S.WARN} PYTHONPATH contaminado con hermes\n     {S.dim(pp[:120])}"
    else:
        env_status = f"{S.OK} PYTHONPATH={pp if pp else '(vacio)'}"
    sections.append((f"{S.label('entorno')}", env_status))

    # Render
    for label, content in sections:
        print(f"\n{label}")
        print(content)


# ─────────────────────────────────────────────────────────────
# INIT / STATUS / AUDIT
# ─────────────────────────────────────────────────────────────

def cmd_init(args):
    """Inicializa DB y workspace."""
    db_init = ROOT / "scripts" / "init_db.py"
    if db_init.exists():
        print(f"[V-CORE] {S.dim('inicializando DB...')}")
        subprocess.run([sys.executable, str(db_init)], check=False)
    (ROOT / "workspace").mkdir(exist_ok=True)
    print(f"[V-CORE] {S.OK}")


def cmd_status(args):
    """Estado rápido del sistema."""
    from api.version import VERSION
    cli_version = VERSION
    status = {
        "version": cli_version,
        "root": str(ROOT),
        "api": API_BASE,
        "agents": ["ENLIL", "ENKI", "SHAMASH", "NISABA"],
    }
    try:
        health = _api("/health")
        status["server"] = health.get("status", "?")
        status["server_version"] = health.get("version", "?")
    except Exception:
        status["server"] = "DOWN"

    rows = _db_query("SELECT COUNT(*) as n FROM task_graphs")
    status["task_graphs"] = rows[0]["n"] if rows else 0
    rows = _db_query("SELECT COUNT(*) as n FROM agent_execution")
    status["executions"] = rows[0]["n"] if rows else 0
    rows = _db_query("SELECT COUNT(*) as n FROM llm_usage_log")
    status["llm_calls"] = rows[0]["n"] if rows else 0

    print(json.dumps(status, indent=2, ensure_ascii=False))


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="vcore",
        description="V-CORE CLI — sistema multi-agente con frontend web",
    )
    sub = parser.add_subparsers(dest="command")

    # ── chat ──
    p = sub.add_parser("chat", help="Chat con ENLIL")
    p.add_argument("-q", "--query", help="Mensaje one-shot")
    p.add_argument("-c", "--continue", dest="continue_", action="store_true",
                   help="Continuar última sesión")
    p.add_argument("-r", "--resume", help="Resumir sesión por ID")
    p.set_defaults(func=cmd_chat)

    # ── sessions ──
    p = sub.add_parser("sessions", help="Gestionar sesiones")
    sp = p.add_subparsers(dest="action")
    sp.add_parser("list", help="Listar sesiones recientes")
    rp = sp.add_parser("resume", help="Resumir sesión")
    rp.add_argument("session_id")
    dp = sp.add_parser("delete", help="Eliminar sesión")
    dp.add_argument("session_id")
    ep = sp.add_parser("export", help="Exportar sesiones a JSONL")
    ep.add_argument("--file", help="Archivo de salida")
    ep.add_argument("--limit", type=int, default=50, help="Máximo de sesiones")
    p.set_defaults(func=cmd_sessions)

    # ── agents ──
    p = sub.add_parser("agents", help="Control multi-agente")
    sp = p.add_subparsers(dest="action")
    sp.add_parser("list", help="Listar agentes")
    sp.add_parser("status", help="Estado de agentes")
    ip = sp.add_parser("invoke", help="Invocar agente específico")
    ip.add_argument("agent_name")
    ip.add_argument("message")
    cp = sp.add_parser("council", help="Council mode")
    cp.add_argument("message")
    cp.add_argument("--models", help="Modelos (separados por coma)")
    p.set_defaults(func=cmd_agents)

    # ── config ──
    p = sub.add_parser("config", help="Configuración")
    sp = p.add_subparsers(dest="action")
    sp.add_parser("show", help="Ver configuración")
    ep = sp.add_parser("edit", help="Abrir en editor")
    ep.add_argument("--file", help="Archivo a editar")
    sp.add_parser("validate", help="Validar YAMLs")
    sp.add_parser("path", help="Mostrar rutas")
    p.set_defaults(func=cmd_config)

    # ── models ──
    p = sub.add_parser("models", help="Gestión de modelos")
    sp = p.add_subparsers(dest="action")
    sp.add_parser("list", help="Listar modelos")
    sp_switch = sp.add_parser("switch", help="Cambiar modelo")
    sp_switch.add_argument("role", help="Rol (lead, council, enki_plan, etc.)")
    sp_switch.add_argument("model", help="Modelo a usar")
    sp.add_parser("presets", help="Ver presets con context windows")
    p.set_defaults(func=cmd_models)

    # ── tools ──
    p = sub.add_parser("tools", help="Herramientas")
    sp = p.add_subparsers(dest="action")
    sp.add_parser("list", help="Listar herramientas")
    p.set_defaults(func=cmd_tools)

    # ── mcp ──
    p = sub.add_parser("mcp", help="Servidores MCP")
    sp = p.add_subparsers(dest="action")
    sp.add_parser("list", help="Listar servidores")
    ap = sp.add_parser("add", help="Agregar servidor")
    ap.add_argument("name")
    rp = sp.add_parser("remove", help="Remover servidor")
    rp.add_argument("name")
    p.set_defaults(func=cmd_mcp)

    # ── gate ──
    p = sub.add_parser("gate", help="Security Gate")
    sp = p.add_subparsers(dest="action")
    sp.add_parser("rules", help="Mostrar reglas")
    lp = sp.add_parser("log", help="Ver decisiones recientes")
    lp.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_gate)

    # ── approvals ──
    p = sub.add_parser("approvals", help="Cola de aprobación")
    sp = p.add_subparsers(dest="action")
    sp.add_parser("list", help="Ver pendientes")
    ap = sp.add_parser("approve", help="Aprobar")
    ap.add_argument("approval_id", type=int)
    rp = sp.add_parser("reject", help="Rechazar")
    rp.add_argument("approval_id", type=int)
    p.set_defaults(func=cmd_approvals)

    # ── memory ──
    p = sub.add_parser("memory", help="Memoria semántica")
    sp = p.add_subparsers(dest="action")
    sp.add_parser("status", help="Estado de ChromaDB")
    sp_search = sp.add_parser("search", help="Buscar en memoria")
    sp_search.add_argument("query")
    sp.add_parser("stats", help="Estadísticas de colecciones")
    p.set_defaults(func=cmd_memory)

    # ── logs ──
    p = sub.add_parser("logs", help="Observabilidad")
    sp = p.add_subparsers(dest="source")
    lp = sp.add_parser("gate", help="Log del gate")
    lp.add_argument("--limit", type=int, default=20)
    lp = sp.add_parser("llm", help="Log de uso de LLM")
    lp.add_argument("--limit", type=int, default=20)
    lp = sp.add_parser("traces", help="Trazas recientes")
    lp.add_argument("--limit", type=int, default=20)
    sp.add_parser("circuit", help="Circuit breakers")
    p.set_defaults(func=cmd_logs)

    # ── db ──
    p = sub.add_parser("db", help="Base de datos")
    sp = p.add_subparsers(dest="action")
    sp.add_parser("stats", help="Estadísticas")
    sp.add_parser("tables", help="Listar tablas y columnas")
    p.set_defaults(func=cmd_db)

    # ── serve ──
    p = sub.add_parser("serve", help="Levantar servidor API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=_port())
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    # ── doctor ──
    p = sub.add_parser("doctor", help="Diagnóstico completo")
    p.set_defaults(func=cmd_doctor)

    # ── init ──
    p = sub.add_parser("init", help="Inicializar DB y workspace")
    p.set_defaults(func=cmd_init)

    # ── status ──
    p = sub.add_parser("status", help="Estado rápido")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    if not hasattr(args, 'func'):
        # Sin comando: entrar al chat interactivo (como `claude` sin args)
        cmd_chat(argparse.Namespace(query=None, continue_=False, resume=None))
    else:
        args.func(args)


if __name__ == "__main__":
    main()
