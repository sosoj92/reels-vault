# 14 — Commandes de référence

Exécuter depuis la racine du repo.

## Python
~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-vault.txt
~~~

## Embeddings
~~~powershell
powershell -ExecutionPolicy Bypass -File .\setup_semantic_model.ps1
.\.venv\Scripts\python.exe vault_embeddings.py
~~~

## Upstreams / Jarvis / Agency
~~~powershell
powershell -ExecutionPolicy Bypass -File .\setup_upstreams.ps1
.\.venv\Scripts\python.exe install_jarvis_voice_bridge.py
.\.venv\Scripts\python.exe vault_agent_registry.py --list
~~~

## LLM
~~~powershell
.\.venv\Scripts\python.exe llm_setup.py
.\.venv\Scripts\python.exe llm_setup.py --status
~~~

## Telegram
~~~powershell
.\.venv\Scripts\python.exe telegram_setup.py
.\.venv\Scripts\python.exe telegram_gateway.py
~~~

## Notion
~~~powershell
.\.venv\Scripts\python.exe vault_notion_library.py --confirm
~~~

## Agency packet
~~~powershell
.\.venv\Scripts\python.exe vault_orchestrator.py --packet --markdown --vault "question" > consultation.md
~~~

## Security
~~~powershell
.\.venv\Scripts\python.exe vault_security_gate.py CHEMIN
.\.venv\Scripts\python.exe lis_improvement.py --scan
~~~

## Windows tasks
~~~powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows_tasks.ps1
Get-ScheduledTask | Where-Object { $_.TaskName -like "Reels Vault*" } | Select-Object TaskName, State
~~~

## Git
~~~powershell
git status
git branch --show-current
git log --oneline -10
git switch -c <branche>
~~~

Pas de force push par défaut et pas de développement direct sur main.
