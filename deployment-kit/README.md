# Life Intelligence System — Deployment Kit

Ce dossier documente la reproduction, l'installation, l'utilisation, la migration et la restauration du Life Intelligence System (LIS).

Il décrit la recette finale qui a survécu, pas l'historique de tous les essais.

## Deux parcours
- **Nouvelle installation** : PC Windows neuf → Vault neuf → LIS READY.
- **Restauration / migration** : nouveau PC → code GitHub + sauvegarde personnelle → LIS RESTORED.

## Architecture de référence
- GitHub contient le code.
- `vault.db` est la mémoire canonique.
- Notion est le cockpit/projection.
- Telegram est l'interface rapide/mobile.
- ChatGPT est une interface d'analyse riche via connecteurs, pas la mémoire.
- Jarvis est l'interface vocale.
- Claude Code est l'outil principal de construction, installation, maintenance et diagnostic.
- LLM Router garde les modèles interchangeables.
- Agency Agents sont des playbooks de raisonnement, pas des modèles.
- MacroDroid + Syncthing forment le pipeline mobile Android.
- Tailscale ne fait pas partie de la stack finale.
- Mistral n'est pas un fournisseur runtime de référence.

## Ordre de lecture
1. 00_START_HERE.md
2. 13_STACK_MATRIX.md
3. 01_INSTALL_WINDOWS.md
4. 02_LLM_AND_MODELS.md
5. 03_TELEGRAM.md
6. 04_NOTION_AND_CHATGPT.md
7. 05_JARVIS.md
8. 06_ANDROID_MACRODROID_SYNCTHING.md
9. 07_WINDOWS_AUTOMATION.md
10. 08_SECURITY.md
11. 09_HEALTH_CHECK.md
12. 10_BACKUP_RESTORE.md
13. 11_MIGRATE_NEW_PC.md
14. 12_TROUBLESHOOTING.md
15. 14_COMMAND_REFERENCE.md

Le dossier `prompts/` contient les prompts maîtres pour Claude Code.

## Sources de vérité techniques
Prioriser FINAL_ARCHITECTURE.md, docs/INGESTION.md, docs/LLM_ROUTER.md, docs/NOTION_VAULT_LIBRARY.md, docs/MENTOR_PROACTIVE_V3.md, docs/AGENCY_SECURITY_SELF_IMPROVEMENT.md, UPSTREAMS.md, .env.example et les scripts setup_* actuels.

README.md contient encore de l'historique de l'ancien Reels Vault (Cowork/iPhone/iCloud). En cas de contradiction, les documents ci-dessus priment.
