# Prompt maître — Restaurer un LIS existant

Tu es le technicien de restauration du Life Intelligence System sur un nouveau PC Windows.

Je possède un LIS existant et un dossier de backup `LIS-BACKUP-YYYYMMDD-HHMMSS` créé par
`backup_lis.ps1`. Ton objectif est de reconstruire le logiciel et restaurer mon état
personnel sans créer de Vault concurrent.

Lis d'abord :
- deployment-kit/README.md (sections RESTORE / DISASTER RECOVERY et MIGRATION)
- deployment-kit/10_BACKUP_RESTORE.md
- deployment-kit/11_MIGRATE_NEW_PC.md
- deployment-kit/09_HEALTH_CHECK.md
- FINAL_ARCHITECTURE.md

Règles :
- ne crée pas de nouveau Vault si une sauvegarde existe ;
- ne lance aucune migration/ingestion avant inspection de la sauvegarde ;
- ne modifie pas main ;
- ne force-push pas ;
- ne montre aucun secret ; la passphrase du backup est saisie par moi (saisie masquée) ;
- reconstruis .venv au lieu de le copier ;
- n'utilise jamais `-ReplaceVault` sans mon accord explicite ;
- si `.ps1` est bloqué : `powershell -ExecutionPolicy Bypass -File <script>`.

Étapes :
1. audite la machine et le repo (`.\setup_lis.ps1 -Audit`) ;
2. vérifie le backup sans rien écrire :
   `.\restore_lis.ps1 -BackupPath <backup> -VerifyOnly`
   (format, SHA-256 de chaque fichier, `integrity_check`, lock, SHA du code) ;
3. si un état local existe déjà (vault.db, .env), arrête-toi et rapporte-le ;
4. restaure : `.\restore_lis.ps1 -BackupPath <backup> -RestoreSecrets`
   (vault.db, fichiers personnels, secrets, puis `setup_lis.ps1 -Install` : Python, .venv,
   dépendances, Ollama/Qwen, embeddings, upstreams aux SHA exacts, Jarvis + bridge,
   mobile-inbox, Syncthing, tâches Windows ; puis `CHECK_LIS.ps1`) ;
5. reconnecte Telegram/Notion/providers si nécessaire (`telegram_setup.py`, `llm_setup.py`) ;
6. guide la réassociation Syncthing/MacroDroid (deployment-kit/android/) ;
7. fais les tests manuels de deployment-kit/09_HEALTH_CHECK.md.

Rapporte SHA main, SHA upstreams, backup utilisé, contrôles d'intégrité, composants restaurés, composants volontairement non configurés et écarts.

Verdict uniquement : `LIS RESTORED` (tel qu'affiché par `restore_lis.ps1`) ou `LIS NOT RESTORED` + bloquants.

Aucun développement supplémentaire.
