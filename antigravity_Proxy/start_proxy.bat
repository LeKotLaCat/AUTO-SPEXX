@echo off
chcp 65001 > nul
echo ===================================================
echo   Starting Antigravity Proxy
echo ===================================================

cd /d "%~dp0"

if not exist .env (
    if exist .env.example (
        copy .env.example .env > nul
        echo Created .env from .env.example
    )
)

python -c "import os, sys, subprocess; env_vars=os.environ.copy(); [env_vars.update({k.strip(): v.strip().strip('\"').strip('\'')}) for k, _, v in (line.partition('=') for line in open('.env', 'r', encoding='utf-8') if line.strip() and not line.startswith('#')) if k.strip()] if os.path.exists('.env') else None; sys.exit(subprocess.call([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000', '--log-level', 'info'], env=env_vars))"

pause
