# 11 — Migrer vers un nouveau PC

Objectif : reconstruire le logiciel depuis GitHub et ne restaurer que l'état personnel.

## Sur l'ancien PC

~~~powershell
.\CHECK_LIS.ps1
.\backup_lis.ps1 -IncludeSecrets -Destination E:\LIS-BACKUPS
~~~

Le backup contient : `vault.db` cohérent, secrets chiffrés, export MacroDroid (si déposé
dans `android/macrodroid/`), mapping Syncthing, SHA main/upstreams, rapport d'installation
et Health Check, fichiers personnels non reconstructibles.

Désactiver ensuite les tâches « Reels Vault* » de l'ancien PC (une seule passerelle
Telegram par bot).

## Sur le nouveau PC
1. Installer Git (`winget install Git.Git`).
2. Installer Claude Code (pour le parcours guidé).
3. Cloner le repo, se placer sur le `main` voulu.
4. `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
5. Vérifier le backup : `.\restore_lis.ps1 -BackupPath <backup> -VerifyOnly`.
6. Restaurer : `.\restore_lis.ps1 -BackupPath <backup> -RestoreSecrets`. Le script :
   - restaure `vault.db` (jamais par-dessus une base existante) et les fichiers personnels ;
   - restaure `.env` et les secrets sans les afficher ;
   - recrée `.venv` et les dépendances, Ollama/Qwen, embeddings (via `setup_lis.ps1`) ;
   - restaure les upstreams aux SHA exacts et réinstalle Jarvis + bridge ;
   - remet les données Jarvis, compare Syncthing au mapping sauvegardé ;
   - recrée les tâches Windows ;
   - lance `CHECK_LIS.ps1`.
7. Reconnecter Notion/Telegram/providers si nécessaire (`llm_setup.py`, `telegram_setup.py`).
8. Réassocier Syncthing si l'identité du PC n'a pas été restaurée ; vérifier MacroDroid.
9. Test vocal Jarvis, partage Android réel, connecteurs ChatGPT.

Alternative guidée : `prompts/02_RESTORE_EXISTING_LIS.md` dans Claude Code.

## Interdictions
- ne pas créer un Vault neuf par-dessus l'ancien (`restore_lis.ps1` bloque) ;
- ne pas lancer ingestion/migrations destructrices avant inspection ;
- ne pas modifier `main` pour des chemins personnels ;
- ne pas copier `.venv` : le reconstruire.

Verdict : `LIS RESTORED` uniquement si le Health Check passe et si un échantillon réel des
données est retrouvable.
