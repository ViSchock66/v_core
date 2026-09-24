# V-CORE File Manifest

> Generado desde el disco el 2026-09-24. **No editar a mano** — regenerar
> cuando cambie la estructura (ver `docs/DOCUMENTATION.md`, regla 5).
>
> Las lineas son conteos del momento y quedan obsoletos rapido; sirven para
> ubicar archivos, no como fuente de metricas. El estado real del sistema se
> consulta al servidor vivo (`GET /health`, `GET /system/model`).

```
V-Core/
├── .github/
│   └── workflows/
│       └── ci.yml (95 lines)
├── agents/
│   ├── Curator/
│   │   ├── SKILL/
│   │   │   └── Contexto SKILL Curator.yaml (47 lines)
│   │   ├── Curator.md (45 lines)
│   │   ├── curator.py (322 lines)
│   │   └── memory.py (482 lines)
│   ├── Orchestrator/
│   │   ├── SKILL/
│   │   │   └── Orquesta SKILL Orchestrator.yaml (62 lines)
│   │   ├── Orchestrator.md (59 lines)
│   │   └── orchestrator.py (2152 lines)
│   ├── Planner/
│   │   ├── SKILL/
│   │   │   └── Programacion SKILL Planner.yaml (71 lines)
│   │   ├── Planner.md (72 lines)
│   │   └── planner.py (868 lines)
│   └── Retriever/
│       ├── SKILLS/
│       │   ├── Filesystem SKILL Retriever.yaml (34 lines)
│       │   ├── Impact mapping SKILL Retriever .yaml (44 lines)
│       │   └── Index SKILL Retriever.yaml (49 lines)
│       ├── Retriever.md (49 lines)
│       └── retriever.py (685 lines)
├── api/
│   ├── __init__.py (0 lines)
│   ├── approval_broker.py (202 lines)
│   ├── embed.py (354 lines)
│   ├── events.py (407 lines)
│   ├── files_api.py (479 lines)
│   ├── llm_client.py (1879 lines)
│   ├── main.py (1721 lines)
│   ├── policy.py (112 lines)
│   ├── ports.py (70 lines)
│   ├── search_api.py (194 lines)
│   ├── shell_api.py (223 lines)
│   ├── state_bridge.py (385 lines)
│   └── version.py (43 lines)
├── docs/
│   ├── ANALISIS_REFACTOR_FRONTEND.md (405 lines)
│   ├── CHANGELOG.md (471 lines)
│   ├── COMPARATIVA_HARNESSES.md (428 lines)
│   ├── DISENO_PERMISOS.md (634 lines)
│   ├── DOCUMENTATION.md (56 lines)
│   ├── FILE_MANIFEST.md (158 lines)
│   ├── PLAN_REFACTOR_V2.md (642 lines)
│   ├── ROADMAP_TAURI.md (124 lines)
│   ├── SESSION_ISOLATION_ARCHITECTURE.md (258 lines)
│   └── audit_frontend_checklist.md (54 lines)
├── scripts/
│   ├── _npm.py (32 lines)
│   ├── audit.py (507 lines)
│   ├── audit_live.py (136 lines)
│   ├── audit_public.py (281 lines)
│   ├── capture_frontend.py (222 lines)
│   ├── diagnostics.py (143 lines)
│   ├── e2e_chat_full.py (94 lines)
│   ├── e2e_chat_probe.py (93 lines)
│   ├── e2e_hitl.py (105 lines)
│   ├── init_db.py (237 lines)
│   ├── probe_models.py (100 lines)
│   ├── probe_tools.py (103 lines)
│   ├── test_e2e_v2.py (155 lines)
│   ├── test_event_protocol.py (158 lines)
│   ├── test_frontend.py (132 lines)
│   ├── test_frontend_v2.py (140 lines)
│   ├── test_fs_policy.py (55 lines)
│   ├── test_gate_v2.py (87 lines)
│   ├── test_gate_wiring.py (155 lines)
│   ├── test_policy_commands.py (107 lines)
│   ├── test_theme.py (77 lines)
│   ├── test_visual_audit.py (59 lines)
│   └── watchdog.py (46 lines)
├── sessions/
│   └── default/
│       ├── .env.keys.example (3 lines)
│       ├── gate_rules.yaml (85 lines)
│       ├── model_routing.yaml (415 lines)
│       └── state.json (9 lines)
├── system/
│   ├── mcp_servers/
│   │   ├── vcore_audit_mcp.py (107 lines)
│   │   ├── vcore_fs_mcp.py (368 lines)
│   │   └── vcore_shell_mcp.py (134 lines)
│   ├── __init__.py (1 lines)
│   ├── mcp_manager.py (345 lines)
│   ├── observability.py (506 lines)
│   ├── policy_commands.py (480 lines)
│   ├── proactive_agent.py (384 lines)
│   ├── task_graph_engine.py (899 lines)
│   ├── vcore_mcp_bridge.py (145 lines)
│   └── visual_auditor.py (330 lines)
├── web/
│   ├── src/
│   │   ├── components/
│   │   │   ├── ApprovalCard.tsx (118 lines)
│   │   │   ├── ArtifactViewer.tsx (238 lines)
│   │   │   ├── Composer.tsx (96 lines)
│   │   │   ├── ErrorBoundary.tsx (55 lines)
│   │   │   ├── RunList.tsx (161 lines)
│   │   │   ├── RunView.tsx (120 lines)
│   │   │   ├── Sidebar.tsx (299 lines)
│   │   │   ├── SystemPanel.tsx (242 lines)
│   │   │   ├── ToolCallCard.tsx (104 lines)
│   │   │   ├── WorkspacePanel.tsx (166 lines)
│   │   │   └── ui.tsx (118 lines)
│   │   ├── lib/
│   │   │   ├── api.ts (282 lines)
│   │   │   ├── protocol.ts (117 lines)
│   │   │   └── reduce.ts (366 lines)
│   │   ├── state/
│   │   │   ├── chat.ts (228 lines)
│   │   │   └── theme.ts (74 lines)
│   │   ├── styles/
│   │   │   └── index.css (328 lines)
│   │   ├── App.tsx (172 lines)
│   │   └── main.tsx (36 lines)
│   ├── index.html (13 lines)
│   ├── package-lock.json (4408 lines)
│   ├── package.json (41 lines)
│   ├── tsconfig.json (27 lines)
│   ├── tsconfig.tsbuildinfo (1 lines)
│   └── vite.config.ts (57 lines)
├── workspace/
│   └── .gitkeep (0 lines)
├── .env.example (45 lines)
├── .gitignore (53 lines)
├── AGENTS.md (52 lines)
├── LICENSE (21 lines)
├── README.md (279 lines)
├── VCORE_ARCHITECTURE.md (103 lines)
├── VCORE_ROADMAP.md (128 lines)
├── VCORE_STATE.json (46 lines)
├── gate.py (764 lines)
├── gate_rules.yaml (180 lines)
├── mcp_config.json (28 lines)
├── model_routing.yaml (415 lines)
├── requirements.txt (51 lines)
├── run_clean.bat (4 lines)
├── start_clean.sh (13 lines)
├── start_vcore.bat (12 lines)
├── test_audit.py (2 lines)
└── vcore.py (1235 lines)
```
