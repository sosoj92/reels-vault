# 00 — START HERE

Objectif : partir d'un PC Windows et arriver à un LIS opérationnel sans connaissance préalable du projet.

## Prérequis
- PC Windows.
- Connexion Internet pour l'installation.
- Compte GitHub.
- Accès à Claude Code.
- Compte Telegram.
- Compte Notion.
- Téléphone Android pour le pipeline mobile complet.
- Comptes/API IA uniquement pour les fournisseurs choisis.

## Étape 0 — préparer l'atelier

~~~powershell
winget install --id Git.Git -e --source winget
git --version
~~~

Installer Claude Code selon la méthode officielle Anthropic disponible au moment de l'installation, puis :

~~~powershell
claude --version
~~~

## Récupérer le dépôt

~~~powershell
cd $HOME
git clone https://github.com/Roseytech/reels-vault.git
cd reels-vault
git status
git branch --show-current
~~~

Pour une installation normale, la référence doit être `main` et le dépôt propre.

Autoriser les scripts locaux (Windows neuf : les `.ps1` sont bloqués par défaut) :

~~~powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
~~~

(ou préfixer chaque script par `powershell -ExecutionPolicy Bypass -File`).

## Chemin automatisé

~~~powershell
.\setup_lis.ps1 -Audit       # état de chaque composant, aucune modification
.\setup_lis.ps1 -DryRun      # plan détaillé, aucune modification
.\setup_lis.ps1 -Install     # A : installe ce qui manque puis lance CHECK_LIS.ps1
.\restore_lis.ps1 -BackupPath <dossier LIS-BACKUP-...> -VerifyOnly   # B : vérifier le backup
.\restore_lis.ps1 -BackupPath <dossier LIS-BACKUP-...> -RestoreSecrets
.\CHECK_LIS.ps1              # Health Check, lecture seule
~~~

Détails de chaque script : `README.md` de ce dossier (section Outillage).

## Lancer Claude Code

~~~powershell
claude
~~~

Choisir :
- **A — Nouvelle installation** : Vault vide.
- **B — Restauration** : ancien `vault.db` + configuration personnelle.

A → `prompts/01_INSTALL_WINDOWS.md`  
B → `prompts/02_RESTORE_EXISTING_LIS.md`

## Expérience complète
Construction : Git, GitHub, Claude Code, PowerShell.  
Cœur : Python, .venv, SQLite/vault.db, ingestion, retrieval, Mentor, Agency, Security Gate.  
IA : Ollama/Qwen + fournisseurs cloud choisis.  
Interfaces : Telegram, Notion, ChatGPT, Jarvis.  
Mobile : MacroDroid + Syncthing.  
Automatisation : Windows Task Scheduler.

## Hors stack
- Tailscale.
- Mistral comme runtime par défaut.
- Ancien pipeline iPhone/iCloud du tutoriel historique (`docs/legacy/REELS_VAULT_TUTORIAL.md`).

L'installation n'est terminée que lorsque `.\CHECK_LIS.ps1` renvoie `LIS READY` (ou
`READY WITH WARNINGS` expliqués) et que les tests manuels de `09_HEALTH_CHECK.md`
passent réellement.
