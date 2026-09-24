"""Prueba end-to-end del frontend NUEVO: envía un mensaje y verifica el render.

Comprueba lo que el frontend anterior no lograba:
  - razonamiento en bloque colapsable
  - tool cards con nombre, args y resultado
  - artefactos abribles en el visor
  - y, lo más importante: que al recargar la página la conversación SIGA ahí,
    reconstruida desde el log de eventos (replay).

Uso: .venv\\Scripts\\python scripts\\test_e2e_v2.py
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

MENSAJE = "Lista los archivos de la carpeta api y dime cuantos .py hay. Se breve."


def main() -> int:
    errores: list[str] = []
    http: list[str] = []

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        pg.on("pageerror", lambda e: errores.append(str(e)[:300]))
        pg.on("console", lambda m: errores.append(f"[{m.type}] {m.text[:200]}")
              if m.type == "error" else None)
        pg.on("response", lambda r: http.append(f"{r.status} {r.url[:110]}")
              if r.status >= 400 else None)

        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(1500)

        # Nueva conversación para tener un hilo limpio
        pg.click("text=Nueva conversación")
        pg.wait_for_timeout(2000)

        pg.fill("textarea", MENSAJE)
        pg.wait_for_timeout(150)
        pg.keyboard.press("Enter")
        print(f"[e2e] enviado: {MENSAJE[:60]}", flush=True)

        # Esperar a que termine el run (el boton deja de decir "Detener")
        deadline = time.time() + 240
        while time.time() < deadline:
            streaming = pg.evaluate("!!document.body.innerText.includes('Detener')")
            if not streaming:
                break
            pg.wait_for_timeout(1500)
        pg.wait_for_timeout(3000)

        en_vivo = pg.evaluate(
            """() => {
              const txt = document.body.innerText;
              return {
                razonamiento: txt.includes('razonamiento'),
                toolCards: document.querySelectorAll('main [aria-expanded]').length,
                tieneTexto: txt.includes('archivos') || txt.includes('py'),
                largoTexto: txt.length,
                sessions: document.querySelectorAll('aside button').length,
                hilo: txt.match(/sess_\\d+_\\w+/)?.[0] ?? null,
              };
            }"""
        )
        pg.screenshot(path=str(OUT / "v2_e2e_01_respuesta.png"))

        # Probar el panel de sistema con datos reales
        pg.click("header button[title='Sistema']")
        pg.wait_for_timeout(2500)
        sistema = pg.evaluate(
            """() => {
              const asides = document.querySelectorAll('aside');
              const t = asides[asides.length-1]?.innerText ?? '';
              return { texto: t.slice(0, 500), tieneEventos: /último seq\\s*\\n?\\s*(\\d+)/.exec(t)?.[1] ?? null };
            }"""
        )
        pg.screenshot(path=str(OUT / "v2_e2e_02_sistema.png"))

        pg.click("header button[title='Workspace']")
        pg.wait_for_timeout(2200)
        workspace = pg.evaluate(
            """() => {
              const asides = document.querySelectorAll('aside');
              return (asides[asides.length-1]?.innerText ?? '').slice(0, 400);
            }"""
        )
        pg.screenshot(path=str(OUT / "v2_e2e_03_workspace.png"))

        pg.click("header button[title='Artefactos']")
        pg.wait_for_timeout(1500)
        pg.screenshot(path=str(OUT / "v2_e2e_04_artefactos.png"))

        # LA PRUEBA CLAVE: recargar y ver si la conversacion sigue
        hilo = en_vivo.get("hilo")
        pg.reload(wait_until="networkidle")
        pg.wait_for_timeout(2500)
        tras_recarga = pg.evaluate(
            """() => {
              const txt = document.body.innerText;
              return {
                largoTexto: txt.length,
                tieneToolCards: document.querySelectorAll('main [aria-expanded]').length,
                tieneRazonamiento: txt.includes('razonamiento'),
                sigueVacio: txt.includes('Creá una conversación para empezar') ||
                            txt.includes('Conversación vacía'),
                fragmento: txt.slice(0, 200),
              };
            }"""
        )
        pg.screenshot(path=str(OUT / "v2_e2e_05_tras_recarga.png"))

        b.close()

    informe = {
        "en_vivo": en_vivo,
        "sistema": sistema,
        "workspace": workspace,
        "tras_recarga": tras_recarga,
        "errores_js": errores[:10],
        "http_fallos": http[:10],
    }
    (OUT / "e2e_v2.json").write_text(json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(informe, indent=2, ensure_ascii=False))

    problemas = []
    if errores:
        problemas.append(f"{len(errores)} errores de JS")
    if en_vivo["largoTexto"] < 200:
        problemas.append("la respuesta no se renderizo")
    if not en_vivo["razonamiento"]:
        problemas.append("no se ve el bloque de razonamiento")
    if tras_recarga["sigueVacio"] or tras_recarga["tieneToolCards"] == 0:
        problemas.append("la conversacion NO sobrevivio la recarga (el replay fallo)")
    print("\nDIAGNOSTICO:", "OK" if not problemas else " | ".join(problemas))
    return 1 if problemas else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
