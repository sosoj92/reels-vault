# Où envoyer un contenu — `vault_ingestion.py`

| Tu as… | Envoie-le… |
|---|---|
| un TikTok, un Reel, un article, un lien | **Telegram** (colle le lien) |
| une idée, une note | **Telegram** : « garde ça : … », « note : … », « idée : … » (ou un vocal qui commence ainsi) |
| une note pour un projet | « Garde ça pour Nexdoor Talent : … » → rattachée directement au projet |
| un PDF, un document, une photo, une capture | **Telegram** (en pièce jointe, légende facultative) |
| un gros fichier ou un lot | `C:\Users\rosyb\reels-vault\inbox\files\` |
| un document personnel / sensible (santé, finances) | `C:\Users\rosyb\reels-vault\inbox\sensitive\` (jamais de cloud, rien de détaillé dans Notion) |
| un mot de passe, une clé | **nulle part** : jamais Telegram, jamais un LLM |

Le pipeline mobile existant (MacroDroid / Syncthing → `mobile-inbox\inbox.txt`) reste inchangé.
Notion n'est qu'une entrée secondaire, jamais la source maître.

## Ce qui se passe
détection du type → entrée `ingestion_items` (canal, URL/fichier d'origine, message Telegram,
date, sensibilité, type) → enregistrement dans `vault.db` → classement (catégories et sous-thèmes
**existants**, tags, résumé, outils nommés dans le texte) → rattachement projet (explicite, ou
proposé à valider) → événements du Mentor → Vault Library (Notion) au passage suivant.

- Vidéo TikTok / Instagram : pipeline vidéo existant (transcription locale), au passage horaire.
- Article : titre, description, auteur, texte récupérés immédiatement (hors ligne : le lien est gardé).
- LLM indisponible : le contenu est enregistré, le classement attend et reprend automatiquement.
- Fichiers traités de l'inbox locale : déplacés dans `inbox\processed\<date>\`, jamais supprimés.

## Commandes Telegram
`/inbox` derniers ajouts · `/save` (en réponse à un message) · `/project Nexdoor Talent`
(en réponse à une confirmation d'ajout).

Confirmation courte : `✅ Ajouté au Vault` · type · `🧠` thèmes · `🛠️` outils · `💼` projet.
