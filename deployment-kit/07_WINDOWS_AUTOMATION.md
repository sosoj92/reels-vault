# 07 — Automatisation Windows

Le LIS final utilise le Planificateur de tâches Windows plutôt qu'un service Windows custom.

## Installation

~~~powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows_tasks.ps1
~~~

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
Le repo conserve aussi les tâches d'ingestion/horaire existantes.

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

Ne considérer une automatisation validée qu'après exécution depuis Task Scheduler, car le contexte diffère d'un terminal interactif.
