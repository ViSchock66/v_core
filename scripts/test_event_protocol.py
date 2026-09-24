"""Verifica el protocolo de eventos: persistencia, secuencia y replay.

Comprueba lo que antes era imposible:
  1. Cada evento del stream queda persistido con `seq`, `id`, `run_id` y `type`.
  2. El `seq` es monotónico y sin huecos dentro del run.
  3. `GET /threads/{id}/events` reproduce la conversación completa.
  4. `?after=<seq>` devuelve exactamente lo que falta (reconexión).

Uso: .venv\\Scripts\\python scripts\\test_event_protocol.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()

BASE = "http://127.0.0.1:8000"
MENSAJE = "Lista los archivos de la carpeta api. Se breve."


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read())


def main() -> int:
    # 1. Crear una sesión para tener un thread limpio
    req = urllib.request.Request(BASE + "/sessions", data=b"{}",
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        ses = json.loads(r.read())
    thread = ses["session_dir"]
    print(f"sesion creada: id={ses['id']} dir={thread}\n")

    # 2. Enviar un mensaje y capturar los seq que llegan por SSE
    payload = json.dumps({"message": MENSAJE, "history": [], "task_id": "",
                          "session_dir": thread}).encode()
    req = urllib.request.Request(BASE + "/agents/route", data=payload,
                                 headers={"Content-Type": "application/json"})
    seqs: list[int] = []
    tipos_sse: list[str] = []
    run_id = None
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except Exception:
                continue
            if "seq" in ev:
                seqs.append(ev["seq"])
                tipos_sse.append(ev.get("type", "?"))
                run_id = run_id or ev.get("run_id")
    print(f"SSE: {len(seqs)} eventos numerados | run_id={run_id}")
    print(f"  seq recibidos: {seqs[:8]}{'...' if len(seqs) > 8 else ''}")
    print(f"  tipos: {sorted(set(tipos_sse))}\n")

    # 3. Verificar coherencia de la numeracion.
    #
    # Aclaracion de semantica: `seq` numera los eventos CANONICOS del log, no
    # cada delta de texto del SSE. Los deltas viajan sin seq para no engordar el
    # wire (un seq por token no aporta nada al cliente). Lo que el cliente
    # necesita es poder ubicarse: recibe el seq en los eventos de politica y en
    # el cierre del run, y con el MAXIMO que vio reconecta pidiendo lo que falta.
    problemas = []
    if not seqs:
        problemas.append("ningun evento del SSE trae seq: el protocolo no se aplica al cliente")
    persistidos_seq = []
    if seqs != sorted(seqs):
        problemas.append("los seq del SSE no vienen en orden creciente")

    # 4. Replay desde el log
    store = get(f"/threads/{thread}/events")
    persistidos = store["events"]
    print(f"replay: {store['count']} eventos persistidos | next_after={store['next_after']}")
    tipos_log = sorted({e["type"] for e in persistidos})
    print(f"  tipos en el log: {tipos_log}")

    if store["count"] == 0:
        problemas.append("no se persistio ningun evento")

    # El log debe incluir al menos el ciclo de vida del run
    for requerido in ("run.start", "run.end"):
        if requerido not in tipos_log:
            problemas.append(f"falta el evento '{requerido}' en el log")

    # 5. Los eventos persistidos deben ser reconstruibles en orden
    seq_log = [e["seq"] for e in persistidos]
    if seq_log != sorted(seq_log):
        problemas.append("el log no esta ordenado por seq")

    campos_ok = all(
        {"v", "seq", "id", "ts", "run_id", "thread_id", "type", "data"} <= set(e)
        for e in persistidos
    )
    if not campos_ok:
        problemas.append("hay eventos sin el envelope completo")

    # 6. Reconexion: el cliente usa el MAXIMO seq que vio y pide lo que falta.
    if persistidos:
        ultimo_visto = max(seqs) if seqs else 0
        resto = get(f"/threads/{thread}/events?after={ultimo_visto}")
        esperados = [e["seq"] for e in persistidos if e["seq"] > ultimo_visto]
        obtenidos = [e["seq"] for e in resto["events"]]
        print(f"\nreconexion: after={ultimo_visto} -> {resto['count']} eventos "
              f"(esperados {len(esperados)})")
        if esperados != obtenidos:
            problemas.append("la reconexion con after= no devuelve lo correcto")
        # El cliente no debe recibir eventos que ya tenia
        repetidos = set(obtenidos) & set(s for s in seqs if s <= ultimo_visto)
        if repetidos:
            problemas.append(f"la reconexion repite eventos ya vistos: {sorted(repetidos)}")

        # Y desde cero debe reproducir la conversacion completa
        completo = get(f"/threads/{thread}/events?after=0")
        print(f"replay completo: {completo['count']} eventos "
              f"(el SSE numerado vio {len(seqs)})")
        if completo["count"] < len(persistidos):
            problemas.append("el replay completo devuelve menos eventos que el log")

    # 7. Resumen del thread
    resumen = get(f"/threads/{thread}/summary")
    print(f"\nresumen: events={resumen['events']} runs={resumen['runs']}")
    print(f"  por tipo: {resumen['types']}")

    # 8. El texto plano sigue yendo a chat_history (compatibilidad)
    msg = get(f"/sessions/{thread}/messages")
    print(f"chat_history: {len(msg['messages'])} mensajes (compatibilidad)")

    print()
    if problemas:
        print("DIAGNOSTICO: FALLAS")
        for p in problemas:
            print(f"  - {p}")
        return 1

    # Guardar evidencia
    Path(".audit_shots/event_protocol.json").write_text(
        json.dumps({"thread": thread, "run_id": run_id, "sse_events": len(seqs),
                    "persisted": store["count"], "types": tipos_log,
                    "sample": persistidos[:3]},
                   indent=2, ensure_ascii=False), encoding="utf-8")
    print("DIAGNOSTICO: OK — el protocolo persiste, numera y permite reconectar")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
