# 02 — LLM, modèles et routage

Le LIS est multi-modèle. Les Agency Agents ne sont liés à aucun fournisseur.

## Routeur
`llm_router.py` choisit selon tâche, sensibilité, disponibilité, coût et fallback.

Chaînes actuelles :
- deterministic : aucun LLM ;
- light : local → Gemini → OpenRouter → file ;
- standard : Gemini → local → OpenRouter → file ;
- complex : Gemini → OpenRouter → file ;
- sensitive : local uniquement dans la configuration de référence.

## Ollama / Qwen
Référence : `OLLAMA_MODEL=qwen3:1.7b`.

Rôle : coût cloud nul, traitement local, données sensibles, tâches légères.

Après installation Ollama/modèle :

~~~powershell
.\.venv\Scripts\python.exe llm_setup.py --status
~~~

Qwen doit être READY quand service/modèle/ressources sont disponibles.

## Gemini
Supporté nativement, optionnel. Modèles séparés light/standard/complex.

## OpenRouter
Réserve/multi-modèle. L'instance de référence l'utilise actuellement pour certaines analyses complexes. Budgets, réserve de crédit et journal d'usage sont intégrés.

## Anthropic API
Optionnelle. L'abonnement Claude.ai/Claude Code n'est pas une clé Anthropic API. Hors chaînes automatiques par défaut.

## ChatGPT / OpenAI
ChatGPT peut servir d'interface riche via connecteurs et paquets Agency sans API OpenAI. Une API OpenAI pourrait être ajoutée comme provider futur mais n'est pas requise aujourd'hui.

## Mistral
Hors runtime final de référence. Ne pas l'installer/configurer par défaut.

## Configuration

~~~powershell
.\.venv\Scripts\python.exe llm_setup.py
.\.venv\Scripts\python.exe llm_setup.py --status
~~~

Les clés sont saisies masquées et stockées dans `.env` après test réussi.

Conserver `LLM_SENSITIVE_CLOUD_ALLOWED=false` sauf décision explicite.
