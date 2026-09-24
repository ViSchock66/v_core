@echo off
REM Arranca el backend de V-CORE.
REM El directorio se deriva de la ubicacion de este script (%~dp0), no de una
REM ruta absoluta, para que funcione en cualquier checkout.
REM El puerto sale de VCORE_PORT; si no esta definido, usa el default de
REM api/ports.py (8000).
setlocal
cd /d "%~dp0"
set PYTHONPATH=.venv\Lib\site-packages;.
if "%VCORE_PORT%"=="" set VCORE_PORT=8000
echo [V-CORE] Iniciando backend en http://127.0.0.1:%VCORE_PORT%
.venv\Scripts\uvicorn api.main:app --host 127.0.0.1 --port %VCORE_PORT% --reload
