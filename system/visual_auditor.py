#!/usr/bin/env python3
"""
V-CORE Visual Auditor — módulo compartido (consolidado).
Usado por: ENLIL agent loop (task_graph_engine) + CLI standalone (visual_auditor_v2.py).

Fase 1: Playwright DOM — detecta bugs estructurales (MISSING, consola errores, interacciones rotas).
Fase 2: NIM vision (council/escalation) — solo si Fase 1 no encontró bugs y el usuario pidió análisis visual.

Playwright sync es obligatorio: async_playwright() conflictúa con el event loop de uvicorn.
"""
from system.observability import get_tracer
import base64, json, re, time
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent
REPORT_DIR = BASE_DIR / "scripts" / "visual_audits"

# Prompt de verificación de bugs (instrucciones dinámicas)
_INSTRUCTION_PROMPT_TEMPLATE = (
    "You are a QA engineer inspecting a V-CORE frontend screenshot. "
    "INSTRUCCIONES DEL USUARIO: {instructions}\n\n"
    "Analyze the screenshot carefully. "
    "Describe every element you see in detail: colors, positions, text, icons, layout, spacing. "
    "If there are bugs, inconsistencies, missing elements, or styling issues, list them specifically. "
    "Return your analysis in JSON with keys: 'issues_found', 'elements_detected', 'analysis', 'recommendations'."
)

# Prompt general de fallback
_GENERAL_VISION_PROMPT = (
    "You are a QA engineer inspecting a V-CORE frontend screenshot. "
    "List every visual bug, missing icon, layout issue, spacing inconsistency, broken element you see. "
    "Be specific about positions and colors. "
    "Return JSON with keys: 'bugs', 'ok_elements', 'recommendations'."
)

# Elementos siempre visibles (si faltan → BUG real)
ALWAYS_VISIBLE_IDS = [
    "sidebar", "messages", "send-btn", "input-field",
    "conv-list", "rpanel", "sb-toggle", "rp-toggle",
]

# Elementos condicionalmente visibles (HIDDEN es normal en ciertos estados)
CONDITIONAL_IDS = [
    "activity", "file-tree", "monaco-editor", "conn-banner", "scroll-pill",
]

# Mapa de qué IDs deberían estar visibles en qué estado de la app
STATE_EXPECTED = {
    "empty": {  # Sin conversación activa
        "visible": ["sidebar", "messages", "send-btn", "input-field", "conv-list",
                     "rpanel", "sb-toggle", "rp-toggle"],
        "hidden_ok": ["activity", "file-tree", "monaco-editor", "conn-banner", "scroll-pill"],
    },
    "active": {  # Con conversación activa
        "visible": ["sidebar", "messages", "send-btn", "input-field", "conv-list",
                     "rpanel", "sb-toggle", "rp-toggle", "activity", "monaco-editor"],
        "hidden_ok": ["file-tree", "conn-banner", "scroll-pill"],
    },
}


def _detect_app_state(page) -> str:
    """Detecta si la app está en estado 'empty' o 'active'."""
    try:
        msg_area = page.query_selector("#messages")
        if msg_area:
            children = msg_area.query_selector_all(":scope > *")
            if len(children) > 1:
                return "active"
        return "empty"
    except Exception:
        return "empty"


def run_visual_audit(base_url: str | None = None,
                      analyze: bool = False,
                      screenshot_dir: Path | None = None,
                      instructions: str | None = None,
                      task_id: str = "",
                      vision_fallback: bool = True) -> dict:
    """
    Auditoría visual del frontend en 2 fases.

    FASE 1 — Playwright DOM: Screenshot + check estructural + interacciones + errores consola.
    Devuelve bugs reales (elementos MISSING que deberían estar) e info contextual.
    
    FASE 2 — NIM vision (council): Solo si:
    - analyze=True Y la Fase 1 no encontró bugs estructurales, O
    - El usuario pidió específicamente análisis visual con instrucciones
    
    Args:
        base_url: URL del frontend. Si es None, se resuelve desde api/ports.py.
        analyze: Si True, permite enviar screenshot a NIM vision
        screenshot_dir: Directorio para guardar screenshots
        instructions: Instrucciones del usuario para el análisis visual
        task_id: ID de tarea para tracing
        vision_fallback: Si True, usa NIM vision si DOM audit no encontró bugs

    Returns:
        dict con keys: status, screenshot, bugs, dom_info, interaction_audit, 
                       console_errors, vision_analysis, needs_vision
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"error": "Playwright no instalado", "status": "unavailable"}

    # Fuente unica del puerto (api/ports.py). Antes era fijo a 8000.
    if base_url is None:
        from api.ports import url as _api_url
        base_url = _api_url("/")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = screenshot_dir or REPORT_DIR
    out_dir.mkdir(exist_ok=True)

    bugs = []       # Only real bugs
    dom_info = {}   # Structural info (not bugs)
    interaction_audit = {}
    console_errors = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})

            # Capturar errores de consola
            page.on("console", lambda msg: console_errors.append(f"[{msg.type}] {msg.text}")
                    if msg.type in ("error", "warning") else None)

            # Navegar al frontend
            page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)

            # Screenshot
            screenshot_path = out_dir / f"audit_{ts}.png"
            page.screenshot(path=str(screenshot_path), full_page=False)

            # Detectar estado de la app
            app_state = _detect_app_state(page)
            dom_info["app_state"] = app_state

            # ── FASE 1: DOM Audit (contextual) ──
            expected = STATE_EXPECTED.get(app_state, STATE_EXPECTED["empty"])
            all_ids = list(set(ALWAYS_VISIBLE_IDS + CONDITIONAL_IDS))
            
            for eid in all_ids:
                el = page.query_selector(f"#{eid}")
                if not el:
                    status = "MISSING"
                elif not el.is_visible():
                    status = "HIDDEN"
                else:
                    status = "OK"
                
                dom_info[eid] = status
                
                # Solo reportar como BUG si el elemento debería estar visible y no lo está
                if eid in expected["visible"] and status == "MISSING":
                    bugs.append(f"BUG: #{eid} falta en el DOM pero debería estar visible (estado: {app_state})")
                elif eid in expected["visible"] and status == "HIDDEN":
                    bugs.append(f"BUG: #{eid} está oculto pero debería estar visible (estado: {app_state})")
                # HIDDEN de elementos condicionales = normal, no es bug
                elif eid in expected.get("hidden_ok", []) and status in ("HIDDEN", "MISSING"):
                    pass  # Expected, not a bug

            # Artifact cards — info, no bug
            cards = page.query_selector_all(".artifact-card")
            dom_info["artifact_cards"] = len(cards)

            # ── INTERACTION TESTS ──
            # Test 1: Right panel close
            rpanel = page.query_selector("#rpanel")
            rp_toggle = page.query_selector("#rp-toggle")
            if rpanel and rp_toggle:
                # Force panel open
                page.evaluate("""() => { const p = document.getElementById('rpanel'); if(p) { p.classList.add('open'); if(typeof rpOpen!=='undefined') rpOpen=true; } }""")
                page.wait_for_timeout(300)
                
                was_open = "open" in (rpanel.get_attribute("class") or "")
                try:
                    rp_toggle.click()
                    page.wait_for_timeout(500)
                    still_open = "open" in (rpanel.get_attribute("class") or "")
                    if still_open:
                        bugs.append("BUG: #rp-toggle click no cierra el panel derecho")
                        interaction_audit["rpanel_close"] = "BROKEN"
                    else:
                        interaction_audit["rpanel_close"] = "OK"
                except Exception as e:
                    bugs.append(f"BUG: #rp-toggle click error: {str(e)[:80]}")
                    interaction_audit["rpanel_close"] = f"ERROR: {e}"
            elif not rpanel:
                # rpanel missing is contextual — could be expected
                pass

            # Test 2: Toggle sidebar
            sb_toggle = page.query_selector("#sb-toggle")
            sidebar = page.query_selector("#sidebar")
            if sb_toggle and sidebar:
                was_collapsed = "collapsed" in (sidebar.get_attribute("class") or "")
                try:
                    sb_toggle.click()
                    page.wait_for_timeout(500)
                    is_collapsed = "collapsed" in (sidebar.get_attribute("class") or "")
                    changed = was_collapsed != is_collapsed
                    if not changed:
                        bugs.append("BUG: #sb-toggle click no alterna el sidebar")
                        interaction_audit["toggle_sb"] = "BROKEN"
                    else:
                        interaction_audit["toggle_sb"] = "OK"
                except Exception as e:
                    bugs.append(f"BUG: #sb-toggle click error: {e}")
                    interaction_audit["toggle_sb"] = f"ERROR: {e}"

            # Test 3: Console errors → real bugs
            for err in console_errors[:10]:
                if "[error]" in err:
                    bugs.append(f"BUG: Console error: {err[8:100]}")

            browser.close()

            # ── FASE 2: NIM vision (council/escalation) ──
            vision_result = None
            needs_vision = False
            
            # Solo usar NIM vision si:
            # - analyze=True (pedido por usuario o agente)
            # - Y (no se encontraron bugs DOM O hay instrucciones específicas del usuario)
            should_vision = analyze and (
                (vision_fallback and len(bugs) == 0)  # NoDOM bugs → escalamos a vision
                or (instructions and instructions.strip())  # Usuario pidió análisis específico
            )
            
            if should_vision and screenshot_path.exists():
                vision_instructions = instructions if instructions and instructions.strip() else _GENERAL_VISION_PROMPT
                vision_result = _analyze_with_vision(screenshot_path, instructions=vision_instructions)
                needs_vision = False  # Already used
            elif analyze and not should_vision and len(bugs) > 0:
                # DOM ya encontró bugs, no necesitamos vision
                needs_vision = False
            elif analyze and should_vision:
                needs_vision = True  # Would need but couldn't

            # Compute summary
            bug_count = len(bugs)
            
            return {
                "status": "completed",
                "screenshot": str(screenshot_path),
                "bugs": bugs,
                "bug_count": bug_count,
                # Alias consumido por los comandos /audit, /viz y el polish loop
                # de ENLIL, que leen `findings`. Estaba sin definir: esos comandos
                # hacian data.get("findings", []) y siempre obtenian lista vacia,
                # asi que reportaban "sin bugs" aunque hubiera hallazgos.
                "findings": bugs,
                "dom_info": dom_info,
                "interaction_audit": interaction_audit,
                "console_errors": console_errors[:10],
                "vision_analysis": vision_result,
                "needs_vision": needs_vision,
                "app_state": app_state,
            }

    except Exception as e:
        return {"error": str(e), "status": "failed"}


def _analyze_with_vision(screenshot_path: Path, model: str = "meta/llama-3.2-90b-vision-instruct", instructions: str | None = None) -> dict | None:
    """Envía screenshot a un modelo de visión vía NVIDIA NIM API (OpenAI-compatible).
    
    Usa NIM en lugar de Ollama/llava. El modelo por defecto es llama-3.2-90b-vision.
    Alternativa: nvidia/nemotron-nano-12b-v2-vl (más rápido, menos detalle).
    
    Si se proveen instrucciones, usa el prompt dinámico de QA engineer.
    Caso contrario, usa el prompt general de detección de bugs.
    """
    import os as _os
    
    # Get NIM API key
    api_key = _os.getenv("NVIDIA_API_KEY", "")
    if not api_key:
        env_path = BASE_DIR / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if "NVIDIA_API_KEY" in line and "=" in line:
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    
    if not api_key:
        return {"error": "NVIDIA_API_KEY no configurada", "note": "Set .env or env var for NIM vision"}
    
    # Elegir prompt
    if instructions and instructions.strip():
        prompt = _INSTRUCTION_PROMPT_TEMPLATE.format(instructions=instructions) if "{instructions}" in _INSTRUCTION_PROMPT_TEMPLATE else instructions
    else:
        prompt = _GENERAL_VISION_PROMPT
    
    try:
        from openai import OpenAI
        img_b64 = base64.b64encode(screenshot_path.read_bytes()).decode()
        
        client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
            ]}],
            max_tokens=800,
            temperature=0.2,
            timeout=120,
        )
        raw = response.choices[0].message.content or ""
        
        # Intentar parsear JSON, fallback a texto crudo
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return {"raw": raw[:1500], "model": model}
    
    except ImportError:
        return {"error": "openai package no instalado", "note": "pip install openai"}
    except Exception as e:
        return {"error": str(e), "note": f"NIM/{model} no disponible"}
