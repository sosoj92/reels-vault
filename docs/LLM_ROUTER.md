# LLM Router — `llm_router.py`

Un seul routeur choisit le moteur. Les modules métier (Mentor, Agency Agents, Telegram,
synthèses, classement à l'ingestion) déclarent seulement une **tâche** et une **sensibilité**.

| Tâche | Moteur |
|---|---|
| `deterministic` | aucun LLM (Python, SQLite, embeddings) — erreur si un LLM est demandé |
| `light` | Qwen/Ollama local **si disponible et PC assez libre**, sinon Mistral |
| `standard` | Mistral API, sinon Qwen local |
| `complex` | Claude API si configurée, sinon Mistral, sinon local |

Claude n'est jamais un repli : seulement pour une vraie tâche `complex`.
Clé absente → fournisseur désactivé, le reste continue.

## Confidentialité (avant tout appel)

| Classe | Règle |
|---|---|
| public / internal | cloud autorisé selon le routage |
| personal (emails, téléphones) | cloud seulement si `LLM_PERSONAL_CLOUD_ALLOWED=true` |
| sensitive (santé, traitements, finances privées, confidentiel) | **local uniquement** (`LLM_SENSITIVE_CLOUD_ALLOWED=false`) |
| secret (mots de passe, clés API, tokens, IBAN, cartes) | **jamais** envoyé à un LLM |

## Budget et journal
`logs/llm_usage.db` (hors Git, hors `vault.db`) : fournisseur, modèle, tâche, tokens, coût estimé,
durée, succès, repli. Jamais de contenu ni de secret. Budgets `LLM_DAILY_BUDGET_EUR`,
`LLM_MONTHLY_BUDGET_EUR`, `LLM_CLAUDE_DAILY_BUDGET_EUR` vérifiés avant chaque appel cloud.

## Résilience
Timeout par fournisseur (local 75 s, Mistral 30 s, Claude 60 s), une relance courte (jamais après
un timeout), coupe-circuit : 3 échecs → pause de 5 min. Hors ligne : réponse immédiate `None`,
les analyses attendent (candidats, classement), le Vault, l'ingestion et Telegram continuent.

## PC
Avant Qwen : Ollama joignable, CPU < `LLM_LOCAL_MAX_CPU`, RAM libre ≥ `LLM_LOCAL_MIN_FREE_MB`
(sauf si Qwen est déjà chargé). Déchargement après `OLLAMA_IDLE_UNLOAD_MINUTES`.

## Configuration
`.env` (voir `.env.example`) : modèles, clés, politique, budgets. `python llm_router.py` affiche
l'état de chaque moteur (utilisable ou non, et pourquoi).

Hors routeur, volontairement : les extracteurs batch V2 (`vault_claims`, `vault_analysis`) qui
ont déjà un choix explicite de fournisseur en ligne de commande.
