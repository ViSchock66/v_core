"""V-CORE Frontend Test Suite
   Playwright · structural + functional checks · dark + light · accent + theme
   Ejecutar: PYTHONPATH=".venv/Lib/site-packages;." .venv/Scripts/python scripts/test_frontend.py
"""
from playwright.sync_api import sync_playwright
import json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api.ports import api_base as _api_base

# Fuente unica del puerto (api/ports.py). Antes estaba fijo a 8001 mientras el
# backend canonico usa 8000: la suite fallaba con ERR_CONNECTION_REFUSED contra
# un servidor que estaba levantado.
BASE = _api_base() + "/"
TIMEOUT = 8000

def run():
    results = {"date": time.strftime("%Y-%m-%d %H:%M"), "checks": [], "passed": 0, "failed": 0}

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1440, "height": 900})

        # ═══ DARK THEME STRUCTURAL ═══
        pg.goto(BASE)
        pg.wait_for_timeout(800)

        checks = [
            ("sidebar present", pg.query_selector("#sidebar") is not None),
            ("sidebar width 240", pg.evaluate("document.getElementById('sidebar').offsetWidth") == 240),
            ("topbar height 36", pg.evaluate("document.getElementById('topbar').offsetHeight") == 36),
            ("4 tabs", pg.evaluate("document.querySelectorAll('.sb-tab').length") == 4),
            ("4 tab icons", pg.evaluate("document.querySelectorAll('.sb-tab svg').length") == 4),
            ("new-chat btn", pg.query_selector(".new-chat-btn") is not None),
            ("conv-list", pg.query_selector("#conv-list") is not None),
            ("sb-footer", pg.query_selector("#sb-footer") is not None),
            ("2 foot-labels", pg.evaluate("document.querySelectorAll('.sb-foot-label').length") == 2),
            ("6 accent dots", pg.evaluate("document.querySelectorAll('.accent-dot').length") == 6),
            ("theme toggle", pg.query_selector("#theme-toggle") is not None),
            ("input field", pg.query_selector("#input-field") is not None),
            ("send btn", pg.query_selector("#send-btn") is not None),
            ("send btn disabled initially", pg.evaluate("document.getElementById('send-btn').disabled") == True),
            ("model selector", pg.query_selector("#model-selector") is not None),
            ("tok-info", pg.query_selector("#tok-info") is not None),
            ("rp-toggle", pg.query_selector("#rp-toggle") is not None),
            ("sb-toggle", pg.query_selector("#sb-toggle") is not None),
            ("cb-alert", pg.query_selector("#cb-alert") is not None),
            ("conn-banner", pg.query_selector("#conn-banner") is not None),
            ("monaco-editor div", pg.query_selector("#monaco-editor") is not None),
        ]
        for name, passed in checks:
            results["checks"].append({"name": name, "passed": passed})
            if passed: results["passed"] += 1
            else: results["failed"] += 1

        # ═══ THEME TOGGLE → LIGHT ═══
        pg.click("#theme-toggle")
        pg.wait_for_timeout(400)
        theme = pg.evaluate("document.documentElement.getAttribute('data-theme')")
        surf = pg.evaluate("getComputedStyle(document.body).getPropertyValue('--surf-chat').trim()")
        results["checks"].append({"name": "theme toggles to light", "passed": theme == "light"})
        results["checks"].append({"name": "surf-chat light is #F8F7F5", "passed": surf == "#F8F7F5"})
        if theme == "light": results["passed"] += 1
        else: results["failed"] += 1
        if surf == "#F8F7F5": results["passed"] += 1
        else: results["failed"] += 1

        # ═══ ACCENT SWAP ═══
        pg.evaluate("document.documentElement.setAttribute('data-theme','dark')")
        pg.wait_for_timeout(200)
        pg.evaluate("document.querySelector('.accent-dot[data-accent=\"emerald\"]').click()")
        pg.wait_for_timeout(300)
        accent = pg.evaluate("document.documentElement.getAttribute('data-accent')")
        accent_color = pg.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
        results["checks"].append({"name": "accent swaps to emerald", "passed": accent == "emerald"})
        results["checks"].append({"name": "accent color is #10A37F", "passed": accent_color == "#10A37F"})
        if accent == "emerald": results["passed"] += 1
        else: results["failed"] += 1
        if accent_color == "#10A37F": results["passed"] += 1
        else: results["failed"] += 1

        # ═══ SIDEBAR TOGGLE ═══
        pg.evaluate("document.documentElement.setAttribute('data-accent','cyan')")
        pg.wait_for_timeout(100)
        pg.click("#sb-toggle")
        pg.wait_for_timeout(400)
        sb_closed = pg.evaluate("document.getElementById('sidebar').offsetWidth")
        results["checks"].append({"name": "sidebar collapses to 0", "passed": sb_closed == 0})
        if sb_closed == 0: results["passed"] += 1
        else: results["failed"] += 1

        # ═══ SEND BTN ENABLES ON INPUT ═══
        pg.click("#sb-toggle")  # reopen sidebar
        pg.wait_for_timeout(300)
        pg.fill("#input-field", "test message")
        pg.wait_for_timeout(200)
        send_enabled = pg.evaluate("document.getElementById('send-btn').disabled") == False
        results["checks"].append({"name": "send btn enables on input", "passed": send_enabled})
        if send_enabled: results["passed"] += 1
        else: results["failed"] += 1

        # ═══ 9.4: SEND REAL MESSAGE ═══
        pg.fill("#input-field", "")
        pg.fill("#input-field", "Hola Orchestrator, responde con un JSON {\"status\":\"ok\"}")
        pg.wait_for_timeout(100)
        pg.click("#send-btn")
        pg.wait_for_timeout(TIMEOUT)

        msg_count = pg.evaluate("document.querySelectorAll('.msg-ai, .msg-user').length")
        has_ai_msg = pg.evaluate("document.querySelector('.msg-ai') !== null")
        has_body = pg.evaluate("document.querySelector('.msg-body') !== null")

        results["checks"].append({"name": "9.4: messages rendered", "passed": msg_count > 1})
        results["checks"].append({"name": "9.4: AI msg present", "passed": has_ai_msg})
        results["checks"].append({"name": "9.4: msg-body rendered", "passed": has_body})
        if msg_count > 1: results["passed"] += 1
        else: results["failed"] += 1
        if has_ai_msg: results["passed"] += 1
        else: results["failed"] += 1
        if has_body: results["passed"] += 1
        else: results["failed"] += 1

        b.close()

    results["status"] = "PASS" if results["failed"] == 0 else "FAIL"
    return results

if __name__ == "__main__":
    r = run()
    print(json.dumps(r, indent=2, ensure_ascii=False))
    sys.exit(0 if r["status"] == "PASS" else 1)
