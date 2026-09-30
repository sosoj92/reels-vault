# Notion — où retrouver une information

Notion est la vitrine pour consulter et piloter ; `vault.db` reste maître. Modifier une page
Notion ne modifie pas le Vault (les retours sont contrôlés : décisions du cockpit uniquement).

## COMMAND CENTER
- **📚 Explorer ma mémoire** : Vault Library, derniers contenus, vidéos, thèmes, outils, projets,
  à exploiter.
- **💼 Mes activités & projets** : Nexdoor Talent, Nexdoor Partners, Recherche emploi remote,
  Side businesses, verticales, clients / opportunités.
- Cockpit (suggestions du Mentor, décisions), Tâches, Mentor, Système : inchangés.

## 📚 KNOWLEDGE & VEILLE
Porte d'entrée : Vault Library et ses vues, Veille marché, Mentor Insights (non dupliqué).

## Life Intelligence — Vault Library (`vault_notion_library.py`)
Une page par source du Vault (`Vault ref = source:<id>`, `Vault managed`), jamais en double.
Propriétés : titre, type, URL originale, plateforme, auteur, date de capture, description,
résumé, aperçu du transcript, thème, sous-thèmes, tags, outils (normalisés : Make = make.com),
concepts, projets, entités, signal et pertinence du Mentor, à exploiter, sensibilité, canal
d'ingestion, Source ID, dernière synchro. Aucune valeur inventée : un champ absent reste vide.

Page d'une source : résumé en haut, « pourquoi c'est utile », informations, lien original,
description, **transcript complet repliable**. Contenus sensibles : métadonnées seulement.

Vues : 🆕 Derniers contenus · 🧠 Par thème · 🛠️ Par outil · 💼 Par projet · 🎬 Vidéos ·
⭐ À exploiter · 📚 Tout le Vault · 💼 <projet> (une vue filtrée par projet).
Exemple : retrouver tous les contenus Apify → vue 🛠️ Par outil, filtre Outils contient Apify.

## Fiches projet (pages existantes)
Section « 🧠 Fiche Life Intelligence » ajoutée en bas de la page (le reste n'est jamais touché) :
vue d'ensemble (profil), objectif / offre / cibles / marchés / produits (« À compléter » tant que
rien n'est enregistré), outils & stack, process / systèmes (sous-pages existantes), clients /
prospects, priorités, décisions, tâches actives, ressources du Vault (liens + vue filtrée),
**SOP** (sous-pages « Process / SOP / Procédure », sinon « À formaliser »), dossier Drive,
métadonnées, et le profil technique (règles composées…) dans un bloc repliable.

## Modifier un projet
Le profil machine est dans `vault.db` (`mentor_project_profiles`) :
`python mentor_project_profiles.py --export profils.json` → modifier → `--import profils.json`.
La fiche Notion est régénérée au passage quotidien.

## Synchronisation
Horaire : sources (incrémental, sans LLM). Quotidienne (PC au repos) : vues, fiches projet,
portails. Manuelle : `python vault_notion_library.py --confirm`.
