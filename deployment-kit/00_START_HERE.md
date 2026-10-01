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
git clone <URL_DU_REPO_LIS>
cd <DOSSIER_DU_LIS>
git status
git branch --show-current
~~~

Pour une installation normale, la référence doit être `main` et le dépôt propre.

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
- Ancien pipeline iPhone/iCloud du README historique.

L'installation n'est terminée que lorsque `09_HEALTH_CHECK.md` passe réellement.
