# 07 — Automatisation Windows

Le LIS final utilise le Planificateur de tâches Windows plutôt qu'un service Windows custom.

## Installation

~~~powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows_tasks.ps1
.\setup_windows_tasks.ps1 -Only Ingestion,MentorDaily    # sous-ensemble
.\setup_windows_tasks.ps1 -SkipTelegramGateway           # Telegram pas encore configure
~~~

`setup_lis.ps1 -Install` (étape `tasks`) ne crée que les tâches manquantes, et la Gateway
seulement si Telegram est configuré.

## Tâches importantes

### Reels Vault - Telegram Gateway
- à l'ouverture de session ;
- permanente ;
- redémarrage automatique si erreur ;
- Python de `.venv`.

### Reels Vault - Mentor Daily
- quotidien à 07:30 ;
- +20 min après ouverture de session ;
- PC au repos ;
- StartWhenAvailable pour rattrapage.

### Ingestion périodique
« Reels Vault - Hourly » : `inbox.bat` toutes les heures, StartWhenAvailable, réveil du PC.
`setup_windows_tasks.ps1` la crée **uniquement si aucune tâche ne lance déjà `inbox.bat`** ;
les tâches existantes ne sont jamais modifiées. Sur le PC de référence, deux tâches lancent
`inbox.bat` (« Reels Vault - Hourly » et l'ancienne « Reels Vault Ingest ») :
`CHECK_LIS.ps1` signale ce doublon en WARNING ; désactiver l'une des deux est une décision
manuelle.

## Scripts

`inbox.bat` :
- force le cwd sur le repo ;
- ingère `mobile-inbox/inbox.txt` ;
- refresh ;
- ingestion locale/reconcile ;
- projection Notion sources ;
- Mentor worker ;
- sync Notion.

`mentor_daily.bat` :
- Mentor proactif ;
- Agency ;
- classement en attente ;
- refresh Notion.

## Vérification

~~~powershell
Get-ScheduledTask | Where-Object { $_.TaskName -like "Reels Vault*" } | Select-Object TaskName, State
~~~

`.\CHECK_LIS.ps1` (section WINDOWS TASKS) vérifie présence, déclencheurs, StartWhenAvailable,
redémarrage de la Gateway et utilisation de la `.venv` du repo.

Ne considérer une automatisation validée qu'après exécution depuis Task Scheduler, car le contexte diffère d'un terminal interactif.

Les tâches exécutent la branche actuellement extraite du repo : ne jamais laisser une
branche de travail non validée extraite sur le PC de production.
