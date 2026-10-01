# Prompt maître — Mettre à jour le LIS sans casser main

Tu dois mettre à jour un LIS stable.

Règles :
- main est la référence stable ;
- comprendre git status avant toute action ;
- backup des données avant changement structurel ;
- branche dédiée ;
- jamais de force push par défaut ;
- upstream externe = Security Gate + revue + nouveau SHA écrit volontairement dans `deployment-kit/lis.lock.json`, puis `.\setup_upstreams.ps1 -DryRun` avant application ;
- dépendance Python modifiée = `deployment-kit/requirements-lock.txt` régénéré ;
- `.\backup_lis.ps1` avant tout changement structurel, `.\CHECK_LIS.ps1` avant et après ;
- pas de refactor gratuit ;
- une seule implémentation par besoin ;
- tests avant merge ;
- test réel pour Telegram/Jarvis/provider/mobile si touché.

Process :
1. synchronise les refs sans modifier le working tree ;
2. rapporte main local vs origin/main ;
3. crée branche depuis origin/main ;
4. implémente le changement minimal ;
5. cherche les doublons avant d'ajouter un mécanisme ;
6. tests ciblés ;
7. suite complète pertinente ;
8. scan secrets/diff ;
9. rapport READY TO MERGE / NOT READY TO MERGE ;
10. attendre autorisation ;
11. après merge : push main, redémarrer runtime touché, smoke test ;
12. arrêter.

En cas de divergence :
- sauvegarder le HEAD local dans une branche backup ;
- prendre le remote validé comme socle ;
- comparer sémantiquement ;
- reporter uniquement les apports uniques ;
- ne jamais force-écraser une branche distante sans décision explicite.
