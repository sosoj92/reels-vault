# Life Intelligence System — Deployment Kit

Ce dossier documente **et outille** la reproduction, l'installation, l'utilisation, la
migration et la restauration du Life Intelligence System (LIS) sur Windows.

Il décrit la recette finale qui a survécu, pas l'historique de tous les essais. Aucune
fonctionnalité nouvelle : uniquement l'existant, figé (`lis.lock.json`), vérifiable
(`CHECK_LIS.ps1`), sauvegardable (`backup_lis.ps1`) et restaurable (`restore_lis.ps1`).

## Deux parcours

| Parcours | Résultat | Automatisé par | Guide | Prompt Claude Code |
|---|---|---|---|---|
| **A — Nouvelle installation** : PC Windows neuf → Vault neuf | `LIS READY` | `setup_lis.ps1` puis `CHECK_LIS.ps1` | `01_INSTALL_WINDOWS.md` | `prompts/01_INSTALL_WINDOWS.md` |
| **B — Restauration / migration** : nouveau PC → GitHub + backup personnel | `LIS RESTORED` | `restore_lis.ps1` (appelle `setup_lis.ps1` et `CHECK_LIS.ps1`) | `10_BACKUP_RESTORE.md`, `11_MIGRATE_NEW_PC.md` | `prompts/02_RESTORE_EXISTING_LIS.md` |

Les scripts font tout ce qui est automatisable ; les étapes humaines (clés masquées,
BotFather, téléphone, voix, connecteurs ChatGPT) sont signalées `MANUAL TEST REQUIRED`
ou `NOT CONFIGURED`, et détaillées dans les guides numérotés.

## Architecture de référence
- GitHub contient le code.
- `vault.db` est la mémoire canonique.
- Notion est le cockpit/projection.
- Telegram est l'interface rapide/mobile.
- ChatGPT est une interface d'analyse riche via connecteurs, pas la mémoire.
- Jarvis est l'interface vocale.
- Claude Code est l'outil principal de construction, installation, maintenance et diagnostic.
- LLM Router garde les modèles interchangeables.
- Agency Agents sont des playbooks de raisonnement, pas des modèles.
- MacroDroid + Syncthing forment le pipeline mobile Android.
- Tailscale ne fait pas partie de la stack finale.
- Mistral n'est pas un fournisseur runtime de référence.

## Ordre de lecture
1. 00_START_HERE.md
2. 13_STACK_MATRIX.md
3. 01_INSTALL_WINDOWS.md
4. 02_LLM_AND_MODELS.md
5. 03_TELEGRAM.md
6. 04_NOTION_AND_CHATGPT.md
7. 05_JARVIS.md
8. 06_ANDROID_MACRODROID_SYNCTHING.md (+ `android/` : export MacroDroid, Syncthing)
9. 07_WINDOWS_AUTOMATION.md
10. 08_SECURITY.md
11. 09_HEALTH_CHECK.md
12. 10_BACKUP_RESTORE.md
13. 11_MIGRATE_NEW_PC.md
14. 12_TROUBLESHOOTING.md
15. 14_COMMAND_REFERENCE.md

Le dossier `prompts/` contient les prompts maîtres pour Claude Code. Ce README sert de
référence détaillée des scripts (sections ci-dessous).

## Sources de vérité techniques
Prioriser FINAL_ARCHITECTURE.md, docs/INGESTION.md, docs/LLM_ROUTER.md,
docs/NOTION_VAULT_LIBRARY.md, docs/MENTOR_PROACTIVE_V3.md,
docs/AGENCY_SECURITY_SELF_IMPROVEMENT.md, UPSTREAMS.md, .env.example,
`deployment-kit/lis.lock.json` et les scripts actuels (`setup_*.ps1`, `CHECK_LIS.ps1`,
`backup_lis.ps1`, `restore_lis.ps1`).

Le README.md racine décrit désormais le LIS actuel ; l'ancien tutoriel Reels Vault
(Cowork/iPhone/iCloud) est conservé comme **legacy** dans
`docs/legacy/REELS_VAULT_TUTORIAL.md` et ne fait plus référence.

## Outillage

| Fichier | Rôle |
|---|---|
| `lis.lock.json` | versions **exactes** validées : SHA du LIS, SHA des 3 upstreams, Python, Qwen, e5, outils |
| `requirements-lock.txt` | `pip freeze` exact de la `.venv` validée (Python 3.14.4) |
| `lib/LisDeploy.psm1` | fonctions communes PowerShell (rapports, lock, git, chiffrement des secrets) |
| `lib/lis_deploy.py` | outils Python *stdlib* : backup SQLite, `integrity_check`, manifest SHA-256 et copies (chemins > 260 caractères), santé du Vault, scan de secrets |
| `android/` | MacroDroid + Syncthing (pipeline mobile de référence) |
| `reports/` | rapports locaux des scripts (ignorés par Git) |

Scripts à la racine du repo :

| Commande | Écrit ? | Rôle |
|---|---|---|
| `.\CHECK_LIS.ps1` | non (rapport seulement) | Health Check complet, codes 0/1/2 |
| `.\setup_lis.ps1 -Audit \| -DryRun \| -Install` | seulement avec `-Install` | orchestrateur d'installation idempotent |
| `.\setup_upstreams.ps1 [-Audit] [-DryRun] [-Force]` | sauf `-Audit`/`-DryRun` | upstreams aux SHA du lock + Jarvis Voice Bridge |
| `.\backup_lis.ps1 [-IncludeSecrets]` | nouveau dossier de backup | backup cohérent et vérifié |
| `.\restore_lis.ps1 -BackupPath ... [-VerifyOnly]` | sauf `-VerifyOnly` | restauration depuis GitHub + backup |
| `.\setup_windows_tasks.ps1 [-Only ...]` | tâches planifiées | ingestion horaire, Telegram Gateway, Mentor Daily |

> **Politique d'exécution PowerShell.** Sur un Windows neuf, les scripts `.ps1` sont
> bloqués par défaut. Lance-les avec
> `powershell -ExecutionPolicy Bypass -File .\CHECK_LIS.ps1` (idem pour les autres),
> ou une fois pour toutes : `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
> Les exemples ci-dessous supposent l'une de ces deux options.

---

## HEALTH CHECK

```powershell
.\CHECK_LIS.ps1
```

Lecture seule : `vault.db` est ouvert en `mode=ro`, aucune donnée de test n'est écrite,
aucun secret n'est affiché (`CONFIGURED` / `NOT CONFIGURED`), aucun appel cloud.
`llm_setup.py --status` n'est **pas** lancé (il envoie une vraie requête Gemini quand la
clé existe) : `.\CHECK_LIS.ps1 -LiveLlmStatus` pour le lancer volontairement.

Sections : GIT, WINDOWS, PYTHON, VAULT, AGENCY / SECURITY, LLM, TELEGRAM,
NOTION / CHATGPT, JARVIS, ANDROID, WINDOWS TASKS, BACKUP.

```
COMPONENT                          | STATUS               | DETAILS
sqlite integrity_check             | OK                   | ok (57 tables)
routage sensible                   | OK                   | sensible / personnel / secret : local uniquement
macrodroid export                  | MANUAL TEST REQUIRED | EXPORT REQUIRED FROM ANDROID DEVICE
```

| Statut | Sens |
|---|---|
| `OK` | vérifié |
| `WARNING` | fonctionne, mais à regarder |
| `FAIL` | composant cassé ou absent |
| `NOT CONFIGURED` | optionnel non configuré (Gemini, Anthropic, Notion...) |
| `MANUAL TEST REQUIRED` | nécessite un humain / le téléphone / la voix |

Codes de sortie : `0` = **LIS READY**, `1` = **READY WITH WARNINGS**, `2` = **NOT READY**.

Options : `-SkipSystem` (sans tâches/Ollama/Syncthing, pour CI), `-SkipSemantic`,
`-Db <chemin>` (autre base, tests), `-JsonOut <fichier>`, `-NoReport`, `-BackupDir <dossier>`.

---

## NEW INSTALL (cas A : PC Windows neuf)

```powershell
winget install Git.Git                     # si git absent
git clone https://github.com/Roseytech/reels-vault.git $HOME\reels-vault
cd $HOME\reels-vault
.\setup_lis.ps1 -Audit                     # ce qui est present / absent
.\setup_lis.ps1 -DryRun                    # plan detaille, rien n'est modifie
.\setup_lis.ps1 -Install                   # applique ce qui manque, puis CHECK_LIS.ps1
```

`setup_lis.ps1` détecte chaque composant avant d'agir :
`SKIP` (présent et correct), `INSTALL` (absent → installé), `WARNING` (version
différente mais compatible, rien n'est remplacé), `BLOCK` (situation risquée, rien
n'est touché), `MANUAL TEST REQUIRED` / `NOT CONFIGURED` (étape humaine).

| Étape (`-Only`) | Installation automatique |
|---|---|
| `winget` | non (App Installer du Microsoft Store si absent) |
| `git` | `winget install Git.Git` |
| `python` | `winget install Python.Python.3.14` |
| `venv` | `python -m venv .venv` (une `.venv` cassée → BLOCK, jamais supprimée) |
| `requirements` | `pip install -r deployment-kit\requirements-lock.txt` (repli : `requirements-vault.txt` + yt-dlp, gallery-dl, faster-whisper) |
| `ffmpeg` | `winget install Gyan.FFmpeg` |
| `ollama` | `winget install Ollama.Ollama` |
| `qwen` | `ollama pull qwen3:1.7b` |
| `embeddings` | `.\setup_semantic_model.ps1` (SHA-256 vérifié) |
| `upstreams` | `.\setup_upstreams.ps1` (SHA exacts + Jarvis Voice Bridge) |
| `jarvis-venv` | `py -3.13 -m venv` + `pip install -r requirements.txt` de Jarvis |
| `jarvis-config` | manuel (secrets restaurés, ou `config.example.yaml`) |
| `env` | copie `.env.example` → `.env` **seulement si `.env` absent** |
| `llm-keys` | manuel : `.\.venv\Scripts\python.exe llm_setup.py` (saisie masquée) |
| `telegram` | manuel : `.\.venv\Scripts\python.exe telegram_setup.py` |
| `notion` | manuel : variable utilisateur `NOTION_TOKEN` (optionnel, projection seulement) |
| `vault` | base vide avec le schéma `vault_db` **seulement si `vault.db` absent** |
| `mobile-inbox` | crée `mobile-inbox\inbox.txt` vide |
| `syncthing` | `winget install Syncthing.Syncthing` ; appairage manuel |
| `android` | manuel (MacroDroid, voir `android/README.md`) |
| `tasks` | `.\setup_windows_tasks.ps1 -Only <manquantes>` (Gateway seulement si Telegram configuré) |

Après une installation par winget, ouvre un **nouveau terminal** si une commande
n'est pas encore trouvée, puis relance `.\setup_lis.ps1 -Install` (idempotent).

Étapes humaines d'une installation neuve, dans l'ordre :

1. `.\.venv\Scripts\python.exe llm_setup.py` (clés Gemini / OpenRouter, optionnelles)
2. `.\.venv\Scripts\python.exe telegram_setup.py` puis `.\setup_windows_tasks.ps1 -Only TelegramGateway`
3. Jarvis : `external\jarvis-assistant-vocal\config.yaml` (voir son `INSTALL_WITH_AI.md`)
4. Syncthing + MacroDroid : `android/README.md`
5. `.\CHECK_LIS.ps1`

---

## BACKUP

```powershell
.\backup_lis.ps1                                  # vers %USERPROFILE%\LIS-BACKUPS
.\backup_lis.ps1 -Destination E:\LIS-BACKUPS      # disque externe
.\backup_lis.ps1 -IncludeSecrets                  # + secrets chiffres (passphrase demandee)
.\backup_lis.ps1 -DryRun                          # liste, n'ecrit rien
```

Autres options : `-ExcludeImages` (sans `images/`, ~120 Mo), `-NoSystem` (tests : ignore
tâches Windows, Syncthing et variables utilisateur), `-Root`, `-Db`, `-Python`.

```
LIS-BACKUP-YYYYMMDD-HHMMSS/
    vault.db                  instantané via l'API de sauvegarde SQLite (jamais une copie brute)
    manifest.json             date UTC, hôte, SHA LIS + upstreams, versions, SHA-256 et taille de chaque fichier
    lis.lock.json
    installation-report.json  Python, pip freeze, modèles Ollama, tâches, SHA (sans secret)
    health-check.json         dernier rapport CHECK_LIS (s'il existe)
    config/                   env.redacted (.env sans valeurs sensibles), user-env-names.json (noms seuls), tasks/*.xml
    macrodroid/               export .macro, ou EXPORT_REQUIRED.txt
    syncthing/                syncthing-mapping.json (dossiers + appareils, sans clé)
    personal-files/           raw, images, index.md, taxonomy.md, progression.txt, journal.json,
                              mobile-inbox, inbox, *.enabled, recovered_tiktok_links_*.txt,
                              logs/llm_usage.db, jarvis/notes (et memoire/contenus/... si présents)
    secrets/secrets.lisenc    UNIQUEMENT avec -IncludeSecrets
```

Garanties :
- `vault.db` est copié par `sqlite3.Connection.backup` (instantané transactionnel cohérent
  même si la passerelle écrit), puis `PRAGMA integrity_check` sur la copie ;
- le dossier est écrit en `.partial`, le manifest est vérifié, puis il est renommé : un
  backup incomplet ne peut pas passer pour valide ;
- chaque exécution crée un **nouveau** dossier ; un backup existant n'est jamais modifié ;
- non sauvegardé (reconstructible) : `.venv`, modèles Ollama, `models/` (e5),
  `external/` (upstreams), caches, logs (sauf `llm_usage.db`), `vault.html`.

### Secrets (`-IncludeSecrets`)

Contenu : `.env`, `config.yaml` / `google_credentials.json` / `google_token.json` de Jarvis,
identité Syncthing du PC (`cert.pem`, `key.pem`) et valeurs des variables utilisateur Windows
du LIS (`NOTION_*`, `CLICKUP_*`, `GOOGLE_*`, `GEMINI_*`, `OPENROUTER_*`, `ANTHROPIC_*`,
`TELEGRAM_*`...).

Chiffrement : **AES-256-CBC + HMAC-SHA256 (encrypt-then-MAC)**, clé dérivée de la
passphrase par **PBKDF2-SHA256, 600 000 itérations**, sel et IV aléatoires. Implémenté
avec .NET uniquement (aucune dépendance) : déchiffrable sur tout Windows avec PowerShell 5.1.
Le clair n'est **jamais** écrit sur disque (assemblé et chiffré en mémoire) ; ni la
passphrase ni les valeurs ne sont journalisées. Passphrase : saisie masquée (2 fois), ou
variable d'environnement `LIS_BACKUP_PASSPHRASE` pour l'automatisation.

Limites (assumées) :
- la sécurité repose entièrement sur la passphrase (12 caractères minimum ; prends-en
  une longue). Passphrase perdue = secrets irrécupérables ;
- format propre au LIS (`lis-secrets v1`), pas un standard comme age/GPG ; il est
  documenté dans `lib/LisDeploy.psm1` (`Protect-LisBytes`) ;
- PBKDF2 (et non scrypt/Argon2, absents de .NET Framework) ;
- pendant le chiffrement, les secrets sont en mémoire du processus PowerShell.

Copie toujours le dossier de backup **hors du PC** (disque externe, cloud).

---

## RESTORE / DISASTER RECOVERY (cas B : PC neuf + GitHub + backup)

```powershell
winget install Git.Git
git clone https://github.com/Roseytech/reels-vault.git $HOME\reels-vault
cd $HOME\reels-vault
.\restore_lis.ps1 -BackupPath E:\LIS-BACKUPS\LIS-BACKUP-20261001-120000 -VerifyOnly
.\restore_lis.ps1 -BackupPath E:\LIS-BACKUPS\LIS-BACKUP-20261001-120000 -RestoreSecrets
```

Ordre exact de `restore_lis.ps1` :

1. lit `manifest.json`, vérifie format et version (`lis-backup` v1) ;
2. vérifie le SHA-256 et la taille de **chaque** fichier ; fichier modifié, manquant ou
   non déclaré → **BACKUP REFUSE**, rien n'est restauré ;
3. `PRAGMA integrity_check` + tables minimales sur le `vault.db` du backup ;
4. compare le lock du backup avec celui du code ; compare le SHA du code au SHA du backup
   (sinon : `git checkout <sha>` indiqué, jamais fait automatiquement) ;
   — `-VerifyOnly` s'arrête ici, sans aucune écriture ;
5. restaure `vault.db` (copie, `integrity_check`, puis renommage). Une base existante
   **bloque** la restauration, sauf si elle est identique au backup (relance
   idempotente) ; `-ReplaceVault` la **déplace** dans
   `backups\vault-before-restore-<date>\` (jamais supprimée) ;
6. restaure les fichiers personnels **sans jamais écraser** un fichier existant
   (fichiers identiques reconnus ; fichiers différents conservés et signalés) ;
7. avec `-RestoreSecrets` : déchiffre en mémoire et n'écrit que les cibles absentes
   (`.env` existant jamais écrasé ; variables Windows déjà définies conservées) ;
   sinon `.env` est recréé depuis `config/env.redacted` (réglages sans clés) ;
8. `setup_lis.ps1 -Install` : Python, `.venv`, paquets, FFmpeg, Ollama/Qwen, e5,
   upstreams **aux SHA exacts**, Jarvis + Voice Bridge, mobile-inbox, Syncthing, tâches ;
9. données et secrets Jarvis remis dans `external\jarvis-assistant-vocal` ;
10. Syncthing comparé à `syncthing-mapping.json` (ID du dossier à recréer si besoin) ;
11. `CHECK_LIS.ps1` ; **LIS RESTORED** seulement si les composants critiques
    (`vault.db`, `integrity_check`, schéma, `.venv`, imports critiques, retrieval lexical,
    Agency, Jarvis, lock, secrets non suivis) passent. Sinon **LIS NOT RESTORED**.

Options : `-SkipTasks` (pas de tâches Windows), `-SkipInstall`, `-SkipHealthCheck`
(affiche alors `DATA RESTORED`, jamais `LIS RESTORED`), `-Root <dossier>`, `-NoSystem`
(tests : ne touche ni aux variables utilisateur Windows ni à Syncthing), `-Python`.
Relancer `restore_lis.ps1` est sans danger : une base identique au backup est reconnue.

> Ne jamais tester une restauration par-dessus le LIS réel : utiliser
> `-Root C:\Temp\lis-test` (dossier temporaire).

Étapes humaines après restauration : appairage Syncthing si l'identité n'a pas été
restaurée, test MacroDroid, test vocal Jarvis, `telegram_setup.py` si `.env` n'a pas
été restauré, connecteurs ChatGPT.

---

## MIGRATION (ancien PC → nouveau PC)

1. Ancien PC : `.\CHECK_LIS.ps1` puis `.\backup_lis.ps1 -IncludeSecrets -Destination E:\LIS-BACKUPS`.
2. Arrêter l'ancien PC côté LIS (désactiver les tâches « Reels Vault* ») pour éviter deux
   passerelles Telegram sur le même bot.
3. Nouveau PC : section **RESTORE** ci-dessus avec `-RestoreSecrets`.
4. Nouveau PC : `.\CHECK_LIS.ps1` → `LIS READY` ou `READY WITH WARNINGS`.

---

## UPSTREAM LOCK

`setup_upstreams.ps1` ne suit **plus** `origin/main` et ne fait plus de `reset --hard` :

| Situation | Comportement |
|---|---|
| upstream absent | `git init` + `fetch` du SHA exact + `checkout --detach` |
| HEAD = SHA du lock | rien (idempotent) ; modifications locales hors bridge → WARNING |
| HEAD ≠ SHA du lock | checkout du SHA ; les fichiers modifiés par le bridge sont sauvegardés en patch puis réappliqués |
| modifications locales non gérées | **BLOCK** ; `-Force` les sauvegarde dans `backups\upstream-patches\` puis continue |
| dossier non Git / remote inattendu | **BLOCK** (jamais supprimé ; `-ForceRefresh` est obsolète et ignoré) |
| fichiers non suivis (`config.yaml`, jetons...) | jamais touchés (aucun `git clean`) |

Puis `install_jarvis_voice_bridge.py` réapplique l'intégration (modules
`integrations/jarvis/*.py` → `tools/`, routage vocal, résumé Mentor du brief) ; il n'écrit
que si un contenu change. `python install_jarvis_voice_bridge.py --check` = lecture seule.

**Mettre à jour un upstream** (volontaire, jamais automatique) :

1. cloner le nouveau commit à part, `python vault_security_gate.py <dossier>` et revue ;
2. modifier le `sha` dans `deployment-kit/lis.lock.json` ;
3. `.\setup_upstreams.ps1 -DryRun` puis `.\setup_upstreams.ps1` ;
4. tests + `.\CHECK_LIS.ps1`, commit du lock sur une branche, PR.

Changer une dépendance Python : mettre à jour `requirements-lock.txt`
(`.\.venv\Scripts\python.exe -m pip freeze > deployment-kit\requirements-lock.txt`), puis
`.\CHECK_LIS.ps1` (ligne `requirements (lock)`).

---

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_deploy_*.py" -v
```

Bases SQLite factices, faux LIS dans des dossiers temporaires, upstream Git local,
`-NoSystem` (ni variables Windows, ni Syncthing, ni tâches) : le vrai `vault.db` n'est
jamais ouvert. Le workflow `.github/workflows/lis-deployment-check.yml` rejoue ces tests
sur `windows-latest`.

### Tests réels (hors CI)

| Test | Nécessite |
|---|---|
| installation complète `setup_lis.ps1 -Install` | un autre PC Windows (ou VM) vierge |
| restauration complète `restore_lis.ps1 -RestoreSecrets` | idem + un backup réel |
| partage TikTok → `inbox.txt` → `vault.db` | le téléphone Android (MacroDroid + Syncthing) |
| message, vocal, suggestions | Telegram réel |
| projection cockpit / Library | Notion réel |
| consultation via connecteurs | ChatGPT |
| « vault ... » à voix haute → réponse du LIS | Jarvis vocal réel (micro) |
