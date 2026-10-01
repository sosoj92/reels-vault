# 10 — Backup et restauration

Scripts : `backup_lis.ps1` et `restore_lis.ps1`. Référence détaillée (structure du backup,
chiffrement, ordre de restauration, options) : `README.md`, sections BACKUP et RESTORE.

## Reconstructible (non sauvegardé)
- code GitHub ;
- `.venv` et packages Python (`requirements-lock.txt`) ;
- Ollama / modèle Qwen ;
- modèle e5 (`setup_semantic_model.ps1`) ;
- upstreams et Agency Agents (SHA de `lis.lock.json`) ;
- tâches Windows (`setup_windows_tasks.ps1`) ;
- scripts ;
- structure Notion gérée par le LIS.

## Sauvegardé par `backup_lis.ps1`
- `vault.db` (API de sauvegarde SQLite + `integrity_check`) ;
- `lis.lock.json`, SHA du LIS et des upstreams, versions (`manifest.json`) ;
- `.env` **sans valeurs sensibles** (`config/env.redacted`) ; noms des variables Windows ;
- export des tâches Windows (référence) ;
- export MacroDroid s'il existe dans `android/macrodroid/` (sinon `EXPORT_REQUIRED.txt`) ;
- mapping Syncthing (dossiers, appareils, sans clé) ;
- fichiers personnels non reconstructibles : `raw/`, `images/`, `index.md`, `taxonomy.md`,
  `progression.txt`, `journal.json`, `mobile-inbox/`, `inbox/`, verrous `*.enabled`,
  `logs/llm_usage.db`, données Jarvis non secrètes ;
- rapport d'installation et dernier Health Check ;
- **secrets uniquement avec `-IncludeSecrets`, chiffrés** : `.env`, `config.yaml` et jetons
  Google de Jarvis, identité Syncthing, variables utilisateur (Notion, ClickUp, Google...).

~~~powershell
.\backup_lis.ps1 -DryRun
.\backup_lis.ps1 -Destination E:\LIS-BACKUPS
.\backup_lis.ps1 -IncludeSecrets -Destination E:\LIS-BACKUPS
~~~

Chaque fichier est listé avec son SHA-256 et sa taille dans `manifest.json`, vérifié avant
que le dossier `.partial` soit renommé en `LIS-BACKUP-YYYYMMDD-HHMMSS`.

## vault.db
Jamais de copie brute pendant des écritures : `backup_lis.ps1` utilise
`sqlite3.Connection.backup` (instantané cohérent, source ouverte en `query_only`).

Ne jamais synchroniser `vault.db` en écriture simultanée entre plusieurs machines.

## .env
Ne jamais le committer, l'envoyer dans un prompt ou le déposer en clair dans un stockage
partagé. Seule forme sauvegardée en clair : `env.redacted` (clés sans valeurs sensibles).

## Restauration

~~~powershell
.\restore_lis.ps1 -BackupPath E:\LIS-BACKUPS\LIS-BACKUP-... -VerifyOnly
.\restore_lis.ps1 -BackupPath E:\LIS-BACKUPS\LIS-BACKUP-... -RestoreSecrets
~~~

Garanties : backup altéré (SHA-256, fichier manquant ou ajouté, version de format inconnue)
→ refusé sans rien écrire ; `vault.db` existant jamais écrasé (`-ReplaceVault` le déplace
dans `backups\`) ; `.env` existant jamais écrasé ; verdict `LIS RESTORED` seulement si les
composants critiques de `CHECK_LIS.ps1` passent.

## Test de restauration
1. restaurer dans un environnement de test :
   `.\restore_lis.ps1 -BackupPath ... -Root C:\Temp\lis-test -SkipInstall -SkipHealthCheck -NoSystem` ;
2. ouvrir le Vault restauré (intégrité, nombre de contenus) ;
3. vérifier sources/projets ;
4. lancer une requête ;
5. vérifier qu'aucun fichier requis n'est absent ;
6. supprimer le dossier de test.

Ne jamais tester une restauration par-dessus le LIS réel. Voir `11_MIGRATE_NEW_PC.md`.
