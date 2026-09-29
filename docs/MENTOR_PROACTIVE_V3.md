# Mentor Engine V3 — canal proactif, phase 1

Branche `feature/mentor-proactive-v3`, partie de `main` (V2 mergée).
Rien de la V1/V2 n'est remplacé : `vault.db` reste la source de vérité, Notion une projection,
Qwen/Ollama le LLM local par défaut, aucun cloud, aucune action externe sans validation humaine.

```
EVENT → détection déterministe → candidat → score → shortlist │ → Qwen → Agency Agents → suggestion → décision humaine
└──────────────────────── phase 1 (ce document) ─────────────┘ └──────────── phase 2 (non branchée) ────────────┘
```

## Modules

| Fichier | Rôle |
|---|---|
| `mentor_proactive_store.py` | Schéma (5 tables additives), file d'événements, candidats, suggestions `SUG-xxxx` et leur cycle de vie, journal des consultations Agency, verrou du vrai Vault |
| `mentor_proactive.py` | Publication des événements depuis l'état du Vault, détecteurs, score, dédoublonnage, shortlist, passe CLI, démo |
| `vault_refresh.py` | Étape 5 ajoutée : **publie** les événements (aucune détection). Non bloquante, `--skip-proactive-events` |

## Schéma SQLite (additif, idempotent)

`meta.mentor_proactive_schema_version = 1`. Aucune table ni colonne existante n'est modifiée.

| Table | Contenu |
|---|---|
| `mentor_events` | `event_id` stable `EVT-<sha256(dedup_key)[:16]>`, `dedup_key` UNIQUE, `event_type`, `entity_type`, `entity_id`, `project_id`, `payload` JSON, `status` (pending/processed/failed/ignored), `attempts`, `last_error`, `result`, `publisher`, `created_at`, `processed_at` |
| `mentor_candidates` | `candidate_id` `CAND-<fingerprint[:12]>`, `candidate_type`, `project_id`, `focus_entity_id`, `focus_key`, `title`, `score`, `score_breakdown`, `evidence`, `source_ids`/`claim_ids`/`task_ids`/`insight_ids`/`event_ids`, `fingerprint` UNIQUE, `status`, `rejection_reason`, `seen_count`, `detector_version`, horodatages |
| `mentor_suggestions` | `suggestion_id` `SUG-0001`…, `candidate_id` UNIQUE, `suggestion_type`, `title`, `summary`, `explanation`, `project_id`, `score`, `evidence`, `fingerprint` UNIQUE, `status`, `decision`, `decision_note`, `decided_by/at`, `snoozed_until`, `notion_page_id`, `telegram_message_id`, `status_history`, horodatages |
| `mentor_agent_consultations` | `consultation_id` `CONS-…`, `agent_id`, `agent_name`, `mode` A/B/C, `session_id`, `suggestion_id`, `candidate_id`, `inputs`, `verdict`, `value_score` 0-5, `proposed_action`, `main_objection`, `confidence` 0-1, `evidence_refs`, `model`, `created_at` |
| `mentor_watch_state` | dernière empreinte d'un projet : `project_changed` n'est émis que sur un vrai changement |

## Événements

| Type | Publié par | Clé de dédoublonnage |
|---|---|---|
| `source_ingested` | scan du Vault (refresh) | `source_ingested:source:<id>` |
| `insight_created` / `insight_validated` | scan (insights du Mentor ; validé = statut validated/accepted/approved/confirmed) | `…:insight:<id>` |
| `task_created` / `task_completed` | scan (`actions`) | `…:task:<id>` |
| `task_overdue` | scan : tâche approved/todo/doing, échéance dépassée | `task_overdue:task:<id>:<échéance>` |
| `project_changed` | scan : nom, type, attention ou parent d'une entité projet modifié | ancien→nouveau hash |
| `question_asked` | API `mentor_proactive.publish_question()` (pas encore branchée sur Ask) | question normalisée + jour |
| `daily_pass` | scan, une fois par jour | `daily_pass:<jour>` |

Un **projet** = entité de type project/activity/vertical/client/prospect, ou entité rattachée dans
`intelligence_entity_bindings` (Nexdoor Partners, verticales, clients, prospects).

## Candidats

| Type | Déclencheur | Condition déterministe | Empreinte |
|---|---|---|---|
| convergence | source_ingested, project_changed, daily_pass | ≥ 3 sources liées à la même entité sur 120 jours | entité + palier (3-4 / 5-7 / 8+ sources) |
| opportunity | insight_created / validated | insight opportunity ou opportunity_convergence | insight |
| reactivation | source_ingested, question_asked | entité sans activité depuis ≥ 180 jours qui revient | entité + source (ou question) |
| tension | source_ingested, daily_pass | évaluation V2 contradiction / possible_tension / possible_supersession ouverte | évaluation |
| forgotten | task_overdue, daily_pass | tâche engagée en retard ; entité « Maintenant » inactive ≥ 21 jours | tâche + échéance ; entité + dernière activité |
| next_action | task_completed, project_changed, daily_pass | projet (ou entité « Maintenant ») sans autre tâche ouverte | tâche terminée ; entité + date |

**Score** : somme pondérée de facteurs dans [0, 1] (poids par type dans `WEIGHTS`, somme = 1),
détail conservé dans `score_breakdown`. Exemple convergence :
volume 0,30 · indépendance des auteurs 0,20 · récence 0,15 · ancrage projet 0,20 · appui d'insights 0,15.

**Statuts** :
- `rejected` (journalisé, jamais présenté ni projeté, jamais rouvert) : `same_author_only`, `sensitive_topic`
  (santé/finance/droit, détecteur existant de `vault_analysis`), `entity_dismissed`, `insight_dismissed`,
  `below_floor:<score>` (< 0,30) ;
- `watching` : conservé et re-scoré à chaque nouvelle preuve — `no_project_anchor` ou `below_shortlist_threshold` ;
- `detected` → `shortlisted` : score ≥ 0,55 et ancré (projet = 1 ; entité « Maintenant » = 0,7), 5 au plus par passe ;
- `expired` : remplacé par un palier supérieur (`superseded_by:…`) ou en observation sans nouvelle preuve depuis 60 jours ;
- `promoted` : a donné une suggestion (phase 2).

**Dédoublonnage** : même empreinte → mise à jour du candidat ouvert (`seen_count`, `event_ids`, preuves, score),
jamais de second candidat. Un candidat fermé n'est pas rouvert.

## Suggestions (préparées, non alimentées en phase 1)

`promote_candidate()` crée `SUG-xxxx` au statut `ready`. Transitions contrôlées :
`ready → sent → opened → accepted | dismissed | snoozed | expired`, `snoozed → ready` (via `wake_snoozed`),
puis `accepted → action_proposed → approved → executed` (statuts déjà présents dans le schéma).
`projectable_suggestions()` est la seule lecture autorisée pour une future projection Notion/Telegram :
elle ne renvoie que des suggestions, jamais de candidats.

## Verrou du vrai Vault

Comme pour la V2 : tant que la phase n'est pas validée, **aucune** écriture proactive sur le vrai `vault.db`
(ni tables, ni événements). `vault_refresh` renvoie `proactive_events: skipped / proactive_not_validated`,
`--confirm` est refusé avant toute ouverture. Vérifié en production : le passage planifié de 20:50 sur
cette branche n'a créé aucune table `mentor_*` proactive.

Déverrouillage, après validation : créer `mentor_proactive.enabled` à la racine (non versionné) ou `VAULT_PROACTIVE_WRITES=1`.

## Commandes

```powershell
.\.venv\Scripts\python.exe mentor_proactive.py --demo        # scénario fictif
.\.venv\Scripts\python.exe mentor_proactive.py --dry-run     # vrai Vault, copie en mémoire
.\.venv\Scripts\python.exe mentor_proactive.py --status      # file et candidats (lecture seule)
.\.venv\Scripts\python.exe mentor_proactive.py --dry-run --json
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_mentor_proactive.py"
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

## Constat sur le vrai Vault (dry-run du 2026-09-29)

137 événements, 52 candidats, 3 rejetés, **shortlist vide** : les 8 entités projet (Nexdoor Partners,
verticales, clients, prospects) n'ont aucune source liée ; les convergences réelles portent sur des outils
(GitHub 7 sources, Claude 6, ChatGPT 5…) et restent en observation (`no_project_anchor`).
Le canal proactif ne produira de suggestions de projet qu'une fois les sources rattachées aux projets.

## Phase intermédiaire — propositions de rattachement source → projet (`mentor_project_links.py`)

Réutilise les projets (`intelligence_entities` + `intelligence_entity_bindings`) et le lien réel
existant `intelligence_entity_sources` (nouveau rôle `relevant_to`, créé **seulement** après validation).
`intelligence_resolution_candidates` n'est pas détournée (identité entité ↔ entité).
Table additive : `mentor_project_link_proposals` (`PL-0001`…, confiance, niveau, raison, preuves JSON,
empreinte unique, une seule proposition ouverte par paire source/projet, statuts
proposed/approved/rejected/superseded, `reviewed_at`). Créée seulement par `--review`.

Profils de projet traçables : chaque terme porte sa provenance (nom du projet, exemple de Rosy,
synonyme, déduit à valider). Les synonymes d'une même idée forment un **concept** et ne s'additionnent pas.

| Niveau | Règle |
|---|---|
| HIGH | nom du projet / client / prospect cité ; ou ≥ 3 signaux indépendants (concepts + classement + proximité) et confiance ≥ 0,80 |
| MEDIUM | ≥ 2 concepts du projet + un élément de contexte (ou un 3ᵉ concept), confiance ≥ 0,55, pas de concurrent proche |
| LOW | un seul concept, contexte seul, résumé IA seul, source sensible sans nom, parent couvert par un enfant nommé |
| AMBIGUOUS | alias partagé (« Nexdoor »), ou deux projets non liés à moins de 0,15 d'écart |

Outils et thèmes transversaux (ChatGPT, Claude, IA, automatisation, agents…) ne comptent jamais.
LOW/AMBIGUOUS ne sont jamais appliqués ; HIGH non plus sans validation humaine.

```powershell
.\.venv\Scripts\python.exe mentor_project_links.py --dry-run --show 20
.\.venv\Scripts\python.exe mentor_project_links.py --review   # plus tard : sauvegarde, puis [1] approuver [2] refuser [3] passer
```

Modes de consultation Agency (stockés, non exécutés) : A = agent nommé par Rosy ;
B = agents choisis automatiquement pour une question de Rosy ; C = agents choisis par le Mentor
pour évaluer un candidat avant suggestion.

## Profils de projet persistants (`mentor_project_profiles`, rules `project-links-v2`)

Les connaissances métier ne sont plus dans le code : table additive `mentor_project_profiles`
(une ligne par entité projet existante : `description`, `core_topics`, `context_topics`,
`named_entities`, `specific_tools`, `excluded_topics`, `profile_status`, `profile_source`, `notes`).
`intelligence_entities.summary/metadata` ne sont pas réutilisés (texte libre / JSON d'identité écrit par d'autres modules).

- Profils initiaux : `setup_project_profiles.py` (données de Rosy du 2026-09-29) ; ne remplace jamais un profil existant sans `--force`.
- Édition : `mentor_project_profiles.py --export profils.json`, modifier, puis `--import profils.json`.
- Sujet = idée ; `terms` = ses variantes (pluriels, traductions). Deux sujets qui se chevauchent dans le texte comptent une fois.
- Exclusions : `alone_insufficient` (ne compte jamais comme sujet) ; `block` (empêche toute proposition sans mention explicite — santé personnelle en neuropsychologie).
- Règles génériques restées dans le code : termes universels (IA, ChatGPT, Claude…), un client/prospect se désigne par son nom,
  alias partagés dérivés des noms (« nexdoor »), plafond des sources sensibles (sauf projet qui déclare ses anti-signaux santé).
- Décisions prises sur un dry-run : `record_decision()` (PL-0001 source 16 → Nexdoor Talent approuvée, PL-0002 source 12 → Recherche emploi remote refusée).
