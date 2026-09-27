@echo off
setlocal

set "ROOT=%USERPROFILE%\reels-vault"
set "PY=%ROOT%\.venv\Scripts\python.exe"

"%PY%" "%ROOT%\vault_analysis_worker.py" --db "%ROOT%\vault.db" --vault "%ROOT%" --provider auto --limit 2
exit /b %errorlevel%
