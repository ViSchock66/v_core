"""Prueba end-to-end del chat V-CORE: envia un mensaje real, espera SSE y
reporta si se renderizan tool calls, artifacts, razonamiento, y si se
persiste el historial.

Uso: .venv\\Scripts\\python scripts/e2e_chat_probe.py "mensaje"
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".audit_shots"
OUT.mkdir(exist_ok=True)
BASE = "http://127.0.0.1:8000/"

MSG = sys.argv[1] if len(sys.argv) > 1 else "Crea un archivo hello.txt con el contenido 'hola vcore' usando write_file y confirmame cuando este listo."


def main() -> None:
    events: list[dict] = []
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 950})
        page.on("pageerror", lambda e: errors.append(str(e)[:300]))
        page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text[:250]}") if m.type == "error" else None)

        page.on("response", lambda r: events.append({"http": r.status, "url": r.url[:160]}) if r.status >= 400 else None)

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1000)

        page.fill("#input-field", MSG)
        page.wait_for_timeout(200)
        page.click("#send-btn")
        print(f"[e2e] mensaje enviado: {MSG[:80]}", flush=True)

        # esperar a que termine el streaming (boton vuelve a modo enviar)
        deadline = time.time() + 240
        while time.time() < deadline:
            streaming = page.evaluate("document.getElementById('send-btn')?.classList.contains('stop-mode')")
            if not streaming:
                break
            page.wait_for_timeout(2000)
        page.wait_for_timeout(2500)

        state = page.evaluate(
            """() => ({
              toolBlocks: document.querySelectorAll('.tool-call-block').length,
              artifactCards: document.querySelectorAll('.artifact-card').length,
              reasoningBlocks: document.querySelectorAll('.reasoning').length,
              mdRendered: document.querySelectorAll('.md-rendered').length,
              msgAi: document.querySelectorAll('.msg-ai').length,
              msgUser: document.querySelectorAll('.msg-user').length,
              welcomeGone: !document.querySelector('.welcome-state'),
              rpanelOpen: document.getElementById('rpanel')?.classList.contains('open'),
              monacoText: (document.getElementById('monaco-editor')?.textContent||'').slice(0,200),
              lastText: (document.querySelector('.msg-ai:last-of-type .msg-body')?.textContent||'').slice(0,300),
              tokInfo: document.getElementById('tok-info')?.textContent,
            })"""
        )
        page.screenshot(path=str(OUT / "05_chat_result.png"))

        # recargar la pagina: ¿se conserva la conversacion?
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(2000)
        after_reload = page.evaluate(
            """() => ({
              msgAi: document.querySelectorAll('.msg-ai').length,
              msgUser: document.querySelectorAll('.msg-user').length,
              welcome: !!document.querySelector('.welcome-state'),
              convItems: document.querySelectorAll('.conv-item').length,
            })"""
        )
        page.screenshot(path=str(OUT / "06_after_reload.png"))
        browser.close()

    payload = {"state": state, "after_reload": after_reload,
               "errors": errors[:15], "http_failures": events[:15]}
    (OUT / "e2e_chat_probe.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(payload, indent=2, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
