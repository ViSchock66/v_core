"""Auditoria viva del frontend V-CORE con Playwright.

Objetivo: obtener evidencia real (no suposiciones) del estado actual del
frontend antes del refactor. Captura:
  - errores de consola y pageerror
  - requests fallidos (>=400) con su URL
  - estado del selector de modelos (click real en el dropdown)
  - estado del arbol de archivos
  - estado del visor de artefactos (Monaco: carga o no)
  - toggle de tema/acento

Uso:
  .venv\\Scripts\\python scripts/audit_live.py
Salida:
  .audit_shots/audit_live.json + screenshots
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".audit_shots"
OUT.mkdir(exist_ok=True)
BASE = "http://127.0.0.1:8000/"

console: list[dict] = []
pageerrors: list[str] = []
netfail: list[dict] = []


def main() -> dict:
    report: dict = {"date": time.strftime("%Y-%m-%d %H:%M:%S"), "base": BASE, "steps": {}}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 950})

        page.on("console", lambda m: console.append({"type": m.type, "text": m.text[:300]}))
        page.on("pageerror", lambda e: pageerrors.append(str(e)[:300]))

        def on_response(resp):
            if resp.status >= 400:
                netfail.append({"status": resp.status, "url": resp.url[:200]})

        page.on("response", on_response)

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1500)

        # --- estado inicial ---
        report["steps"]["initial"] = page.evaluate(
            """() => ({
              modelLabel: document.getElementById('model-label')?.textContent,
              topbarModel: document.getElementById('topbar-model-label')?.textContent,
              convItems: document.querySelectorAll('.conv-item').length,
              welcome: !!document.querySelector('.welcome-state'),
              treeNodes: document.querySelectorAll('#file-tree *').length,
              tokInfo: document.getElementById('tok-info')?.textContent,
            })"""
        )
        page.screenshot(path=str(OUT / "01_initial.png"), full_page=False)

        # --- dropdown de modelos: click real ---
        page.click("#model-selector")
        page.wait_for_timeout(1200)
        report["steps"]["model_dropdown"] = page.evaluate(
            """() => {
              const dd = document.getElementById('model-dropdown');
              return { open: dd?.classList.contains('show'), html: (dd?.innerHTML||'').slice(0,400),
                       rows: dd?.querySelectorAll('div').length };
            }"""
        )
        page.screenshot(path=str(OUT / "02_model_dropdown.png"))

        # --- file tree ---
        page.click('.sb-tab[data-sb="files"]')
        page.wait_for_timeout(1500)
        report["steps"]["file_tree"] = page.evaluate(
            """() => {
              const ft = document.getElementById('file-tree');
              return { html_len: (ft?.innerHTML||'').length, nodes: ft?.querySelectorAll('*').length,
                       first_text: (ft?.textContent||'').trim().slice(0,120) };
            }"""
        )
        page.screenshot(path=str(OUT / "03_file_tree.png"))

        # --- abrir un archivo en el visor de artefactos ---
        page.click('.sb-tab[data-sb="chat"]')
        page.wait_for_timeout(300)
        report["steps"]["monaco_before"] = page.evaluate(
            "() => ({ requireDefined: typeof require !== 'undefined', monacoDefined: typeof monaco !== 'undefined' })"
        )
        page.evaluate("openFileFromDisk('README.md')")
        page.wait_for_timeout(3000)
        report["steps"]["artifact_viewer"] = page.evaluate(
            """() => ({
              rpanelOpen: document.getElementById('rpanel')?.classList.contains('open'),
              paneDisplay: document.getElementById('rp-code')?.style.display,
              filename: document.getElementById('code-filename')?.textContent,
              monacoNodes: document.getElementById('monaco-editor')?.childElementCount,
              monacoGlobal: typeof monaco !== 'undefined',
              textSample: (document.getElementById('monaco-editor')?.textContent||'').slice(0,120),
            })"""
        )
        page.screenshot(path=str(OUT / "04_artifact_viewer.png"))

        # --- workspace real: ¿que ve el usuario como "workspace"? ---
        report["steps"]["workspace_ui"] = page.evaluate(
            """() => ({
              workspaceEls: document.querySelectorAll('[id*=workspace],[class*=workspace]').length,
              hasWorkspaceSelector: !!document.querySelector('#workspace-selector, .workspace-picker'),
            })"""
        )

        browser.close()

    report["console_errors"] = [c for c in console if c["type"] in ("error", "warning")]
    report["pageerrors"] = pageerrors
    report["http_failures"] = netfail
    (OUT / "audit_live.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps({
        "pageerrors": pageerrors[:10],
        "console_errors": report["console_errors"][:10],
        "http_failures": netfail[:10],
        "steps": report["steps"],
    }, indent=2, ensure_ascii=False))
    return report


if __name__ == "__main__":
    main()
