@echo off
setlocal

rem Passage quotidien du Mentor proactif (tache planifiee, PC au repos) :
rem evenements -> candidats -> shortlist -> Qwen + Agency Agents (mode C) -> suggestions.
rem Aucun envoi ici : la passerelle Telegram livre selon le budget anti-spam.
rem Sans mentor_proactive.enabled, --confirm est refuse (aucune ecriture).

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Python du Vault introuvable : %PY% 1>&2
    exit /b 1
)
pushd "%ROOT%" || exit /b 1
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
"%PY%" "%ROOT%\mentor_proactive.py" --confirm --with-agents --review-limit 2 >> "%ROOT%\logs\mentor_daily.log" 2>&1
set "RC=%errorlevel%"
popd
exit /b %RC%
