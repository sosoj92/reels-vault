@echo off
setlocal

rem Traitements lourds Life Intelligence V2 (claims, raisonnement, index semantique).
rem A lancer hors du temps interactif, par ex. via le planificateur de taches.
rem Ajouter --cloud-review pour la revue cloud (payante, cle requise).

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Python du Vault introuvable : %PY% 1>&2
    exit /b 1
)
pushd "%ROOT%" || exit /b 1
"%PY%" "%ROOT%\life_intelligence_batch.py" %*
set "RC=%errorlevel%"
popd
exit /b %RC%
