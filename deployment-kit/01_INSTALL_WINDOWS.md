# 01 — Installation Windows

Chemin automatisé (recommandé) : `setup_lis.ps1` fait l'audit, le plan et l'installation de
tout ce qui suit, composant par composant (`SKIP` / `INSTALL` / `WARNING` / `BLOCK`).

~~~powershell
.\setup_lis.ps1 -Audit
.\setup_lis.ps1 -DryRun
.\setup_lis.ps1 -Install
~~~

Une étape isolée : `.\setup_lis.ps1 -Install -Only python,venv,requirements`
(noms d'étapes : voir `README.md`, section NEW INSTALL). Les sections ci-dessous décrivent
ce que fait chaque étape et les commandes manuelles équivalentes.

## 1. Audit avant modification
`setup_lis.ps1 -Audit` vérifie sans installer : winget, Git, Python, `.venv`, paquets,
FFmpeg, Ollama/Qwen, modèle e5, upstreams aux SHA du lock, venv et config Jarvis, `.env`
présent ou non sans l'afficher, clés configurées ou non, Telegram, Notion, `vault.db`,
`mobile-inbox`, Syncthing, tâches Windows.

Sortie : `COMPONENT | STATUS | DETAILS`. Codes : 0 prêt, 1 actions restantes, 2 bloqué.
`.\CHECK_LIS.ps1` complète l'audit (Windows, espace disque, intégrité SQLite, retrieval,
Agency, Security Gate, secrets suivis...).

## 2. Python / venv
Versions validées : `deployment-kit/lis.lock.json`. L'instance de référence tourne en
**Python 3.14.4** (minimum accepté : 3.12). Ne changer de version qu'après validation.

~~~powershell
winget install Python.Python.3.14
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r deployment-kit\requirements-lock.txt
~~~

`requirements-lock.txt` = versions exactes de la `.venv` validée. Repli si une roue manque :
`.\.venv\Scripts\python.exe -m pip install -r requirements-vault.txt yt-dlp gallery-dl faster-whisper`.

Toujours utiliser le Python de `.venv` dans les tâches/scripts. Une `.venv` copiée d'un
autre PC est cassée : `setup_lis.ps1` la signale `BLOCK` (à renommer, jamais supprimée).

## 3. Ingestion locale
yt-dlp, gallery-dl et faster-whisper sont dans `requirements-lock.txt`.

~~~powershell
winget install Gyan.FFmpeg
~~~

Tests :

~~~powershell
.\.venv\Scripts\python.exe -m yt_dlp --version
.\.venv\Scripts\python.exe -m gallery_dl --version
ffmpeg -version
~~~

Ne pas configurer l'ancien iCloud/iPhone (tutoriel legacy).

## 4. Embeddings

~~~powershell
.\setup_semantic_model.ps1
.\.venv\Scripts\python.exe vault_embeddings.py
~~~

Le modèle attendu est `models/multilingual-e5-small` (SHA-256 vérifié, aussi inscrit dans
le lock). Si le sémantique échoue, le lexical doit continuer à fonctionner.

## 5. Upstreams (SHA figés)
Lire `UPSTREAMS.md` puis :

~~~powershell
.\setup_upstreams.ps1 -Audit
.\setup_upstreams.ps1
~~~

Upstreams : sosoj92/reels-vault, msitarzewski/agency-agents, sosoj92/jarvis-assistant-vocal,
chacun au **SHA exact** de `lis.lock.json` (plus de suivi d'origin/main, plus de
`reset --hard`). Le script réapplique ensuite le Jarvis Voice Bridge.

Toute mise à jour future passe par Security Gate + revue + modification volontaire du lock
(`README.md`, section UPSTREAM LOCK).

## 6. Vérifications cœur

~~~powershell
.\CHECK_LIS.ps1
~~~

Vérifie `vault.db` (intégrité, schéma, retrieval lexical et sémantique en lecture seule),
imports critiques, Agency registry, Security Gate, `lis_improvement.py --scan`, absence de
secret suivi par Git.
