"""
diagnostics.py — Prueba de conectividad y estado de V-CORE
Sin dependencias externas (no requiere API keys para el diagnostico basico).
"""
import asyncio
import json
import os
import sys
from pathlib import Path

# Asegurar que podemos importar desde la raiz
sys.path.insert(0, str(Path(__file__).parent.parent))

async def run_diagnostics():
    results = []
    
    # 1. Verificar archivos de configuracion
    print("=" * 60)
    print("DIAGNOSTICO V-CORE")
    print("=" * 60)
    
    config_files = [
        "model_routing.yaml",
        "gate_rules.yaml",
        ".env",
        "VCORE_STATE.json",
        "vcore.db",
    ]
    
    print("\n[1] ARCHIVOS DE CONFIGURACION:")
    for f in config_files:
        p = Path(f)
        exists = p.exists()
        size = p.stat().st_size if exists else 0
        status = "✅" if exists else "❌"
        print(f"  {status} {f} ({size} bytes)")
        if exists and f.endswith(".yaml"):
            content = p.read_text(encoding="utf-8")
            for line in content.split("\n"):
                if "model:" in line:
                    print(f"       -> {line.strip()}")
    
    # 2. API Keys disponibles
    print("\n[2] API KEYS:")
    keys = {
        "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY", ""),
        "DEEPSEEK_API_KEY": os.getenv("DEEPSEEK_API_KEY", ""),
        "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
        "OLLAMA_HOST": os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434"),
    }
    for name, val in keys.items():
        if val and name != "OLLAMA_HOST":
            print(f"  ✅ {name}: {'*' * 8}{val[-4:]}")
        else:
            print(f"  ❌ {name}: NO CONFIGURADA" if not val else f"  ✅ {name}: {val}")
    
    # 3. Router - cargar config
    print("\n[3] LLM ROUTER - CONFIG:")
    try:
        from api.llm_client import get_router
        router = get_router()
        roles = router.config.get("roles", {})
        print(f"  Roles cargados: {len(roles)}")
        for role, cfg in roles.items():
            print(f"    {role}: {cfg['provider']}/{cfg['model']}")
        
        cb = router.config.get("circuit_breaker", {})
        print(f"  Circuit breaker: error_threshold={cb.get('error_threshold')}, min_calls={cb.get('min_calls_threshold')}")
        
        print(f"  Circuit breaker states:")
        for provider, status in router.get_circuit_status().items():
            print(f"    {provider}: {status['state']}")
    except Exception as e:
        print(f"  ❌ Error cargando router: {e}")
    
    # 4. Base de datos
    print("\n[4] BASE DE DATOS (SQLite):")
    try:
        import sqlite3
        db_path = os.getenv("VCORE_DB_PATH", "vcore.db")
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]
        print(f"  Tablas ({len(tables)}):")
        for t in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            icon = "✅" if t in ["llm_usage_log", "agent_execution", "agent_memory", 
                                 "task_graphs", "circuit_breaker_log", "rag_documents",
                                 "agent_state"] else "⬜"
            print(f"    {icon} {t} ({count} registros)")
        conn.close()
    except Exception as e:
        print(f"  ❌ Error en DB: {e}")
    
    # 5. Agentes con codigo real
    print("\n[5] AGENTES CON CODIGO:")
    agent_dirs = [
        ("Orchestrator", "orchestrator.py"),
        ("Planner", "planner.py"),
        ("Curator", "curator.py"),
        ("Retriever", "retriever.py"),
    ]
    for name, main_file in agent_dirs:
        p = Path(f"agents/{name}/{main_file}")
        md = Path(f"agents/{name}/{name}.md")
        if p.exists():
            lines = len(p.read_text().split("\n"))
            print(f"  ✅ {name}/{main_file} ({lines} lines)")
        elif md.exists():
            md_size = md.stat().st_size
            print(f"  🔴 {name}/{main_file} NO EXISTE — solo {name}.md ({md_size} bytes)")
        else:
            print(f"  🔴 {name}/ — carpeta vacia")
    
    # 6. API Endpoints expuestos
    print("\n[6] API SURFACE (FastAPI main.py):")
    try:
        import ast
        with open("api/main.py", "r") as f:
            tree = ast.parse(f.read())
        
        endpoints = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call):
                        if hasattr(decorator.func, 'attr') and decorator.func.attr in ['get', 'post', 'put', 'delete']:
                            method = decorator.func.attr.upper()
                            if decorator.args:
                                path = decorator.args[0].value if hasattr(decorator.args[0], 'value') else str(decorator.args[0])
                                endpoints.append(f"    {method} {path}")
        print(f"  Endpoints encontrados: {len(endpoints)}")
        for ep in sorted(endpoints):
            print(ep)
    except Exception as e:
        print(f"  ❌ Error: {e}")
    
    print("\n" + "=" * 60)
    print("DIAGNOSTICO COMPLETO")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(run_diagnostics())