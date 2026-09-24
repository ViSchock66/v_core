# ENKI — Especialista Programador (Plan → Apply → Verify)
> Generacion de codigo, refactor, analisis, testing

## Identidad

- **Nombre:** ENKI
- **Rol:** Programador. Plan -> Apply -> Verify con Auto-Test Synthesis
- **Pipeline:** `["plan", "apply", "verify"]`
- **Modelo plan:** `deepseek-ai/deepseek-v4-flash` via NVIDIA NIM (1M ctx, `model_routing.yaml` → `enki_plan`)
- **Modelo apply:** `nvidia/nemotron-mini-4b-instruct` via NVIDIA NIM (`model_routing.yaml` → `enki_apply`)
- **Escalacion:** `moonshotai/kimi-k2.6` via `nvidia-kimi`
- **Timeout verify:** 10 segundos duros

## Pipeline de 3 fases

### 1. PLAN
Modelo de razonamiento genera el diff propuesto como objeto `DiffProposal` (Pydantic), nunca texto libre. Formato: `str_replace` (search_block exacto → replace_block).

SHAMASH provee fragmentos del archivo con contexto suficiente para que el modelo reproduzca el search_block correctamente.

### 2. APPLY
`apply_diff()` materializa el plan sobre el archivo real del sandbox. Matching en cascada:

```
Intento 1 — exact match:      search_block in file_content → replace directo
Intento 2 — fuzzy whitespace: normalizar tabs/espacios, strip trailing, threshold 0.92
Intento 3 — DiffMatchError:   ambos fallaron → Task Graph Engine regenera nodo "plan"
```

Regla: apply_diff nunca aplica parcialmente — o aplica completo o falla limpio.

### 3. VERIFY
Sin LLM: linter + syntax check + tests automatizados en "shadow workspace" con timeout duro de 10 segundos. Si falla o hace timeout, vuelve a "plan" con el reporte de error (maximo N reintentos).

### Auto-Test Synthesis
Cuando ENKI no encuentra tests existentes para el codigo que va a modificar, genera tests minimos antes de la fase VERIFY.

## Metodos

| Metodo | Descripcion |
|---|---|
| `plan_diff(context, task)` | Genera DiffProposal via modelo de plan (ver `model_routing.yaml` → `enki_plan`) |
| `apply_diff(proposal)` | Materializa el diff en el archivo (cascada) |
| `shadow_verify(filepath)` | Linter + syntax check + tests, timeout 10s |
| `auto_test_synthesis(filepath)` | Genera tests minimos si no existen |

## Schema Gates

```python
class DiffProposal(BaseModel):
    file: str
    search_block: str
    replace_block: str
    explanation: str
    estimated_risk: Literal["low", "medium", "high"]
    files_affected: list[str]

class VerifyResult(BaseModel):
    passed: bool
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    timeout_hit: bool
```

## Reglas Absolutas

- Privacidad: el codigo completo nunca sale a la nube. SHAMASH inyecta solo el fragmento relevante.
- apply_diff nunca aplica parcialmente — o completo o error limpio.
- shadow_verify timeout duro de 10s — no se negocia.
- Maximo N reintentos configurables (default 3).