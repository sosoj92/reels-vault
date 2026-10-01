# 14 — Commandes de référence

Exécuter depuis la racine du repo. Windows neuf : `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
(ou `powershell -ExecutionPolicy Bypass -File <script>.ps1`).

## Deployment (installation, Health Check, backup, restauration)
~~~powershell
.\CHECK_LIS.ps1                                   # lecture seule ; 0 READY / 1 WARNINGS / 2 NOT READY
.\CHECK_LIS.ps1 -LiveLlmStatus                    # + llm_setup.py --status (appel Gemini reel si cle)
.\setup_lis.ps1 -Audit
.\setup_lis.ps1 -DryRun
.\setup_lis.ps1 -Install
.\setup_lis.ps1 -Install -Only python,venv,requirements
.\backup_lis.ps1 -DryRun
.\backup_lis.ps1 -Destination E:\LIS-BACKUPS
.\backup_lis.ps1 -IncludeSecrets -Destination E:\LIS-BACKUPS
.\restore_lis.ps1 -BackupPath E:\LIS-BACKUPS\LIS-BACKUP-... -VerifyOnly
.\restore_lis.ps1 -BackupPath E:\LIS-BACKUPS\LIS-BACKUP-... -RestoreSecrets
.\restore_lis.ps1 -BackupPath ... -Root C:\Temp\lis-test -SkipInstall -SkipHealthCheck -NoSystem   # test isole
~~~

## Tests deployment
~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_deploy_*.py" -v
~~~

## Python
~~~powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r deployment-kit\requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-vault.txt   # repli
~~~

## Embeddings
~~~powershell
powershell -ExecutionPolicy Bypass -File .\setup_semantic_model.ps1
.\.venv\Scripts\python.exe vault_embeddings.py
~~~

## Upstreams / Jarvis / Agency
~~~powershell
powershell -ExecutionPolicy Bypass -File .\setup_upstreams.ps1 -Audit
powershell -ExecutionPolicy Bypass -File .\setup_upstreams.ps1          # SHA exacts du lock
.\.venv\Scripts\python.exe install_jarvis_voice_bridge.py --check
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
powershell -ExecutionPolicy Bypass -File .\setup_windows_tasks.ps1 -Only TelegramGateway
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
