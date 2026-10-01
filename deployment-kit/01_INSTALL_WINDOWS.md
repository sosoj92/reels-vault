# 01 — Installation Windows

## 1. Audit avant modification
Claude Code vérifie sans installer : Windows/architecture, PowerShell, Git, Python, espace disque, état Git/branche/SHA, présence de .env sans l'afficher, .venv, FFmpeg, yt-dlp, gallery-dl, Ollama, upstreams, embeddings et tâches Windows.

Sortie : COMPOSANT | ÉTAT | ACTION, puis `READY FOR INSTALLATION` ou `BLOCKED`.

## 2. Python / venv
L'instance de référence a été construite autour de Python 3.12. Ne changer de version qu'après validation.

~~~powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-vault.txt
~~~

Toujours utiliser le Python de `.venv` dans les tâches/scripts.

## 3. Ingestion locale

~~~powershell
.\.venv\Scripts\python.exe -m pip install yt-dlp gallery-dl faster-whisper
winget install Gyan.FFmpeg
~~~

Tests :

~~~powershell
.\.venv\Scripts\python.exe -m yt_dlp --version
.\.venv\Scripts\python.exe -m gallery_dl --version
ffmpeg -version
~~~

Ne pas configurer l'ancien iCloud/iPhone du README.

## 4. Embeddings

~~~powershell
powershell -ExecutionPolicy Bypass -File .\setup_semantic_model.ps1
.\.venv\Scripts\python.exe vault_embeddings.py
~~~

Le modèle attendu est `models/multilingual-e5-small`. Si le sémantique échoue, le lexical doit continuer à fonctionner.

## 5. Upstreams
Lire `UPSTREAMS.md` puis :

~~~powershell
powershell -ExecutionPolicy Bypass -File .\setup_upstreams.ps1
~~~

Upstreams : sosoj92/reels-vault, msitarzewski/agency-agents, sosoj92/jarvis-assistant-vocal.

Conserver les SHA réellement installés dans le rapport. Toute mise à jour future passe par revue/Security Gate.

## 6. Vérifications cœur
Vérifier `vault.db`, imports critiques, Agency registry dynamique, Security Gate, `lis_improvement.py --scan` et absence de secret tracké.
