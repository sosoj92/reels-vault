# 10 — Backup et restauration

## Reconstructible
- code GitHub ;
- `.venv` ;
- packages Python ;
- Ollama/modèle ;
- upstreams ;
- Agency Agents ;
- tâches Windows ;
- scripts ;
- structure Notion gérée par le LIS.

## À sauvegarder
- `vault.db` ;
- `.env` de façon chiffrée/sécurisée ;
- fichiers personnels non présents ailleurs ;
- export/config MacroDroid ;
- mapping/config Syncthing ;
- rapport d'installation ;
- SHA de `main` ;
- SHA des upstreams.

## vault.db
Éviter une copie pendant des écritures. Avant une copie brute, arrêter les writers ou utiliser un mécanisme SQLite de backup cohérent.

Ne jamais synchroniser `vault.db` en écriture simultanée entre plusieurs machines.

## .env
Ne jamais le committer, l'envoyer dans un prompt ou le déposer en clair dans un stockage partagé.

## Test de restauration
1. restaurer dans un environnement de test ;
2. ouvrir le Vault ;
3. vérifier sources/projets ;
4. lancer une requête ;
5. vérifier qu'aucun fichier requis n'est absent.

Voir `11_MIGRATE_NEW_PC.md`.
