# 11 — Migrer vers un nouveau PC

Objectif : reconstruire le logiciel depuis GitHub et ne restaurer que l'état personnel.

## Avant l'ancien PC
Conserver : backup cohérent de `vault.db`, `.env` sécurisé, exports/configs mobiles, providers utilisés, SHA main/upstreams, rapport Health Check et fichiers personnels non reconstruisibles.

## Nouveau PC
1. Installer Git.
2. Installer Claude Code.
3. Cloner le repo.
4. Se placer sur le `main` voulu.
5. Utiliser `prompts/02_RESTORE_EXISTING_LIS.md`.
6. Recréer `.venv`.
7. Installer dépendances.
8. Installer embeddings.
9. Restaurer upstreams.
10. Installer Ollama/Qwen.
11. Restaurer `vault.db`.
12. Restaurer `.env` sans l'afficher.
13. Reconnecter Notion/Telegram/providers si nécessaire.
14. Réinstaller Jarvis/bridge.
15. Réassocier Syncthing.
16. Vérifier MacroDroid.
17. Recréer tâches Windows.
18. Lancer Health Check.

## Interdictions
- ne pas créer un Vault neuf par-dessus l'ancien ;
- ne pas lancer ingestion/migrations destructrices avant inspection ;
- ne pas modifier `main` pour des chemins personnels ;
- ne pas copier `.venv` : le reconstruire.

Verdict : `LIS RESTORED` uniquement si le Health Check passe et si un échantillon réel des données est retrouvable.
