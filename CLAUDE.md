# Reels Vault

Ce dossier est une base personnelle de vidéos TikTok et Instagram sauvegardées.
`vault.db` (SQLite) est la source de vérité opérationnelle ; `index.md` reste
maintenu pour compatibilité pendant la transition.

## Consulter le Vault
- Pour toute question sur le contenu du Vault, commence toujours par lire `index.md`
  (ou interroge l'API locale `http://127.0.0.1:8765` si elle est lancée).
- N'ouvre les fiches complètes de `raw/` que si `index.md` ne suffit pas.

## Classifier une fiche
- Consulte `taxonomy.md` avant de classifier une nouvelle fiche.
- Réutilise en priorité les catégories et sous-thèmes existants.
- Chaque contenu doit avoir :
  - une seule catégorie principale
  - 1 à 3 sous-thèmes
  - des tags libres et précis
- Les tags transversaux comme « bons plans » ne doivent jamais devenir des catégories.
- N'invente aucune information absente des fiches `raw/`.
- Si un nom propre semble mal transcrit, signale l'incertitude.

## Typer les ressources
- Dans `outils_ressources`, chaque ressource porte son type entre crochets, juste après son nom :
  `Nom [type] (précision éventuelle)`.
  Exemple : `OpenAlternative [site], Notion [logiciel], « R-Table » [autre] (transcription incertaine)`.
- Types possibles : outil, logiciel, application, site, personne, livre, formation, service, méthode, lieu, autre.
- N'invente rien : si le type n'est pas clair d'après la fiche, utilise `[autre]`.

## Après toute nouvelle indexation
1. mettre à jour `index.md`
2. mettre à jour `taxonomy.md` si nécessaire
3. mettre à jour `progression.txt`
4. exécuter `python migrate_to_db.py` (importe les nouvelles fiches dans `vault.db`, sans doublon)
5. exécuter `python generate_vault.py --from-db` (régénère `vault.html` depuis `vault.db`)

## Règles de modification
- Ne réécris pas entièrement `index.md` si seules de nouvelles fiches doivent être ajoutées.
- Ne supprime jamais une entrée existante sans instruction explicite.
- Les corrections faites par l'utilisateur dans `vault.db` (description manuelle, titre,
  classification, notes, actions) sont prioritaires : ne les écrase jamais.
- Une action proposée par Claude ou Davy est créée au statut `proposed` ; aucune tâche
  externe (ClickUp, Notion, Trello, Google Tasks, Todoist…) n'est créée automatiquement.

## Langue
- Réponds en français.
