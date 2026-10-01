# Life Intelligence — architecture finale

```
ROSY
 ↓
Telegram (PC + mobile) · Inbox locale · Sync mobile
 ↓
Life Gateway                      telegram_gateway.py + vault_ingestion.py
 ├── Réactif  : questions, vocaux, agents (modes A/B), contexte « ça »
 └── Proactif : 💡 suggestions, digest, pause, snooze
 ↓
Vault / Retrieval                 vault.db · vault_ask.py (projets, recherche lexicale + sémantique)
 ↓
Mentor Engine                     mentor_proactive.py → mentor_council.py → mentor_delivery.py
 ↓
Agency Agents                     vault_agent_registry.py + vault_orchestrator.py (modes A/B/C)
 ↓
LLM Router                        llm_router.py
 ├── Qwen / Ollama local (léger ; seul moteur pour le sensible et hors ligne)
 ├── Gemini API (standard et complexe)
 ├── OpenRouter (réserve, budget et plancher de crédit)
 └── file d'attente (repli ultime : l'analyse reprend plus tard)
 ↓
vault.db = source de vérité
 ├── Notion   : cockpit, Vault Library, fiches projet, décisions, tâches
 ├── Tasks    : table actions (créées seulement après ton clic)
 ├── Drive    : fichiers (dossiers liés aux projets)
 └── Calendar : temps et échéances
```

## Les rôles, simplement

| Élément | Rôle |
|---|---|
| **Vault** | La mémoire : tout y est écrit en premier (sources, relations, historique). |
| **Mentor** | Le directeur de cabinet : observe, relie, priorise, et décide s'il faut parler (souvent : non). |
| **Agents** | Les spécialistes (Recruitment Specialist, Growth Hacker, CFO…) : des playbooks de raisonnement, applicables par n'importe quel moteur ; ils évaluent, ils n'agissent pas. |
| **ChatGPT** | Interface de consultation riche possible (paquet portable + Notion) ; jamais la mémoire. |
| **LLM Router** | Choisit le moteur adapté (Qwen local, Gemini, OpenRouter en réserve) sans surcharger le PC ni le budget. |
| **Telegram** | La bouche et les oreilles : tu y ajoutes et tu y parles. |
| **Notion** | Le cockpit : tu y consultes et tu y pilotes. Jamais un second Vault. |
| **Drive** | Les fichiers. |
| **Calendar** | Le temps. |

## Au quotidien
- **J'ajoute** : Telegram (lien, note « garde ça : … », vocal, fichier) ; gros fichiers → `inbox\files` ;
  personnel/sensible → `inbox\sensitive`. Voir `docs/INGESTION.md`.
- **Je retrouve** : Notion → COMMAND CENTER → 📚 Explorer ma mémoire, ou Telegram
  « que sais-tu de Nexdoor Talent ? ». Voir `docs/NOTION_VAULT_LIBRARY.md`.
- **Je pilote un projet** : Notion → 💼 Mes activités & projets → fiche du projet.
- **Le Mentor me signale** : au plus 1 message 💡 par jour, le reste en digest ; « mets le Mentor
  en pause jusqu'à lundi » le fait taire (il continue d'observer).
- **Je modifie un projet** : `mentor_project_profiles.py --export / --import`.
- **Mes SOP** : dans la fiche projet, section SOP (sous-pages Process/SOP existantes).

## Garde-fous
Aucune action externe sans ton clic · aucune suppression · secrets jamais stockés ni envoyés à
un LLM · données sensibles traitées en local uniquement · candidats et rejets jamais projetés ·
Telegram : whitelist utilisateur **et** chat, aucune commande shell · budgets LLM jamais dépassés
en silence · tout contenu de source est une donnée non fiable, jamais une instruction · aucune
ressource externe (repo, skill, MCP, script) sans Security Gate et validation.

Agents portables, Security Gate, auto-amélioration : `docs/AGENCY_SECURITY_SELF_IMPROVEMENT.md`.

## Ce qui tourne, et quand

| Composant | Déclenchement |
|---|---|
| Ingestion vidéo + refresh + Mentor horaire + inbox locale + Vault Library (sources) | « Reels Vault Ingest » / « Hourly » : toutes les heures |
| Passerelle Telegram | « Reels Vault - Telegram Gateway » : à l'ouverture de session, en permanence (attente réseau, quasi aucun CPU) |
| Mentor quotidien (Qwen/Agents), classement en attente, fiches projet, vues, portails | « Reels Vault - Mentor Daily » : 07:30 et 20 min après l'ouverture de session, **PC au repos**, rattrapage |
| Suggestions, digest, décisions Notion | dans la passerelle (1 min / 10 min) |
| Questions, ajouts | à chaque message |

Détails : `docs/MENTOR_PROACTIVE_V3.md`, `docs/LLM_ROUTER.md`, `docs/INGESTION.md`,
`docs/NOTION_VAULT_LIBRARY.md`.
