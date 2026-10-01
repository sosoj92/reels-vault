# Prompt maître — Restaurer un LIS existant

Tu es le technicien de restauration du Life Intelligence System sur un nouveau PC Windows.

Je possède un LIS existant. Ton objectif est de reconstruire le logiciel et restaurer mon état personnel sans créer de Vault concurrent.

Lis d'abord :
- deployment-kit/10_BACKUP_RESTORE.md
- deployment-kit/11_MIGRATE_NEW_PC.md
- deployment-kit/09_HEALTH_CHECK.md
- FINAL_ARCHITECTURE.md

Règles :
- ne crée pas de nouveau Vault si une sauvegarde existe ;
- ne lance aucune migration/ingestion avant inspection de la sauvegarde ;
- ne modifie pas main ;
- ne force-push pas ;
- ne montre aucun secret ;
- reconstruis .venv au lieu de le copier ;
- compare les versions/schema avant toute écriture ;
- arrête les writers pendant la restauration de vault.db.

Étapes :
1. audite la machine et le repo ;
2. vérifie intégrité et date du backup ;
3. sauvegarde tout état local actuel avant remplacement ;
4. recrée Python/.venv/dépendances ;
5. restaure upstreams et modèle sémantique ;
6. restaure vault.db de façon cohérente ;
7. restaure .env via canal local sécurisé ;
8. vérifie la DB en lecture avant tout writer ;
9. reconnecte Telegram/Notion/providers si nécessaire ;
10. réinstalle Jarvis/bridge ;
11. réassocie Syncthing/MacroDroid ;
12. recrée tâches Windows ;
13. exécute le Health Check complet.

Rapporte SHA main, SHA upstreams, backup utilisé, contrôles d'intégrité, composants restaurés, composants volontairement non configurés et écarts.

Verdict uniquement : `LIS RESTORED` ou `LIS NOT RESTORED` + bloquants.

Aucun développement supplémentaire.
