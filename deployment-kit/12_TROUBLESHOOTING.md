# 12 — Troubleshooting

## Mauvais dossier
~~~powershell
Get-Location
git status
~~~
Relancer Claude Code depuis la racine du repo.

## Mauvais Python
Toujours préférer :

~~~powershell
.\.venv\Scripts\python.exe
~~~

## Dépendances absentes
~~~powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-vault.txt
~~~

## FFmpeg introuvable
Fermer/réouvrir PowerShell puis `ffmpeg -version`.

## Qwen UNAVAILABLE
Vérifier Ollama, modèle, RAM/CPU puis :

~~~powershell
.\.venv\Scripts\python.exe llm_setup.py --status
~~~

## Telegram silencieux
Vérifier tâche Gateway, ancien processus, `.env`, whitelist, logs et SHA actif. Après changement de code, redémarrer la gateway.

## Réponse Telegram coupée
Le code final doit découper les messages et signaler une troncature modèle. Utiliser `prompts/03_DIAGNOSE_LIS.md` avant toute modification.

## Notion en retard
~~~powershell
.\.venv\Scripts\python.exe vault_notion_library.py --confirm
~~~

## Jarvis ne route pas
~~~powershell
.\.venv\Scripts\python.exe install_jarvis_voice_bridge.py
~~~

## Mobile
MacroDroid OK mais rien sur PC → inspecter Syncthing.  
Fichier sur PC mais rien dans Vault → tester `inbox.bat` puis logs ingestion/yt-dlp/FFmpeg.

## Git divergent
Ne pas force-push. Sauvegarder le HEAD local, repartir du remote validé et réappliquer seulement les changements uniques. Voir `prompts/04_UPDATE_SAFELY.md`.
