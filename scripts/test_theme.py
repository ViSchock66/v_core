"""Verifica el toggle de tema con estado controlado y guarda capturas de ambos."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()

from playwright.sync_api import sync_playwright  # noqa: E402

OUT = Path(".audit_shots")


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        errores: list[str] = []
        pg.on("pageerror", lambda e: errores.append(str(e)[:200]))

        # Estado limpio: se fija el tema en el almacenamiento ANTES de cargar.
        pg.goto("http://127.0.0.1:8000/", wait_until="domcontentloaded")
        pg.evaluate("localStorage.setItem('vc.theme','dark')")
        pg.reload(wait_until="networkidle")
        pg.wait_for_timeout(2000)

        pasos = []
        for i in range(3):
            tema = pg.evaluate("document.documentElement.getAttribute('data-theme')")
            fondo = pg.evaluate("getComputedStyle(document.body).backgroundColor")
            texto = pg.evaluate("getComputedStyle(document.body).color")
            pasos.append({"paso": i, "tema": tema, "fondo": fondo, "texto": texto})
            pg.screenshot(path=str(OUT / f"v2_tema_{i}_{tema}.png"))
            btn = pg.query_selector("aside button[title='Cambiar tema']")
            if not btn:
                pasos.append({"error": "boton de tema no encontrado"})
                break
            btn.click()
            pg.wait_for_timeout(700)

        # El acento tambien: se aplica al <html> y cambia --accent
        acento = {}
        dots = pg.query_selector_all("aside button[aria-label^='Acento']")
        if len(dots) >= 3:
            dots[2].click()
            pg.wait_for_timeout(500)
            acento = {
                "atributo": pg.evaluate("document.documentElement.getAttribute('data-accent')"),
                "variable": pg.evaluate(
                    "getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()"
                ),
                "guardado": pg.evaluate("localStorage.getItem('vc.accent')"),
            }
            pg.screenshot(path=str(OUT / "v2_acento.png"))

        b.close()

    informe = {"pasos_tema": pasos, "acento": acento, "errores_js": errores}
    (OUT / "v2_tema.json").write_text(json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(informe, indent=2, ensure_ascii=False))

    temas = [p.get("tema") for p in pasos if "tema" in p]
    problemas = []
    # Alternar 3 veces desde dark debe dar dark, light, dark
    if temas[:3] != ["dark", "light", "dark"]:
        problemas.append(f"el toggle no alterna correctamente: {temas}")
    if errores:
        problemas.append(f"{len(errores)} errores de JS")
    print("\nDIAGNOSTICO:", "OK" if not problemas else " | ".join(problemas))
    return 1 if problemas else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
