# 04 — Notion et ChatGPT

## Notion
Notion est un cockpit/projection. `vault.db` reste maître.

Surfaces attendues : COMMAND CENTER, Vault Library, activités/projets, Mentor Insights, tâches, fiches projet, décisions, vues par thème/outil/projet.

Synchronisation manuelle :

~~~powershell
.\.venv\Scripts\python.exe vault_notion_library.py --confirm
~~~

Les contenus sensibles ne doivent exposer que les métadonnées autorisées.

## ChatGPT
ChatGPT est une interface d'analyse, pas un datastore.

Dans l'instance de référence il peut :
- consulter Notion via connecteur ;
- consulter GitHub via connecteur ;
- analyser le cockpit et la documentation ;
- utiliser une consultation Agency exportée ;
- travailler stratégiquement sur les projets.

Aucune API OpenAI n'est nécessaire uniquement pour cet usage.

## Agency portable

~~~powershell
.\.venv\Scripts\python.exe vault_orchestrator.py --packet --markdown --vault --agent "Security Architect" --agent "AI Engineer" --agent "Workflow Optimizer" "ma question" > consultation.md
~~~

Rôles : Vault = mémoire ; Notion = cockpit ; ChatGPT = analyse riche ; Telegram = interface rapide ; Jarvis = vocal.
