@echo off
setlocal

set "ROOT=%USERPROFILE%\reels-vault"
set "PY=%ROOT%\.venv\Scripts\python.exe"

"%PY%" "%ROOT%\ingest.py" "%ROOT%\mobile-inbox\inbox.txt" --vault "%ROOT%"
if errorlevel 1 exit /b %errorlevel%

"%PY%" "%ROOT%\vault_refresh.py" --vault "%ROOT%" --db "%ROOT%\vault.db"
exit /b %errorlevel%
