@echo off
setlocal

rem ROOT = dossier de ce script (fonctionne meme si le Vault est deplace).
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo Python du Vault introuvable : %PY% 1>&2
    exit /b 1
)

rem Repertoire courant force sur ROOT : les scripts qui ouvrent vault.db en
rem relatif ne doivent jamais creer une base vide ailleurs (MacroDroid,
rem planificateur de taches et raccourcis lancent depuis System32 ou autre).
pushd "%ROOT%" || exit /b 1

"%PY%" "%ROOT%\ingest.py" "%ROOT%\mobile-inbox\inbox.txt" --vault "%ROOT%"
if errorlevel 1 goto :fin

"%PY%" "%ROOT%\vault_refresh.py" --vault "%ROOT%" --db "%ROOT%\vault.db"
if errorlevel 1 goto :fin

rem Ingestion Gateway : inbox locale (inbox\files, inbox\sensitive) et videos envoyees
rem par Telegram rapprochees de leur contenu. Non bloquant.
"%PY%" "%ROOT%\vault_ingestion.py" --db "%ROOT%\vault.db" --local-inbox --reconcile

rem Vault Library (Notion) : projection incrementale des sources, sans LLM. Non bloquant.
"%PY%" "%ROOT%\vault_notion_library.py" --db "%ROOT%\vault.db" --confirm --sources-only

"%PY%" "%ROOT%\mentor_worker.py" --limit 20 --confirm
if errorlevel 1 goto :fin

"%PY%" "%ROOT%\mentor_notion_sync.py" --limit 20 --confirm

:fin
set "RC=%errorlevel%"
popd
exit /b %RC%
