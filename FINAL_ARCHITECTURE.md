# Life Intelligence — architecture finale

```
ROSY
 ↓
Telegram (PC + mobile)                    telegram_gateway.py
 ↓
Life Gateway
 ├── Réactif  : texte · vocal · URL · réponse · commande
 └── Proactif : 💡 suggestions · digest · pause · snooze
 ↓
Retrieval                                  vault_ask.py (lexical + sémantique local)
 ↓
Mentor                                     mentor_proactive.py → mentor_council.py → mentor_delivery.py
 ↓
Agency Agents (spécialistes, modes A/B/C)  vault_agent_registry.py + vault_orchestrator.py
 ↓
Qwen (Ollama, local)                       qwen3:1.7b, num_ctx 4096
 ↓
vault.db  ← source de vérité
 ├── Notion   : cockpit existant (Suggestion / Décision) + Tâches existantes
 ├── Tasks    : table actions (créées seulement après ton clic)
 └── Telegram : messages, boutons, contexte de session
```

## Les rôles, simplement

| Élément | Rôle |
|---|---|
| **Vault** (`vault.db`) | La mémoire. Tout y est écrit en premier ; c'est l'arbitre final. |
| **Mentor** | Le directeur de cabinet : observe les événements, relie les sources aux projets, priorise, et décide **s'il doit parler** (souvent : non). |
| **Agents** (Agency Agents) | Les spécialistes : Recruitment Specialist, Growth Hacker, CFO, Workflow Architect… Ils évaluent, ils n'agissent pas. |
| **Qwen** | Le moteur de raisonnement local. Aucun cloud, aucun repli automatique vers le cloud. |
| **Telegram** | La bouche et les oreilles : l'interface, pas le cerveau. |
| **Notion** | Le cockpit : voir, décider (Approuver / Garder comme insight / Reporter / Ignorer), suivre les tâches. |
| **Drive** | Les fichiers (dossiers de projets reliés aux entités). |
| **Calendar** | Le temps (planification existante des tâches). |

## Les deux canaux

**Réactif** (tu parles) — texte, vocal (transcrit localement par faster-whisper), URL (ajoutée à l'inbox),
réponse à un message (le contexte est retrouvé), commande. Le Vault est interrogé, Qwen synthétise,
les agents sont consultés si utile :
- mode **A** : « Demande au CFO… » — l'agent nommé ;
- mode **B** : « Comment exploiter ça pour Nexdoor Talent ? » — 1 à 3 agents choisis et justifiés ;
  « ajoute le CFO », « sans le Growth Hacker », « demande plutôt au Recruitment Agent ».

**Proactif** (le Mentor parle, rarement) :

```
EVENT → détection déterministe → candidat → score → shortlist
      → Qwen (hypothèse) → Agents mode C (verdicts 0–5) → filtre final → SUG-xxxx
      → 1 push/jour max · le reste en digest · silence sinon
      → ta décision (Telegram ou Notion) → vault.db → Notion / tâche
```

Une suggestion n'existe que si : projet actif et profil complet, preuve suffisante (shortlist),
nouveau (empreinte), actionnable, au moins un agent ≥ 4/5, aucune objection bloquante, fatigue faible.
Les désaccords sérieux entre agents sont affichés.

## Garde-fous

- Aucune action externe sans ton clic (tâche, Notion Tâches) ; aucune suppression.
- Candidats et rejets : uniquement dans `vault.db`, jamais projetés.
- Telegram : whitelist utilisateur **et** chat, aucune commande shell, aucun accès fichier
  (sauf ajout d'URL à l'inbox), token uniquement dans `.env` (ignoré par Git), jamais journalisé.
- Données santé personnelles : jamais rattachées à un projet.
- Pause (« mets le Mentor en pause jusqu'à lundi ») et mode focus : il observe et journalise, n'envoie rien.

## Ce qui tourne, et quand

| Composant | Déclenchement |
|---|---|
| Ingestion + refresh (publie les événements) | Tâches existantes « Reels Vault Ingest » / « Hourly » : toutes les heures |
| Passerelle Telegram | « Reels Vault - Telegram Gateway » : à l'ouverture de session, en permanence, relancée si elle tombe |
| Passage quotidien du Mentor (Qwen + Agents) | « Reels Vault - Mentor Daily » : 07:30 et 20 min après l'ouverture de session, **PC au repos uniquement**, rattrapage si le PC était éteint |
| Livraison des suggestions, synchro des décisions Notion | Dans la passerelle : toutes les minutes (Notion toutes les 10 min) |
| Réponses aux questions | À chaque message |

Détails techniques : `docs/MENTOR_PROACTIVE_V3.md`.
