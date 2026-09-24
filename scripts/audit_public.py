#!/usr/bin/env python3
"""scripts/audit_public.py — auditoría de publicación de V-Core (plan §6).

Criterio de publicación: 0 hallazgos FAIL, sin excepciones.

Checks (sobre archivos trackeados; `--history` extiende al historial git):

  1. Secretos — 11 patrones específicos + asignaciones genéricas
     (api_key/secret/password/token). Incluye el formato nuevo de Google
     (AQ.*) que escapó al escaneo ingenuo de la auditoría original.
  2. Rutas del usuario — username del SO y disco personal. Se buscan por
     username directo, así que cubre TODAS las formas de escape (backslash
     simple, doble backslash YAML, forward slash). "Vicente"/"ViSchock66"
     como identidad de autor en LICENSE/README NO cuentan: están declarados
     a propósito.
  3. Binarios / pesados trackeados — extensiones de runtime (.db, .bin,
     .jsonl, .zip, .log, .pem, .key) y binarios grandes por contenido.
     Los archivos de texto grandes (p. ej. package-lock.json) no cuentan.
  4. Archivos sensibles alguna vez commiteados (.env, .env.keys, .db,
     .jsonl, .zip, credential, .pem) — solo con --history.
     Nota: el "memory" del plan se cubre con .db/.jsonl (dumps);
     agents/Curator/memory.py es fuente legítima, no un dump.
  5. Contenido de los zips del historial — .env / bases de datos dentro.

Uso:
    python scripts/audit_public.py             # worktree trackeado (pre-commit)
    python scripts/audit_public.py --history   # + historial completo (pre-push)

Sale con código 1 si hay algún FAIL. Los WARN no bloquean pero se reportan
(asignaciones genéricas con placeholders conocidos).
"""
from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

# ── Check 1: secretos ───────────────────────────────────────────────────────
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("google-aiza", re.compile(r"AIza[A-Za-z0-9_\-]{10,}")),
    ("google-aq-nuevo", re.compile(r"AQ\.[A-Za-z0-9_\-]{10,}")),
    ("openai-sk", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("anthropic-sk-ant", re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}")),
    ("nvidia-nvapi", re.compile(r"nvapi-[A-Za-z0-9_\-]{20,}")),
    ("github-ghp", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("github-pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("aws-akia", re.compile(r"AKIA[A-Z0-9]{16}")),
    ("slack-xox", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("huggingface-hf", re.compile(r"hf_[A-Za-z0-9]{20,}")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]
GENERIC_SECRET = re.compile(
    r"(?i)\b(api_key|apikey|secret|password|token)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"
)
PLACEHOLDER_HINTS = (
    "dummy", "your_key", "secreto", "placeholder", "changeme", "example", "xxxxx",
)

# ── Check 2: rutas del usuario ──────────────────────────────────────────────
# Case-sensitive a propósito: "Vicente" y "ViSchock66" son identidad de autor
# declarada (LICENSE/README), no filtraciones. El username del SO en cambio
# no tiene uso legítimo en archivos trackeados.
USER_PATH_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("username-so", re.compile(r"vicen")),
    ("disco-personal", re.compile(r"MODELOS\sSUMERIOS", re.IGNORECASE)),
]

# ── Check 3: binarios / pesados ──────────────────────────────────────────────
HEAVY_EXTS = {".db", ".sqlite3", ".bin", ".jsonl", ".zip", ".log", ".pem", ".key"}
BINARY_SIZE_LIMIT = 150_000

# ── Checks 4/5: historial ────────────────────────────────────────────────────
SENSITIVE_PATH = re.compile(
    r"(^|/)(\.env$|\.env\.keys$|.*\.db$|.*\.sqlite3$|.*\.jsonl$|.*\.zip$"
    r"|.*credential.*|.*\.pem$)",
    re.IGNORECASE,
)
TEXT_SKIP_EXTS = HEAVY_EXTS | {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf",
}

failures: list[str] = []
warnings: list[str] = []


def fail(check: str, where: str, detail: str) -> None:
    failures.append(f"[FAIL] {check} | {where} | {detail}")


def warn(check: str, where: str, detail: str) -> None:
    warnings.append(f"[WARN] {check} | {where} | {detail}")


def git_bytes(*args: str) -> bytes:
    proc = subprocess.run(["git", *args], cwd=REPO, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} -> {proc.returncode}: "
            f"{proc.stderr.decode(errors='replace')}"
        )
    return proc.stdout


def git_text(*args: str) -> str:
    return git_bytes(*args).decode("utf-8", errors="replace")


def cat_file_batch(shas: list[str]) -> dict[str, bytes]:
    """Trae muchos blobs en una sola llamada (`git cat-file --batch`).

    Formato de salida: "<sha> <type> <size>\\n<contenido>\\n" por objeto;
    los objetos inexistentes responden "<sha> missing\\n".
    """
    if not shas:
        return {}
    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=REPO,
        input=("\n".join(shas) + "\n").encode(),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git cat-file --batch: {proc.stderr.decode(errors='replace')}")
    out = proc.stdout
    result: dict[str, bytes] = {}
    i = 0
    while i < len(out):
        nl = out.index(b"\n", i)
        header = out[i:nl].decode(errors="replace").split()
        i = nl + 1
        if len(header) < 3:
            continue  # "<sha> missing" o línea vacía
        sha, size = header[0], int(header[2])
        result[sha] = out[i : i + size]
        i += size + 1  # contenido + \n final
    return result


def tracked_paths() -> list[str]:
    out = git_bytes("ls-files", "-z").split(b"\0")
    return [p.decode("utf-8", errors="replace") for p in out if p]


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:1024]


def scan_text(check: str, where: str, text: str) -> None:
    """Escanea un texto línea por línea y acumula hallazgos."""
    for lineno, line in enumerate(text.splitlines(), 1):
        loc = f"{where}:{lineno}"
        stripped = line.strip()
        for name, pat in SECRET_PATTERNS:
            if pat.search(line):
                fail(check, loc, f"secreto {name}: {stripped[:100]!r}")
        m = GENERIC_SECRET.search(line)
        if m:
            frag = stripped[:100]
            if any(h in frag.lower() for h in PLACEHOLDER_HINTS):
                warn(check, loc, f"asignacion generica con placeholder: {frag!r}")
            else:
                fail(check, loc, f"asignacion generica sospechosa: {frag!r}")
        for name, pat in USER_PATH_PATTERNS:
            if pat.search(line):
                fail(check, loc, f"ruta del usuario ({name}): {stripped[:100]!r}")


def check_worktree() -> None:
    for rel in tracked_paths():
        path = REPO / rel
        if path.resolve() == SELF:
            continue  # el auditor no se audita a si mismo (contiene sus patrones)
        if not path.exists():
            warn("3-worktree", rel, "trackeado pero ausente en disco (move sin commit?)")
            continue
        data = path.read_bytes()
        if path.suffix.lower() in HEAVY_EXTS:
            fail("3-binarios", rel, f"extension de runtime trackeada ({path.suffix})")
            continue
        if is_binary(data):
            if len(data) > BINARY_SIZE_LIMIT:
                fail("3-binarios", rel, f"binario de {len(data)} bytes trackeado")
            continue
        scan_text("1/2-worktree", rel, data.decode("utf-8", errors="replace"))


def check_history() -> None:
    # ── Check 4: rutas sensibles alguna vez commiteadas ──
    added: set[str] = set()
    for line in git_text(
        "log", "--all", "--diff-filter=A", "--name-only", "--format="
    ).splitlines():
        if line.strip():
            added.add(line.strip())
    for p in sorted(added):
        if SENSITIVE_PATH.search(p.replace("\\", "/")):
            fail("4-historial", p, "archivo sensible alguna vez commiteado")

    # ── Checks 1/2/5: contenido de todos los blobs del historial ──
    # rev-list --objects deduplica por contenido (cada blob aparece una vez,
    # con UN path) pero también lista trees (path terminado en "/"): se
    # filtran. Los archivos sensibles no-zip ya quedaron cubiertos por el
    # check 4 (sus paths); los zips van al check 5.
    wanted: list[tuple[str, str]] = []
    for line in git_text("rev-list", "--all", "--objects").splitlines():
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        sha = parts[0]
        path = parts[1] if len(parts) > 1 else ""
        if not path or path.endswith("/"):
            continue  # trees y objetos sin path
        norm = path.replace("\\", "/")
        if Path(norm).name == SELF.name:
            # El auditor contiene sus propios patrones de búsqueda — el único
            # lugar legítimo donde esas cadenas deben existir. Sin esta
            # exclusión, el check 2 se autodenuncia en el historial.
            continue
        if Path(norm).suffix.lower() in TEXT_SKIP_EXTS and not norm.lower().endswith(".zip"):
            continue  # binarios/sensibles no-zip: cubiertos por check 4
        wanted.append((sha, path))
    blobs = cat_file_batch([sha for sha, _ in wanted])
    for sha, path in wanted:
        data = blobs.get(sha)
        if data is None:
            continue  # objeto inaccesible: ya quedó reportado por path en check 4
        norm = path.replace("\\", "/")
        if norm.lower().endswith(".zip"):
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
            except Exception as exc:  # zip corrupto o no-zip renombrado
                warn("5-zips", f"{path}@{sha[:8]}", f"zip ilegible en historial: {exc}")
                continue
            for name in zf.namelist():
                if re.search(r"\.env$|\.db$|\.sqlite3$|credential", name, re.IGNORECASE):
                    fail("5-zips", f"{path}@{sha[:8]}::{name}",
                         "archivo sensible dentro de un zip del historial")
            continue
        if is_binary(data):
            continue
        scan_text("1/2-historial", f"{path}@{sha[:8]}",
                  data.decode("utf-8", errors="replace"))


def main() -> int:
    # Consolas Windows usan cp1252 por defecto: sin esto, un hallazgo que
    # contenga '→' (o cualquier no-Latin1) revienta el reporte a mitad.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Auditoria de publicacion (plan seccion 6): 0 hallazgos FAIL."
    )
    parser.add_argument(
        "--history", action="store_true",
        help="escanear tambien TODO el historial git (pre-push)",
    )
    args = parser.parse_args()

    check_worktree()
    if args.history:
        check_history()

    for w in warnings:
        print(w)
    for f in failures:
        print(f)
    mode = "worktree+historial" if args.history else "worktree"
    print(f"\n=== audit_public [{mode}]: FAIL={len(failures)} WARN={len(warnings)} ===")
    print("Criterio de publicacion: 0 FAIL, sin excepciones.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
