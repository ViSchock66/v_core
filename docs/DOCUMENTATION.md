# Documentación de V-CORE

Este archivo define qué documentos son contexto operativo y cuáles son solo
historia. Su objetivo es evitar que un modelo trate snapshots o auditorías
pasadas como estado actual.

## Fuentes vigentes

| Documento | Uso permitido |
|---|---|
| `../README.md` | Puerta de entrada: qué es, cómo se instala y se corre. |
| `../AGENTS.md` | Instrucciones de trabajo y límites de edición. |
| `../VCORE_ARCHITECTURE.md` | Estructura estable, límites y fuentes de verdad. |
| `../VCORE_ROADMAP.md` | Único backlog y priorización activos. |
| `CHANGELOG.md` | Historia de releases; no describe estado actual. |
| `SESSION_ISOLATION_ARCHITECTURE.md` | Diseño detallado de aislamiento de sesiones. |
| `ROADMAP_TAURI.md` | Plan diferido de escritorio; no forma parte del runtime actual. |
| `PLAN_REFACTOR_V2.md` | Plan maestro del refactor v2 (frontend web/, permisos/HITL). |
| `COMPARATIVA_HARNESSES.md` | Comparativa con harnesses del estado del arte; contexto de diseño. |
| `DISENO_PERMISOS.md` | Diseño del subsistema de permisos/HITL (gate v2, aprobaciones, sandbox). |
| `ANALISIS_REFACTOR_FRONTEND.md` | Análisis del frontend v1 que motivó el refactor a web/; referencia de decisiones. |
| `audit_frontend_checklist.md` | Checklist operativo de QA visual del frontend. |
| `FILE_MANIFEST.md` | Inventario de archivos del repo. |
| `agents/*/*.md` | Contrato y responsabilidad de cada agente. |

`DOCUMENTATION.md` es la lista autoritativa: si un `.md` vive en `docs/` y no
aparece en esta tabla, está mal ubicado — o se declara acá o se mueve a
`docs/_archive/`. Al 2026-09-24 no hay documentos sin declarar.

Los documentos de deuda técnica, ruta estratégica, auditoría de puntos débiles,
informes de sesión y cruces de hallazgos fueron consolidados en
`../VCORE_ROADMAP.md`. Sus snapshots se preservan en `docs/_archive/` y no
deben usarse para priorizar trabajo.

## Fuentes vivas fuera de Markdown

- Versión: `VCORE_STATE.json` y `api/version.py`.
- Routing, roles, presets y modelo activo: `model_routing.yaml`.
- Configuración base de sesiones: `sessions/default/model_routing.yaml`.
- Comportamiento: código y endpoints del servidor vivo.

No copiar valores mutables —modelo lead, conteos, cantidad de rutas, uso o
estado de tests— a documentos narrativos. Referenciar su fuente viva.

## Reglas de mantenimiento

1. Un estado o pendiente tiene un único dueño documental.
2. `VCORE_ROADMAP.md` es el único registro de trabajo futuro o abierto.
3. `CHANGELOG.md` es fechado e histórico: nunca se usa para deducir el
   estado actual.
4. Los documentos en `docs/_archive/` no son contexto operativo. Solo se leen
   para investigación histórica explícita.
5. Los inventarios de archivos se generan desde disco bajo demanda; no se
   mantienen como Markdown manual.
6. Al retirar una función, rol o documento, buscar referencias en código,
   configuración y documentación antes de cerrar el cambio.
