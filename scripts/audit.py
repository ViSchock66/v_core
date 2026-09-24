#!/usr/bin/env python3
"""
scripts/audit.py — Auditoría dual de V-CORE (estática + runtime)
===============================================================
Corre en frío (sin server) y en caliente (contra el backend vivo).

El puerto se toma de `api/ports.py` (fuente única). Antes estaba fijado a 8001
mientras el resto del proyecto usaba 8000, así que la fase runtime siempre
reportaba "server caído" contra un servidor que estaba perfectamente arriba.

Uso:
    python scripts/audit.py              # ambas fases (si server responde)
    python scripts/audit.py --static     # solo fase estática
    python scripts/audit.py --runtime    # solo fase runtime
    python scripts/audit.py --json       # salida JSON para CI/parseo

Salida: semáforo por check (✅/❌) + detalle. Exit code 0 si todo pasa.
"""

import json, os, sys, time, re, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from api.ports import api_base as _api_base

SERVER_URL = _api_base()

# ── Helpers ──────────────────────────────────────────────────────────────────

def ok(msg: str) -> str:
    return f"  ✅ {msg}"

def fail(msg: str) -> str:
    return f"  ❌ {msg}"

def warn(msg: str) -> str:
    return f"  🟡 {msg}"

def _get(path: str) -> tuple[int, str]:
    """GET request, returns (status_code, body)."""
    try:
        req = urllib.request.Request(f"{SERVER_URL}{path}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode() if e.fp else ""
    except Exception as e:
        return 0, str(e)

def _post_json(path: str, data: dict) -> tuple[int, str]:
    try:
        body = json.dumps(data).encode()
        req = urllib.request.Request(f"{SERVER_URL}{path}", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode() if e.fp else ""
    except Exception as e:
        return 0, str(e)

def _line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except Exception:
        return -1

# ── Static audit ─────────────────────────────────────────────────────────────

def static_audit() -> list[str]:
    """Audita la consistencia documental y de configuración SIN server."""
    lines: list[str] = []
    errors = 0

    lines.append("═══ STATIC AUDIT (offline) ═══")

    # ── 1. Archivos referenciados en STATE que existen ──
    lines.append("\n── 1. VCORE_STATE.json: referencias a archivos ──")
    state_path = ROOT / "VCORE_STATE.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))

    checks = [
        ("ultimo_roadmap", state.get("ultimo_roadmap", "")),
        ("ultima_arquitectura", state.get("ultima_arquitectura", "")),
        ("ultima_sesion", state.get("ultima_sesion", "")),
    ]
    for field, val in checks:
        if not val:
            lines.append(warn(f"STATE.{field} = '' (vacío)"))
            continue
        # Estos campos pueden ser nombres sin extensión; probar .md y sin ext
        candidates = [
            ROOT / val,
            ROOT / f"{val}.md",
            ROOT / "docs" / val,
            ROOT / "docs" / f"{val}.md",
            ROOT / val,  # path absoluto relativo a ROOT
        ]
        # Si ya tiene path docs/ o similar
        if "/" in val or "\\" in val:
            candidates = [ROOT / val]
        found = any(c.exists() for c in candidates)
        if found:
            lines.append(ok(f"STATE.{field} = '{val}' → archivo existe"))
        else:
            lines.append(fail(f"STATE.{field} = '{val}' → NO EXISTE en disco"))
            errors += 1

    # También ver si ultimo_roadmap tiene número de versión (viejo patrón)
    roadmap_val = state.get("ultimo_roadmap", "")
    if re.search(r'v\d', roadmap_val):
        lines.append(fail(f"STATE.ultimo_roadmap contiene número de versión '{roadmap_val}' — renombrar a nombre canónico sin versión (principle 26)"))

    # ── 2. Model presets consistency ──
    lines.append("\n── 2. model_routing.yaml: roles vs presets ──")
    try:
        import yaml
        routing = yaml.safe_load((ROOT / "model_routing.yaml").read_text(encoding="utf-8"))
    except Exception:
        lines.append(fail("No se pudo cargar model_routing.yaml (falta pyyaml?)"))
        routing = {}

    roles = routing.get("roles", {})
    presets = set(routing.get("model_presets", {}).keys())
    roles_models = {r["model"] for r in roles.values()}

    missing_presets = roles_models - presets
    if missing_presets:
        for m in sorted(missing_presets):
            # Encontrar qué rol usa este modelo
            using_roles = [r for r, cfg in roles.items() if cfg["model"] == m]
            lines.append(fail(f"Modelo '{m}' sin preset en model_presets (usado por: {', '.join(using_roles)})"))
            errors += 1
    else:
        lines.append(ok("Todos los modelos en roles tienen preset en model_presets"))

    # ── 3. Providers en roles vs providers registrados en circuit breakers ──
    lines.append("\n── 3. Providers: roles vs fallback_chains ──")
    providers_in_roles = {r["provider"] for r in roles.values()}
    providers_in_fallbacks = set(routing.get("fallback_chains", {}).keys())
    # providers que usan el alias genérico 'nvidia' (se resuelve en runtime vía _model_to_provider)
    nvidia_alias_roles = {role: cfg for role, cfg in roles.items() if cfg["provider"] == "nvidia"}

    for role, cfg in sorted(nvidia_alias_roles.items()):
        model = cfg["model"]
        lines.append(warn(f"Rol '{role}' usa provider='nvidia' (alias genérico) → se resuelve en runtime por modelo '{model}'"))

    unknown_providers = providers_in_roles - providers_in_fallbacks - {"nvidia", "ollama"}
    if unknown_providers:
        for p in sorted(unknown_providers):
            lines.append(fail(f"Provider '{p}' usado en roles pero sin fallback_chain"))
            errors += 1

    # ── 4. Port hardcoding ──
    lines.append("\n── 4. Puerto canónico: detección de hardcodes ──")
    ports_found: dict[int, list[str]] = {}
    for ext in ["*.py", "*.md", "*.json", "*.yaml"]:
        for f in ROOT.glob(f"**/{ext}"):
            if "__pycache__" in str(f) or ".venv" in str(f) or "node_modules" in str(f):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for match in re.finditer(r':800[01]\b', content):
                port = int(match.group()[1:])
                rel = str(f.relative_to(ROOT))
                ports_found.setdefault(port, []).append(rel)

    for port, files in sorted(ports_found.items()):
        unique_files = sorted(set(files))
        if len(unique_files) <= 3:
            lines.append(warn(f"Puerto {port} referenciado en: {', '.join(unique_files)}"))
        else:
            lines.append(warn(f"Puerto {port} referenciado en {len(unique_files)} archivos"))

    # ── 5. Version consistency ──
    lines.append("\n── 5. Consistencia de versión ──")
    expected = state.get("version", "0.0.0")
    version_pattern = re.compile(r'"v?(\d+\.\d+\.\d+)"')
    hardcoded_versions: dict[str, list[str]] = {}
    for ext in ["*.py", "*.md", "*.json"]:
        for f in ROOT.glob(f"**/{ext}"):
            if "__pycache__" in str(f) or ".venv" in str(f) or "node_modules" in str(f):
                continue
            if f.name == "VCORE_STATE.json":
                continue  # es la fuente
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for match in version_pattern.finditer(content):
                ver = match.group(1)
                if ver != expected:
                    hardcoded_versions.setdefault(ver, []).append(str(f.relative_to(ROOT)))

    if hardcoded_versions:
        for ver, files in sorted(hardcoded_versions.items()):
            lines.append(fail(f"Versión '{ver}' hardcodeada (esperada '{expected}'): {', '.join(files[:5])}"))
            errors += 1
    else:
        lines.append(ok(f"Todas las versiones coinciden con '{expected}'"))

    # ── 6. scripts_operativos vs disco ──
    lines.append("\n── 6. Scripts operativos vs disco ──")
    declared = set(state.get("scripts_operativos", []))
    on_disk = {f.name for f in (ROOT / "scripts").glob("*.py") if f.is_file()}
    missing = declared - on_disk
    undeclared = on_disk - declared
    if missing:
        lines.append(fail(f"Declarados en STATE pero no en disco: {missing}"))
        errors += 1
    if undeclared:
        lines.append(warn(f"En disco pero no en STATE.scripts_operativos: {undeclared}"))
    if not missing and not undeclared:
        lines.append(ok("scripts_operativos coincide con disco"))

    # ── 7. AGENTS.md consistency ──
    lines.append("\n── 7. AGENTS.md vs realidad ──")
    agents_md = ROOT / "AGENTS.md"
    if agents_md.exists():
        content = agents_md.read_text(encoding="utf-8")
        # Puerto: la fuente unica es api/ports.py. Antes este check exigia
        # 8001 mientras el codigo usaba 8000, y la contradiccion se quedo ahi.
        try:
            from api.ports import port as _canon_port
            canon = _canon_port()
        except Exception:
            canon = 8000
        if f":{canon}" in content:
            lines.append(ok(f"AGENTS.md referencia el puerto canonico {canon}"))
        else:
            lines.append(fail(f"AGENTS.md no referencia el puerto canonico {canon} "
                              f"(fuente unica: api/ports.py)"))
            errors += 1
        # V-Core skill path
        if "vcore-app-config" in content or "/vcore" in content:
            lines.append(ok("AGENTS.md referencia skill vcore"))
        else:
            lines.append(warn("AGENTS.md no referencia skill vcore"))
        # Archivos inmunes
        if "gate.py" in content:
            lines.append(ok("AGENTS.md lista archivos inmunes"))
    else:
        lines.append(fail("AGENTS.md no existe"))
        errors += 1

    # ── 8. DOCUMENTATION.md: ¿los docs vigentes existen? ──
    lines.append("\n── 8. DOCUMENTATION.md: vigentes vs disco ──")
    doc_md = ROOT / "docs" / "DOCUMENTATION.md"
    if doc_md.exists():
        # Extraer referencias a archivos .md de la tabla de vigentes
        doc_content = doc_md.read_text(encoding="utf-8")
        refs = re.findall(r'`\.\./([^`]+\.md)`|`([^`]+\.md)`', doc_content)
        for g1, g2 in refs:
            fname = g1 or g2
            path = ROOT / fname if not fname.startswith("docs/") else ROOT / fname
            if not fname.startswith("docs/"):
                path = ROOT / "docs" / fname
            # Intentar varias ubicaciones
            candidates = [ROOT / fname, ROOT / "docs" / fname]
            found = any(c.exists() for c in candidates)
            if found:
                lines.append(ok(f"Vigente: {fname} → existe"))
            else:
                # Podría ser agents/*/*.md, verificar glob
                if "*" in fname:
                    lines.append(ok(f"Vigente: {fname} → patrón glob, ok"))
                else:
                    lines.append(warn(f"Vigente: {fname} → no encontrado (puede estar en otra ubicación)"))
    else:
        lines.append(warn("DOCUMENTATION.md no encontrado"))

    lines.append(f"\n── Resultado estático: {'✅ TODO OK' if errors == 0 else f'❌ {errors} errores'} ──")
    return lines


# ── Runtime audit ─────────────────────────────────────────────────────────────

def runtime_audit() -> list[str]:
    """Audita el sistema VIVO contra el server en SERVER_URL."""
    lines: list[str] = []
    errors = 0

    lines.append(f"\n═══ RUNTIME AUDIT ({SERVER_URL}) ═══")

    # ── 0. Server alive ──
    lines.append("\n── 0. Server ping ──")
    code, body = _get("/health")
    if code == 200:
        try:
            data = json.loads(body)
            lines.append(ok(f"/health → {data.get('status')} (v{data.get('version')})"))
        except Exception:
            lines.append(ok(f"/health → 200 OK"))
    else:
        lines.append(fail(f"/health → {code} — server no responde"))
        errors += 1
        lines.append("\n── Runtime ABORTADO: server no disponible ──")
        return lines

    # ── 1. /system/model → cruzar con model_routing.yaml ──
    lines.append("\n── 1. /system/model vs model_routing.yaml ──")
    code, body = _get("/system/model")
    if code != 200:
        lines.append(fail(f"/system/model → {code}"))
        errors += 1
    else:
        try:
            import yaml
            routing = yaml.safe_load((ROOT / "model_routing.yaml").read_text(encoding="utf-8"))
        except Exception:
            routing = {}
        runtime_models = json.loads(body)
        yaml_roles = routing.get("roles", {})
        for role, info in runtime_models.items():
            yaml_info = yaml_roles.get(role, {})
            if not yaml_info:
                lines.append(warn(f"Rol '{role}' en runtime pero NO en model_routing.yaml"))
                continue
            if info.get("model") != yaml_info.get("model"):
                lines.append(fail(f"Rol '{role}': runtime={info.get('model')} vs YAML={yaml_info.get('model')}"))
                errors += 1
            else:
                cw = info.get("context_window", 0)
                tool = info.get("tool_strategy", "?")
                if cw == 0:
                    lines.append(fail(f"Rol '{role}' ({info.get('model')}): context_window=0 — falta preset"))
                    errors += 1
                else:
                    lines.append(ok(f"Rol '{role}': {info.get('model')} [{info.get('provider')}] ctx={cw} tool={tool}"))

    # ── 2. /llm/providers → circuit breakers ──
    lines.append("\n── 2. /llm/providers — circuit breakers ──")
    code, body = _get("/llm/providers")
    if code != 200:
        lines.append(fail(f"/llm/providers → {code}"))
        errors += 1
    else:
        prov_data = json.loads(body)
        circuit = prov_data.get("circuit_status", {})
        open_cbs = {k: v for k, v in circuit.items() if isinstance(v, dict) and v.get("state") == "OPEN"}
        if open_cbs:
            for cb, st in open_cbs.items():
                lines.append(fail(f"Circuit breaker OPEN: {cb}"))
                errors += 1
        else:
            lines.append(ok(f"{len(circuit)} circuit breakers, todos CLOSED"))

        # Verificar que providers de roles están en circuit_status
        # Resolver alias 'nvidia' → mapeo conocido de llm_client.py
        model_to_provider = {
            "z-ai/glm-5.2": "nvidia-lead", "z-ai/glm-5.1": "nvidia-lead",
            "moonshotai/kimi-k2.6": "nvidia-kimi",
            "nvidia/nemotron-3-ultra-550b-a55b": "nvidia-nemotron",
            "nvidia/llama-3.3-nemotron-super-49b-v1": "nvidia-nemotron",
            "deepseek-ai/deepseek-v4-flash": "nvidia-deepseek",
            "deepseek-ai/deepseek-v4-pro": "nvidia-deepseek",
            "mistralai/mistral-large-3-675b-instruct-2512": "nvidia-mistral-large",
            "meta/llama-3.2-90b-vision-instruct": "nvidia-mistral-large",
        }
        for role_name, role_cfg in routing.get("roles", {}).items():
            provider = role_cfg["provider"]
            if provider == "nvidia":
                provider = model_to_provider.get(role_cfg["model"], "nvidia-lead")
            if provider not in circuit and provider != "ollama":
                lines.append(warn(f"Provider '{provider}' (rol {role_name}) sin entry en circuit_status"))

    # ── 3. Frontend serving ──
    lines.append("\n── 3. Frontend (estáticos) ──")
    for fname in ["/", "/index.html", "/style.css", "/app.js"]:
        code, body = _get(fname)
        size = len(body)
        icon = ok if code == 200 and size > 10 else fail
        if code == 200 and size > 10:
            lines.append(ok(f"{fname} → 200 ({size}B)"))
        else:
            lines.append(fail(f"{fname} → {code} ({size}B) — frontend roto"))
            errors += 1

    # ── 4. Session count vs STATE ──
    lines.append("\n── 4. Sesiones (DB vs STATE) ──")
    code, body = _get("/sessions")
    if code == 200:
        sessions = json.loads(body)
        db_count = len(sessions)
        state_count = state = json.loads((ROOT / "VCORE_STATE.json").read_text(encoding="utf-8"))
        state_session_id = state.get("session_id", 0)
        lines.append(warn(f"DB: {db_count} sesiones abiertas  |  STATE.session_id: {state_session_id}"))
        if db_count > 20:
            lines.append(warn(f"  ⚠ {db_count} sesiones sin garbage collection"))
    else:
        lines.append(fail(f"/sessions → {code}"))
        errors += 1

    # ── 5. /system/health — detectar falsos positivos ──
    lines.append("\n── 5. /system/health (auto-diagnóstico) ──")
    code, body = _get("/system/health")
    if code == 200:
        try:
            health_data = json.loads(body)
            health_flags = health_data.get("health", {})
            api_ok = health_flags.get("api", {}).get("ok", False)
            frontend_ok = health_flags.get("frontend", {}).get("ok", False)
            ollama_ok = health_flags.get("ollama", {}).get("ok", False)

            # Verificar falsos positivos
            if not api_ok:
                lines.append(fail("❌ /system/health dice 'API no responde' → FALSO POSITIVO (la API respondió este mismo chequeo)"))
                errors += 1
            else:
                lines.append(ok("API: ok"))

            if not frontend_ok:
                lines.append(warn("Frontend: /system/health dice 'no responde' — posible puerto wrong (usa :8000)"))

            if not ollama_ok:
                lines.append(warn("Ollama: no responde (esperable si no hay Ollama local)"))

            # Alertas
            alerts = health_data.get("alerts", [])
            for a in alerts:
                sev = a.get("severity", "?")
                lines.append(warn(f"Alerta [{sev}]: {a.get('message', '?')}"))

        except json.JSONDecodeError:
            lines.append(fail("/system/health no retornó JSON válido"))
            errors += 1
    else:
        lines.append(fail(f"/system/health → {code}"))
        errors += 1

    # ── 6. Ping modelo lead ──
    lines.append("\n── 6. Ping lead model (GLM 5.2) ──")
    code, body = _post_json("/agents/route", {
        "session_dir": "",
        "message": "responde solo: ok",
        "temperature": 0.3,
        "stream": False,
    })
    if code == 200 and "ok" in body.lower():
        lines.append(ok("Lead model responde: ok"))
    elif code == 200:
        lines.append(warn(f"Lead model respondió pero sin 'ok' explícito: {body[:100]}"))
    else:
        lines.append(fail(f"Lead model → {code}: {body[:100]}"))
        errors += 1

    # ── 7. Provider 'nvidia' alias resolution ──
    lines.append("\n── 7. Provider 'nvidia' alias en runtime ──")
    code, body = _get("/system/model")
    if code == 200:
        runtime_models = json.loads(body)
        for role, info in runtime_models.items():
            if info.get("provider") == "nvidia":
                lines.append(warn(f"Runtime retorna provider='nvidia' para rol '{role}' (¿no se resolvió el alias?)"))

    lines.append(f"\n── Resultado runtime: {'✅ TODO OK' if errors == 0 else f'❌ {errors} errores'} ──")
    return lines


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="V-CORE Dual Audit")
    parser.add_argument("--static", action="store_true", help="Solo fase estática")
    parser.add_argument("--runtime", action="store_true", help="Solo fase runtime")
    parser.add_argument("--json", action="store_true", help="Salida JSON")
    args = parser.parse_args()

    run_static = args.static or not args.runtime
    run_runtime = args.runtime or not args.static

    all_lines: list[str] = []

    if run_static:
        all_lines.extend(static_audit())

    if run_runtime:
        all_lines.extend(runtime_audit())

    if args.json:
        # Parsear resultados en JSON
        results = {"checks": [], "errors": 0}
        for line in all_lines:
            if line.startswith("  ✅"):
                results["checks"].append({"status": "pass", "message": line[5:]})
            elif line.startswith("  ❌"):
                results["checks"].append({"status": "fail", "message": line[5:]})
                results["errors"] += 1
            elif line.startswith("  🟡"):
                results["checks"].append({"status": "warn", "message": line[5:]})
        results["summary"] = "PASS" if results["errors"] == 0 else f"FAIL ({results['errors']} errors)"
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print("\n".join(all_lines))

    # Exit code
    error_count = sum(1 for l in all_lines if l.startswith("  ❌"))
    sys.exit(0 if error_count == 0 else 1)


if __name__ == "__main__":
    main()
