# LLM Router — `llm_router.py`

Un seul routeur choisit le moteur. Les modules métier (Mentor, Agency Agents, Telegram,
synthèses, classement à l'ingestion) déclarent une **tâche** et une **sensibilité**.
Repli ultime : **file d'attente** — l'analyse attend et reprend ; Vault, Telegram, ingestion et
Notion continuent toujours.

| Tâche | Chaîne |
|---|---|
| `deterministic` | aucun LLM (Python, SQLite, embeddings) |
| `light` | Qwen local (si dispo et RAM/CPU suffisants) → Gemini → OpenRouter → file |
| `standard` | Gemini → Qwen local (si le PC peut) → OpenRouter → file |
| `complex` | Gemini (modèle de raisonnement) → OpenRouter → file |
| hors ligne | Qwen local uniquement, sinon déterministe + file |

Chaînes modifiables : `LLM_CHAIN_LIGHT`, `LLM_CHAIN_STANDARD`, `LLM_CHAIN_COMPLEX`.
Mistral est présent mais hors des chaînes (désactivé). Claude API : optionnelle, hors des
chaînes par défaut.

## Confidentialité
| Classe | Règle |
|---|---|
| public / internal | Gemini / OpenRouter selon la chaîne |
| personal | cloud seulement si `LLM_PERSONAL_CLOUD_ALLOWED=true` |
| sensitive (santé, traitements, finances privées, confidentiel) | **Qwen local uniquement**, sinon file |
| secret (mots de passe, clés, tokens, IBAN, cartes) | **jamais** envoyé à un LLM |

## Gemini
Gemini Developer API, clé en en-tête (`x-goog-api-key`, jamais dans l'URL). Un modèle par
niveau (`GEMINI_LIGHT_MODEL`, `GEMINI_STANDARD_MODEL`, `GEMINI_COMPLEX_MODEL`) ; raisonnement
désactivé pour léger/standard, budget de raisonnement pour complexe. Clé refusée →
« INVALID », fournisseur désactivé, rien ne bloque.

## OpenRouter (réserve)
Budget local : `OPENROUTER_DAILY_BUDGET_USD` (0,25), `OPENROUTER_MONTHLY_BUDGET_USD`
(optionnel), **réserve** `OPENROUTER_RESERVE_USD` (1,00) : crédit restant lu sur l'API
(`/credits`), sinon estimé (`OPENROUTER_INITIAL_CREDIT_USD` − dépenses journalisées). Réserve atteinte →
plus d'appel automatique. Un modèle explicite + au plus 2 modèles de secours ; jamais de
modèle très coûteux, jamais Claude via OpenRouter pour une tâche simple. Coût réel lu dans la
réponse (`usage.cost`).

## Résilience et facturation
Un appel cloud n'est **jamais relancé** (pas de double facturation) : le repli passe au moteur
suivant. Réseau coupé → mode hors ligne 2 min (local uniquement), puis retour automatique.
Coupe-circuit : 3 échecs → pause de 5 min. Timeouts : local 75 s, Gemini 40 s, OpenRouter 45 s.

## Journal
`logs/llm_usage.db` (hors Git, hors `vault.db`) : backend (local / gemini / openrouter), modèle,
tâche, durée, tokens, coût (EUR/USD), repli, succès. Jamais de clé ni de contenu.

## Configuration
`python llm_setup.py` : saisie masquée des clés Gemini / OpenRouter, test réel (Gemini : mini
requête ; OpenRouter : lecture du crédit, sans coût), enregistrement dans `.env` seulement si
le test réussit. `python llm_setup.py --status` :

```
Qwen        : READY / UNAVAILABLE
Gemini      : READY / NOT CONFIGURED / INVALID
OpenRouter  : READY / NOT CONFIGURED / INVALID
Anthropic   : READY / NOT CONFIGURED
```

Hors routeur, volontairement : les extracteurs batch V2 (`vault_claims`, `vault_analysis`),
qui ont déjà un choix explicite de fournisseur en ligne de commande.
