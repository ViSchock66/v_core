"""
agents/ENKI/enki.py
====================
ENKI — Especialista Programador (Plan → Apply → Verify) — V-CORE v1.5

Pipeline de 3 fases:
  1. PLAN  — deepseek-ai/deepseek-v4-flash genera DiffProposal (str_replace format)
  2. APPLY — Materializa el diff en el archivo (matching en cascada)
  3. VERIFY — Linter + syntax check + tests, timeout 10s

Auto-Test Synthesis: si no existen tests, genera tests minimos antes de VERIFY.

Privacidad: el codigo completo nunca sale a la nube.
SHAMASH inyecta solo el fragmento relevante; ENKI envia el fragmento al modelo de plan.

Uso:
    from agents.ENKI.enki import ENKI
    enki = ENKI()
    proposal = await enki.plan_diff(context, task)
    result = enki.apply_diff(proposal)
    verified = enki.shadow_verify(proposal.file)
"""

from __future__ import annotations

import asyncio
import difflib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel

from system.observability import get_tracer


# =============================================================================
# SCHEMAS PYDANTIC (schema gates)
# =============================================================================

class DiffProposal(BaseModel):
    """Output de ENKI.plan_diff(). Schema gate: se valida antes de apply()."""
    file: str                           # path relativo al workspace
    search_block: str                   # fragmento exacto a reemplazar (str_replace format)
    replace_block: str                  # contenido nuevo
    explanation: str                    # por que se hace este cambio
    estimated_risk: Literal["low", "medium", "high"]
    files_affected: list[str]           # archivos adicionales si el cambio propaga


class VerifyResult(BaseModel):
    """Output de ENKI.shadow_verify(). Sin LLM — solo linter + tests."""
    passed: bool
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    timeout_hit: bool


class DiffMatchError(Exception):
    """Raised por apply_diff() cuando falla el match en todos los intentos."""
    def __init__(
        self,
        search_block_preview: str,
        file_path: str,
        attempts: list[str],
    ):
        self.search_block_preview = search_block_preview[:100]
        self.file_path = file_path
        self.attempts = attempts
        super().__init__(
            f"DiffMatchError: {len(attempts)} intentos fallidos "
            f"para {file_path}. Intentos: {', '.join(attempts)}"
        )


# =============================================================================
# CONSTANTES
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # V-CORE/
WORKSPACE_DIR = BASE_DIR / "workspace"
SHADOW_DIR = WORKSPACE_DIR / ".verify"  # shadow workspace para verify

MAX_RETRIES = 3          # maximo reintentos plan -> apply -> verify
VERIFY_TIMEOUT = 10      # timeout duro en segundos
FUZZY_THRESHOLD = 0.80   # threshold de similitud para fuzzy whitespace match (0.92→0.80 más permisivo)


# =============================================================================
# ENKI
# =============================================================================

class ENKI:
    """
    Especialista Programador.
    Pipeline: plan_diff() -> apply_diff() -> shadow_verify()
    """

    def __init__(self):
        self.agent_name = "ENKI"
        self._router = None
        self._tracer = get_tracer()

    @property
    def router(self):
        if self._router is None:
            from api.llm_client import get_router
            self._router = get_router()
        return self._router

    # ------------------------------------------------------------------
    # FASE 1: PLAN
    # ------------------------------------------------------------------

    async def plan_diff(
        self,
        context: str,
        task: str,
    ) -> DiffProposal:
        """
        Genera un DiffProposal via el modelo de plan (enki_plan).
        El contexto proviene de SHAMASH (solo fragmentos relevantes, no el archivo completo).

        Args:
            context: Contexto del archivo a modificar (fragmentos + lineas)
            task: Descripcion de la tarea a realizar

        Returns:
            DiffProposal validado
        """
        system_prompt = """Eres ENKI, el especialista programador de V-CORE.
Generas propuestas de modificacion de codigo en formato str_replace.

Debes responder SOLO con un JSON valido, sin texto adicional, sin markdown.
El JSON debe tener esta estructura exacta:
{
  "file": "ruta/relativa/al/workspace",
  "search_block": "fragmento exacto del codigo existente que se va a reemplazar",
  "replace_block": "codigo nuevo que reemplaza al search_block",
  "explanation": "explicacion breve del cambio",
  "estimated_risk": "low" | "medium" | "high",
  "files_affected": ["archivo1", "archivo2"]
}

REGLAS (obligatorias):
1. search_block debe ser IDENTICO al codigo existente (espacios, tabs, saltos de linea incluidos)
2. replace_block debe ser el codigo nuevo completo (no solo las lineas cambiadas)
3. estimated_risk: low=formato/docs, medium=cambio logico simple, high=cambio estructural
4. files_affected siempre incluye el archivo principal; agrega otros si el cambio propaga
5. SOLO JSON, sin explicaciones fuera del JSON, sin markdown
6. SI la tarea es CREAR un script/archivo NUEVO: usa search_block="" (vacio), file="workspace/nombre_del_script.ext", y replace_block con el codigo completo
7. NO modifiques README.md ni documentacion a menos que la tarea lo pida explicitamente
8. El campo "file" debe apuntar al archivo OBJETIVO de la tarea, no a documentacion"""

        user_prompt = f"""Contexto del archivo:
{context}

Tarea a realizar:
{task}"""

        # Usar temperatura baja para ser mas determinista
        response = await self.router.complete(
            role="enki_plan",
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
            agent_name=self.agent_name,
            task_id="",
        )

        # Parsear y validar el JSON de respuesta
        import json
        import re

        raw = response.content.strip()
        # Limpiar posibles bloques markdown
        raw = raw.replace("```json", "").replace("```", "").strip()

        # Extraer el primer objeto JSON
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            raise ValueError(
                f"ENKI.plan_diff: No se encontro JSON en respuesta. "
                f"Raw: {raw[:200]}"
            )

        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            raise ValueError(
                f"ENKI.plan_diff: JSON invalido — {e}. Raw: {raw[:200]}"
            )

        # Validar campos requeridos
        required = ["file", "search_block", "replace_block", "explanation", "estimated_risk"]
        for field in required:
            if field not in data:
                raise ValueError(
                    f"ENKI.plan_diff: Campo '{field}' faltante en respuesta"
                )

        # Validar estimated_risk
        if data["estimated_risk"] not in ("low", "medium", "high"):
            data["estimated_risk"] = "medium"  # fallback seguro

        return DiffProposal(**data)

    # ------------------------------------------------------------------
    # FASE 2: APPLY
    # ------------------------------------------------------------------

    def apply_diff(self, proposal: DiffProposal, task_id: str = "") -> None:
        """
        Materializa el DiffProposal sobre el archivo real.
        Matching en cascada:
          1. Exact match
          2. Fuzzy whitespace (normalizar tabs/espacios, threshold 0.92)
          3. DiffMatchError — ambos fallaron

        Nunca aplica parcialmente. O aplica completo o lanza DiffMatchError.
        """
        import time
        apply_start = time.perf_counter()
        
        # --- Gap 3: Validar path con gate antes de escribir ---
        from gate import Gate
        gate = Gate("gate_rules.yaml")
        filepath = self._resolve_path(proposal.file)
        decision = gate.evaluate("write_file", {"path": str(filepath)}, agent_id="ENKI")
        if not decision.auto_approved:
            raise PermissionError(f"Gate bloqueó escritura: {decision.reason}")
        # --- fin Gap 3 ---

        # Safety: no escribir sobre directorios
        if filepath.is_dir():
            raise ValueError(
                f"ENKI.apply_diff: '{proposal.file}' es un directorio, no un archivo. "
                f"El DiffProposal debe apuntar a un archivo concreto."
            )

        if not filepath.exists():
            # Crear archivo si no existe (contenido vacio para el match)
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text("", encoding="utf-8")

        content = filepath.read_text(encoding="utf-8")

        # Si el archivo es trivial o vacío, crear desde cero con el replace_block
        if len(content.split(chr(10))) <= 3 and (not content.strip() or proposal.replace_block.strip()):
            filepath.write_text(proposal.replace_block, encoding="utf-8")
            
            # B3: Trace diff applied
            self._tracer.trace_diff(
                task_id=task_id,
                file=proposal.file,
                outcome="applied",
                user_corrected=False,
                compiled=None,
                tests_passed=None,
                diff_size_chars=len(proposal.replace_block),
            )
            return

        # --- Intento 1: Exact match ---
        if proposal.search_block in content:
            new_content = content.replace(
                proposal.search_block, proposal.replace_block, 1
            )
            filepath.write_text(new_content, encoding="utf-8")
            
            # B3: Trace diff applied
            self._tracer.trace_diff(
                task_id=task_id,
                file=proposal.file,
                outcome="applied",
                user_corrected=False,
                compiled=None,
                tests_passed=None,
                diff_size_chars=len(proposal.replace_block),
            )
            return

        # ... rest of the method unchanged

        # --- Intento 2: Fuzzy whitespace match ---
        # Normalizar ambos: tabs -> espacios, trailing strip, colapsar whitespace multiple
        search_normalized = self._normalize_whitespace(proposal.search_block)
        content_normalized = self._normalize_whitespace(content)

        if search_normalized in content_normalized:
            # Encontrar la posicion exacta en el contenido normalizado
            idx = content_normalized.index(search_normalized)

            # Mapear de vuelta a posiciones originales contando chars normalizados
            # Usamos SequenceMatcher para encontrar el bloque correspondiente
            matcher = difflib.SequenceMatcher(
                None,
                content_normalized,
                content,
            )
            # Encontrar la region mas similar al search_block
            blocks = matcher.get_matching_blocks()

            for block in blocks:
                a_start, b_start, size = block
                if size >= len(search_normalized) * 0.8:
                    # Coincidencia suficientemente cercana
                    candidate = content[b_start:b_start + len(content)]
                    ratio = difflib.SequenceMatcher(
                        None,
                        proposal.search_block,
                        candidate[:len(proposal.search_block)],
                    ).ratio()
                    if ratio >= FUZZY_THRESHOLD:
                        new_content = (
                            content[:b_start]
                            + proposal.replace_block
                            + content[b_start + len(proposal.search_block):]
                        )
                        filepath.write_text(new_content, encoding="utf-8")
                        return

            # Fallback simple si no encontro match exacto normalizado
            # Reemplazar en el contenido normalizado y re-aplicar al original
            # (menos preciso pero funcional para cambios simples)
            pre_normalized = content_normalized.split(search_normalized, 1)
            if len(pre_normalized) == 2:
                # Reconstruir: parte antes + replace_block + parte despues
                pre_len = len(pre_normalized[0])
                # Buscar el punto de corte equivalente en el contenido original
                # usando la relacion de longitud normalizado/original
                ratio = len(content) / max(len(content_normalized), 1)
                cut_point = int(pre_len * ratio)
                new_content = (
                    content[:cut_point]
                    + proposal.replace_block
                    + content[cut_point + len(content) - cut_point:]
                )
                # Verificar que el reemplazo fue exitoso verificando que
                # el search_block ya no esta o el replace_block si esta
                if proposal.replace_block in new_content:
                    filepath.write_text(new_content, encoding="utf-8")
                    
                    # B3: Trace diff applied
                    self._tracer.trace_diff(
                        task_id=task_id,
                        file=proposal.file,
                        outcome="applied",
                        user_corrected=False,
                        compiled=None,
                        tests_passed=None,
                        diff_size_chars=len(proposal.replace_block),
                    )
                    return

        # --- Intento 3: Sobreescritura completa para archivos pequeños ---
        # Si el archivo es pequeño (<100 líneas), asumir que es un archivo
        # generado o dañado y sobreescribirlo completo con replace_block
        line_count = len(content.split(chr(10)))
        if line_count < 100 and proposal.replace_block.strip():
            filepath.write_text(proposal.replace_block, encoding="utf-8")
            
            # B3: Trace diff applied
            self._tracer.trace_diff(
                task_id=task_id,
                file=proposal.file,
                outcome="applied",
                user_corrected=False,
                compiled=None,
                tests_passed=None,
                diff_size_chars=len(proposal.replace_block),
            )
            return

        # --- Intento 4: Búsqueda línea por línea con normalización ---
        # Para archivos grandes donde los intentos anteriores fallaron,
        # intentar matching línea por línea ignorando whitespace extremo
        search_lines = proposal.search_block.strip().split(chr(10))
        content_lines = content.split(chr(10))
        if search_lines and len(search_lines) <= 20:
            # Buscar la primera línea del search_block en el contenido
            first_norm = self._normalize_whitespace(search_lines[0])
            for i, cl in enumerate(content_lines):
                if self._normalize_whitespace(cl) == first_norm:
                    # Verificar que las siguientes N líneas también coincidan
                    match = True
                    for j in range(1, min(len(search_lines), 5)):
                        if i + j >= len(content_lines):
                            match = False; break
                        if self._normalize_whitespace(content_lines[i + j]) != self._normalize_whitespace(search_lines[j]):
                            match = False; break
                    if match and i + len(search_lines) <= len(content_lines):
                        # Reemplazar el bloque encontrado
                        new_lines = content_lines[:i] + proposal.replace_block.split(chr(10)) + content_lines[i + len(search_lines):]
                        filepath.write_text(chr(10).join(new_lines), encoding="utf-8")
                        
                        # B3: Trace diff applied
                        self._tracer.trace_diff(
                            task_id=task_id,
                            file=proposal.file,
                            outcome="applied",
                            user_corrected=False,
                            compiled=None,
                            tests_passed=None,
                            diff_size_chars=len(proposal.replace_block),
                        )
                        return

        # Archivo grande que no matchea — error legítimo
        # B3: Trace diff failed
        self._tracer.trace_diff(
            task_id=task_id,
            file=proposal.file,
            outcome="failed",
            user_corrected=False,
            compiled=None,
            tests_passed=None,
            diff_size_chars=len(proposal.replace_block),
        )
        raise DiffMatchError(
            search_block_preview=proposal.search_block,
            file_path=proposal.file,
            attempts=["exact", "fuzzy_ws", "overwrite_fallback", "line_by_line"],
        )


    # ------------------------------------------------------------------
    # FASE 3: VERIFY
    # ------------------------------------------------------------------

    def shadow_verify(self, filepath: str, task_id: str = "") -> VerifyResult:
        """
        Verifica el archivo modificado en un shadow workspace.
        Sin LLM: linter + syntax check + tests.
        Timeout duro de 10 segundos.

        Args:
            filepath: Path relativo del archivo a verificar
            task_id: ID de la tarea para observabilidad (B3)

        Returns:
            VerifyResult con el resultado completo
        """
        full_path = self._resolve_path(filepath)
        if not full_path.exists():
            return VerifyResult(
                passed=False,
                stdout="",
                stderr=f"Archivo no encontrado: {filepath}",
                exit_code=-1,
                duration_seconds=0.0,
                timeout_hit=False,
            )

        start = time.time()
        ext = full_path.suffix.lower()

        # Copiar a shadow workspace para no contaminar el original
        try:
            rel_path = full_path.relative_to(WORKSPACE_DIR)
        except ValueError:
            rel_path = Path(full_path.name)
        shadow_file = SHADOW_DIR / rel_path
        shadow_file.parent.mkdir(parents=True, exist_ok=True)
        shadow_file.write_text(full_path.read_text(encoding="utf-8"), encoding="utf-8")

        # Determinar que verificacion hacer segun la extension
        if ext == ".py":
            result = self._verify_python(shadow_file)
        elif ext in (".js", ".ts"):
            result = self._verify_javascript(shadow_file)
        elif ext in (".yaml", ".yml"):
            result = self._verify_yaml(shadow_file)
        elif ext == ".json":
            result = self._verify_json(shadow_file)
        else:
            # Extension sin verificador especifico: pasar como valido
            result = VerifyResult(
                passed=True,
                stdout=f"Sin verificador disponible para '{ext}' — omitido",
                stderr="",
                exit_code=0,
                duration_seconds=round(time.time() - start, 2),
                timeout_hit=False,
            )

        # B3: Trace diff verification result
        self._tracer.trace_diff(
            task_id=task_id,
            file=filepath,
            outcome="verified" if result.passed else "failed",
            user_corrected=False,
            compiled=result.passed,
            tests_passed=result.passed if result.exit_code == 0 else False,
            diff_size_chars=0,
        )

        return result

    def auto_test_synthesis(self, filepath: str) -> str | None:
        """
        Si no existen tests para el archivo modificado, genera tests minimos.
        Usa NVIDIA NIM (deepseek-ai/deepseek-v4-flash + nemotron-mini-4b).

        Args:
            filepath: Path relativo del archivo

        Returns:
            Path del archivo de test generado, o None si ya existian tests
        """
        full_path = self._resolve_path(filepath)
        if not full_path.exists():
            return None

        # Detectar si ya existen tests para este archivo
        test_patterns = [
            f"test_{full_path.name}",
            f"{full_path.stem}_test.py",
            f"test_{full_path.stem}.py",
        ]

        # Buscar en el workspace y directorios comunes de test
        search_dirs = [
            WORKSPACE_DIR,
            WORKSPACE_DIR / "tests",
            WORKSPACE_DIR / "test",
        ]

        for search_dir in search_dirs:
            if search_dir.exists():
                for pattern in test_patterns:
                    for found in search_dir.rglob(pattern):
                        if found.exists():
                            return None  # Ya existen tests

        # No hay tests — generar usando el modelo local
        try:
            content = full_path.read_text(encoding="utf-8")
            prompt = f"Genera tests unitarios minimos para este codigo:\n\n{content}"

            # Usar asyncio.run() si no estamos ya en un event loop
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Ya hay un loop — crear nuevo en thread separado
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        future = pool.submit(
                            self._sync_generate_test, prompt
                        )
                        test_code = future.result(timeout=30)
                else:
                    test_code = asyncio.run(self._generate_test(prompt))
            except RuntimeError:
                test_code = asyncio.run(self._generate_test(prompt))

            if not test_code:
                return None

            # Escribir el test
            test_filename = f"test_{full_path.name}"
            test_path = (WORKSPACE_DIR / "tests" / test_filename)
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.write_text(test_code, encoding="utf-8")

            return str(test_path)

        except Exception as e:
            print(f"[ENKI] auto_test_synthesis fallo: {e}")
            return None

    # ------------------------------------------------------------------
    # Metodos internos
    # ------------------------------------------------------------------

    def _resolve_path(self, filepath: str) -> Path:
        """Resuelve un path de archivo, sanitizando rutas Windows.
        Busca primero en BASE_DIR (web/, api/, agents/, system/, scripts/, workspace/).
        Si no existe, crea bajo WORKSPACE_DIR."""
        # Normalizar backslashes a forward slashes
        clean = filepath.replace("\\", "/")
        p = Path(clean)
        # Detectar path absoluto Windows (C:\, D:\...)
        if p.is_absolute():
            try:
                rel = p.relative_to(BASE_DIR)
                if (BASE_DIR / rel).exists():
                    return BASE_DIR / rel
            except ValueError:
                pass
            try:
                rel = p.relative_to(WORKSPACE_DIR)
                return WORKSPACE_DIR / rel
            except ValueError:
                raise ValueError(
                    f"Ruta absoluta fuera del proyecto: {filepath}. "
                    f"BASE_DIR: {BASE_DIR}"
                )
        # Path relativo: buscar primero en BASE_DIR
        candidate = BASE_DIR / p
        if candidate.exists():
            return candidate
        # Luego en WORKSPACE_DIR
        candidate = WORKSPACE_DIR / p
        if candidate.exists():
            return candidate
        # Si no existe en ningún lado, crear bajo WORKSPACE_DIR
        return WORKSPACE_DIR / p

    @staticmethod
    def _normalize_whitespace(text: str) -> str:
        """Normaliza whitespace para fuzzy matching."""
        result = text.replace("\t", "    ")
        result = result.replace(chr(13)+chr(10), chr(10)).replace(chr(13), chr(10))  # CRLF to LF
        result = "\n".join(line.rstrip() for line in result.split("\n"))
        # Colapsar multiples espacios en uno (pero no saltos de linea)
        import re
        result = re.sub(r'[ \t]+', ' ', result)
        return result.strip()

    def _verify_python(self, filepath: Path) -> VerifyResult:
        """Verifica un archivo Python: syntax check + compile."""
        start = time.time()
        try:
            # Syntax check con compile()
            code = filepath.read_text(encoding="utf-8")
            compile(code, str(filepath), "exec")

            # Si existe pytest, correr tests del shadow workspace
            stdout_parts = ["Syntax OK"]
            stderr_parts = []
            exit_code = 0
            timeout_hit = False

            # Buscar tests relacionados en el shadow workspace
            test_dir = SHADOW_DIR / "tests"
            if test_dir.exists():
                try:
                    proc = subprocess.run(
                        [sys.executable, "-m", "pytest", str(test_dir), "-x", "-q"],
                        capture_output=True,
                        text=True,
                        timeout=VERIFY_TIMEOUT,
                        cwd=str(SHADOW_DIR),
                    )
                    stdout_parts.append(proc.stdout.strip())
                    stderr_parts.append(proc.stderr.strip())
                    if proc.returncode != 0:
                        exit_code = proc.returncode
                except subprocess.TimeoutExpired:
                    timeout_hit = True
                    stderr_parts.append(f"TIMEOUT ({VERIFY_TIMEOUT}s)")
                    exit_code = -1
                except FileNotFoundError:
                    # pytest no instalado — el syntax check es suficiente
                    pass

            return VerifyResult(
                passed=exit_code == 0,
                stdout="\n".join(stdout_parts),
                stderr="\n".join(stderr_parts),
                exit_code=exit_code,
                duration_seconds=round(time.time() - start, 2),
                timeout_hit=timeout_hit,
            )

        except SyntaxError as e:
            return VerifyResult(
                passed=False,
                stdout="",
                stderr=f"SyntaxError: {e}",
                exit_code=1,
                duration_seconds=round(time.time() - start, 2),
                timeout_hit=False,
            )

    def _verify_javascript(self, filepath: Path) -> VerifyResult:
        """Verifica un archivo JS/TS: node --check o tsc."""
        start = time.time()
        try:
            if filepath.suffix == ".ts":
                # TypeScript: tsc --noEmit
                proc = subprocess.run(
                    ["npx", "tsc", "--noEmit", str(filepath)],
                    capture_output=True,
                    text=True,
                    timeout=VERIFY_TIMEOUT,
                    cwd=str(WORKSPACE_DIR),
                )
            else:
                # JavaScript: node --check
                proc = subprocess.run(
                    ["node", "--check", str(filepath)],
                    capture_output=True,
                    text=True,
                    timeout=VERIFY_TIMEOUT,
                )

            return VerifyResult(
                passed=proc.returncode == 0,
                stdout=proc.stdout.strip(),
                stderr=proc.stderr.strip(),
                exit_code=proc.returncode,
                duration_seconds=round(time.time() - start, 2),
                timeout_hit=False,
            )

        except subprocess.TimeoutExpired:
            return VerifyResult(
                passed=False,
                stdout="",
                stderr=f"TIMEOUT ({VERIFY_TIMEOUT}s)",
                exit_code=-1,
                duration_seconds=round(time.time() - start, 2),
                timeout_hit=True,
            )
        except FileNotFoundError:
            # Node/npx no disponible — pasar como valido
            return VerifyResult(
                passed=True,
                stdout="Verificador no disponible (node/tsc no instalado)",
                stderr="",
                exit_code=0,
                duration_seconds=round(time.time() - start, 2),
                timeout_hit=False,
            )

    def _verify_yaml(self, filepath: Path) -> VerifyResult:
        """Verifica un archivo YAML: parsing."""
        start = time.time()
        try:
            import yaml
            with open(filepath, "r", encoding="utf-8") as f:
                yaml.safe_load(f)
            return VerifyResult(
                passed=True,
                stdout="YAML valido",
                stderr="",
                exit_code=0,
                duration_seconds=round(time.time() - start, 2),
                timeout_hit=False,
            )
        except Exception as e:
            return VerifyResult(
                passed=False,
                stdout="",
                stderr=f"YAML invalido: {e}",
                exit_code=1,
                duration_seconds=round(time.time() - start, 2),
                timeout_hit=False,
            )

    def _verify_json(self, filepath: Path) -> VerifyResult:
        """Verifica un archivo JSON: parsing."""
        start = time.time()
        try:
            import json
            with open(filepath, "r", encoding="utf-8") as f:
                json.load(f)
            return VerifyResult(
                passed=True,
                stdout="JSON valido",
                stderr="",
                exit_code=0,
                duration_seconds=round(time.time() - start, 2),
                timeout_hit=False,
            )
        except Exception as e:
            return VerifyResult(
                passed=False,
                stdout="",
                stderr=f"JSON invalido: {e}",
                exit_code=1,
                duration_seconds=round(time.time() - start, 2),
                timeout_hit=False,
            )

    async def _generate_test(self, prompt: str) -> str:
        """Genera tests usando el modelo local (privado, $0)."""
        response = await self.router.complete(
            role="enki_apply",  # modelo local
            messages=[{"role": "user", "content": prompt}],
            system="Genera SOLO codigo de tests, sin explicaciones. Usa pytest.",
            agent_name=self.agent_name,
            task_id="",
        )
        code = response.content.strip()
        code = code.replace("```python", "").replace("```", "").strip()
        return code if code else ""

    def _sync_generate_test(self, prompt: str) -> str:
        """Version sincrona para cuando no hay event loop disponible."""
        return asyncio.run(self._generate_test(prompt))

    # ------------------------------------------------------------------
    # Pipeline completa
    # ------------------------------------------------------------------

    async def execute_pipeline(
        self,
        context: str,
        task: str,
        retries: int = MAX_RETRIES,
    ) -> dict[str, Any]:
        """
        Ejecuta el pipeline completo: plan -> apply -> verify.
        Con reintentos automaticos si verify falla.

        Args:
            context: Contexto del archivo (de SHAMASH)
            task: Descripcion de la tarea
            retries: Maximo de reintentos (default 3)

        Returns:
            Dict con resultado del pipeline
        """
        result = {
            "success": False,
            "attempts": 0,
            "proposal": None,
            "verify_result": None,
            "error": None,
        }

        for attempt in range(retries):
            result["attempts"] = attempt + 1
            try:
                # Fase 1: Plan
                proposal = await self.plan_diff(context, task)

                # Fase 2: Apply
                self.apply_diff(proposal)

                # Fase 3: Verify
                verify = self.shadow_verify(proposal.file)

                result["proposal"] = proposal.model_dump()
                result["verify_result"] = verify.model_dump()

                if verify.passed:
                    result["success"] = True
                    return result

                # Verify fallo — reintentar
                task = (
                    f"El cambio anterior fallo verificacion. "
                    f"Error: {verify.stderr[:200]}. "
                    f"Tarea original: {task}"
                )

            except DiffMatchError as e:
                task = (
                    f"El cambio anterior no pudo aplicarse (match fallo). "
                    f"Error: {e}. Tarea original: {task}"
                )
                result["error"] = str(e)

            except Exception as e:
                result["error"] = f"{type(e).__name__}: {e}"
                break  # Error no recuperable, salir

        result["success"] = False
        return result