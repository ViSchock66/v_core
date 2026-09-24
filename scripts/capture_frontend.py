"""
scripts/capture_frontend.py
===========================
Captura el frontend de V-CORE en distintos temas y acentos usando Edge
headless + el protocolo DevTools (CDP).

Por que CDP y no --screenshot
-----------------------------
El flag `--screenshot` de Chromium no permite ejecutar JavaScript antes de
capturar. El tema y el acento se leen de localStorage al arrancar app.js
(claves `vc-theme` y `vc_accent`), asi que sin pre-sembrar esas claves toda
captura sale en el default (dark/cyan) — que es exactamente lo que pasaba
con el enfoque de --screenshot.

CDP resuelve esto: `Page.addScriptToEvaluateOnNewDocument` inyecta el script
antes de que corra cualquier JS de la pagina.

Uso
---
    PYTHONPATH=".venv/Lib/site-packages;." .venv/Scripts/python scripts/capture_frontend.py
    ... --url http://127.0.0.1:8000/
    ... --wait-ms 4000

Salida: .audit_shots/<nombre>.png por cada combinacion.
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SHOTS_DIR = BASE_DIR / ".audit_shots"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# Claves reales que lee app.js (ver Frontend/app.js:32 y :43)
LS_THEME = "vc-theme"
LS_ACCENT = "vc_accent"

COMBOS = [
    ("01_dark_cyan", "dark", "cyan"),
    ("02_dark_emerald", "dark", "emerald"),
    ("03_dark_violet", "dark", "violet"),
    ("04_light_cyan", "light", "cyan"),
    ("05_light_amber", "light", "amber"),
    ("06_light_rose", "light", "rose"),
]


def find_edge() -> str:
    for c in EDGE_CANDIDATES:
        if Path(c).exists():
            return c
    raise SystemExit("Edge no encontrado.")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class CDP:
    """Cliente minimo del protocolo DevTools sobre WebSocket."""

    def __init__(self, ws_url: str):
        from websockets.sync.client import connect
        self._ws = connect(ws_url, max_size=64 * 1024 * 1024)
        self._id = 0

    def call(self, method: str, **params):
        self._id += 1
        mid = self._id
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def wait_for_devtools(port: int, timeout: float = 25.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
            time.sleep(0.4)
    raise SystemExit(f"DevTools no respondio en {timeout}s: {last}")


def capture_combo(edge: str, port: int, url: str, theme: str, accent: str,
                  out: Path, wait_ms: int, width: int, height: int) -> bool:
    """Captura una combinacion tema/acento usando una pestana nueva por CDP."""
    # Pestana nueva
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            target = json.loads(r.read())
    except Exception:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/new?about:blank", timeout=10) as r:
            target = json.loads(r.read())

    c = CDP(target["webSocketDebuggerUrl"])
    try:
        c.call("Page.enable")
        c.call("Runtime.enable")
        c.call("Emulation.setDeviceMetricsOverride",
               width=width, height=height, deviceScaleFactor=1, mobile=False)

        # Pre-sembrar localStorage ANTES de que corra app.js
        seed = (f"try{{localStorage.setItem('{LS_THEME}','{theme}');"
                f"localStorage.setItem('{LS_ACCENT}','{accent}');}}catch(e){{}}")
        c.call("Page.addScriptToEvaluateOnNewDocument", source=seed)

        c.call("Page.navigate", url=url)
        time.sleep(wait_ms / 1000.0)

        # Verificar que el tema realmente se aplico (no confiar en el exito)
        applied = c.call("Runtime.evaluate",
                         expression="document.documentElement.getAttribute('data-theme')"
                                    " + '/' + document.documentElement.getAttribute('data-accent')",
                         returnByValue=True)
        got = applied.get("result", {}).get("value")

        shot = c.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(shot["data"]))
        ok = got == f"{theme}/{accent}"
        print(f"  {'OK  ' if ok else 'MISM'} {out.name}  aplicado={got}  esperado={theme}/{accent}"
              f"  {out.stat().st_size:,} bytes")
        return ok
    finally:
        c.close()
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{target['id']}",
                                   timeout=5).read()
        except Exception:
            pass


def main() -> int:
    # Puerto desde la fuente unica (api/ports.py). Antes hardcodeado a 8001,
    # que no es el puerto canonico del backend.
    sys.path.insert(0, str(BASE_DIR))
    from api.ports import url as _api_url
    default_url = _api_url("/")

    ap = argparse.ArgumentParser(description="Capturas del frontend V-CORE")
    ap.add_argument("--url", default=default_url)
    ap.add_argument("--wait-ms", type=int, default=4500)
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--keep-profile", action="store_true")
    args = ap.parse_args()

    edge = find_edge()
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    port = free_port()
    profile = Path(tempfile.mkdtemp(prefix="vcore-cdp-"))

    proc = subprocess.Popen(
        [edge, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
         f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         f"--window-size={args.width},{args.height}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        info = wait_for_devtools(port)
        print(f"Edge: {info.get('Browser')}")
        print(f"URL:  {args.url}\n")

        results = []
        for name, theme, accent in COMBOS:
            results.append(capture_combo(edge, port, args.url, theme, accent,
                                         SHOTS_DIR / f"{name}.png",
                                         args.wait_ms, args.width, args.height))

        ok = sum(1 for r in results if r)
        print(f"\ncapturas correctas: {ok}/{len(results)}")
        return 0 if ok == len(results) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        if not args.keep_profile:
            shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
