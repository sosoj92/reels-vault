# 12 — Troubleshooting

## Premier réflexe

~~~powershell
.\CHECK_LIS.ps1
~~~

Lecture seule : la première ligne `FAIL` ou `WARNING` indique généralement la cause.

## « l'exécution de scripts est désactivée sur ce système »
~~~powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
~~~
ou `powershell -ExecutionPolicy Bypass -File .\CHECK_LIS.ps1`.

## setup_upstreams.ps1 affiche BLOCK
Un upstream a des modifications locales hors bridge, un remote inattendu ou un dossier non
Git. Rien n'a été touché. Inspecter (`git -C external\<nom> status`), puis soit annuler les
modifications soi-même, soit `.\setup_upstreams.ps1 -Force` (patch sauvegardé dans
`backups\upstream-patches\` avant le checkout).

## Backup : chemin introuvable / trop long
Les noms de vidéos dans `images/` dépassent parfois 260 caractères. `backup_lis.ps1` et
`restore_lis.ps1` les gèrent (copie et SHA-256 via `deployment-kit/lib/lis_deploy.py`).
Si une ancienne copie manuelle échoue, utiliser ces scripts plutôt que `Copy-Item`.

## Restauration bloquée : « une base existe deja »
`restore_lis.ps1` ne remplace jamais un `vault.db` différent du backup. Si c'est voulu :
`-ReplaceVault` (l'ancienne base est déplacée dans `backups\vault-before-restore-*`).

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
.\setup_lis.ps1 -Install -Only requirements
~~~

(installe `deployment-kit\requirements-lock.txt`, repli `requirements-vault.txt`).

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
.\.venv\Scripts\python.exe install_jarvis_voice_bridge.py --check
.\.venv\Scripts\python.exe install_jarvis_voice_bridge.py
~~~

## Mobile
MacroDroid OK mais rien sur PC → inspecter Syncthing.  
Fichier sur PC mais rien dans Vault → tester `inbox.bat` puis logs ingestion/yt-dlp/FFmpeg.

## Git divergent
Ne pas force-push. Sauvegarder le HEAD local, repartir du remote validé et réappliquer seulement les changements uniques. Voir `prompts/04_UPDATE_SAFELY.md`.
