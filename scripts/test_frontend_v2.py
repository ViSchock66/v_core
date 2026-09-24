"""Verifica el frontend NUEVO con Chromium real.

Comprueba que la UI carga, que no hay errores de consola, que el selector de
modelos muestra el rol y el modelo activo, y que los paneles funcionan.

Uso: .venv\\Scripts\\python scripts\\test_frontend_v2.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8000/"
OUT = Path(".audit_shots")
OUT.mkdir(exist_ok=True)


def main() -> int:
    errores: list[str] = []
    consola: list[str] = []
    fallos_http: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 950})
        page.on("pageerror", lambda e: errores.append(str(e)[:300]))
        page.on("console", lambda m: consola.append(f"[{m.type}] {m.text[:200]}")
                if m.type in ("error", "warning") else None)
        page.on("response", lambda r: fallos_http.append(f"{r.status} {r.url[:120]}")
                if r.status >= 400 else None)

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(2500)

        estado = page.evaluate(
            """() => {
              const txt = (sel) => document.querySelector(sel)?.textContent?.trim() ?? null;
              return {
                titulo: document.title,
                sidebar: !!document.querySelector('aside'),
                header: !!document.querySelector('header'),
                textarea: !!document.querySelector('textarea'),
                // El modelo activo visible en el compositor o el selector
                textoVisible: document.body.innerText.slice(0, 400),
                botones: [...document.querySelectorAll('button')].map(b => b.textContent.trim()).filter(Boolean).slice(0, 14),
                selects: document.querySelectorAll('select').length,
                root: document.getElementById('root')?.childElementCount ?? 0,
              };
            }"""
        )
        page.screenshot(path=str(OUT / "v2_01_inicio.png"))

        # Selector de modelos: abrir y verificar que muestra roles
        picker = page.query_selector("aside button[aria-expanded]")
        if picker:
            picker.click()
            page.wait_for_timeout(1200)
            selector = page.evaluate(
                """() => ({
                  selects: document.querySelectorAll('select').length,
                  opciones: [...document.querySelectorAll('select option')].map(o => o.textContent.trim()).slice(0, 10),
                  roles: [...document.querySelectorAll('aside .mb-2')].map(e => e.textContent.trim().slice(0, 50)).slice(0, 6),
                })"""
            )
            page.screenshot(path=str(OUT / "v2_02_modelos.png"))
        else:
            selector = {"error": "no se encontro el boton del selector de modelos"}

        # Paneles
        paneles = {}
        for label in ("Sistema", "Workspace", "Artefactos"):
            btn = page.query_selector(f"header button[title='{label}']")
            if not btn:
                paneles[label] = "boton no encontrado"
                continue
            btn.click()
            page.wait_for_timeout(1800)
            paneles[label] = page.evaluate(
                """() => {
                  const aside = document.querySelectorAll('aside');
                  const last = aside[aside.length - 1];
                  return (last?.innerText ?? '').slice(0, 260);
                }"""
            )
            page.screenshot(path=str(OUT / f"v2_03_{label.lower()}.png"))

        # Tema
        theme_btn = page.query_selector("aside button[title='Cambiar tema']")
        tema = {}
        if theme_btn:
            theme_btn.click()
            page.wait_for_timeout(700)
            tema["claro"] = page.evaluate("document.documentElement.getAttribute('data-theme')")
            tema["fondo"] = page.evaluate(
                "getComputedStyle(document.body).backgroundColor"
            )
            page.screenshot(path=str(OUT / "v2_04_tema_claro.png"))
            theme_btn.click()
            page.wait_for_timeout(500)
            tema["oscuro"] = page.evaluate("document.documentElement.getAttribute('data-theme')")

        browser.close()

    informe = {
        "estado": estado,
        "selector_modelos": selector,
        "paneles": paneles,
        "tema": tema,
        "errores_js": errores,
        "consola": consola[:14],
        "http_fallos": fallos_http[:14],
    }
    (OUT / "frontend_v2.json").write_text(
        json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(informe, indent=2, ensure_ascii=False))

    problemas = []
    if errores:
        problemas.append(f"{len(errores)} errores de JS")
    if not estado.get("textarea"):
        problemas.append("no hay caja de entrada")
    if estado.get("root", 0) == 0:
        problemas.append("la app no renderizo nada")
    print("\nDIAGNOSTICO:", "OK" if not problemas else " | ".join(problemas))
    return 1 if problemas else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
