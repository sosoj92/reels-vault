# 09 — Health Check / Definition of Done

Un LIS n'est READY que si les tests pertinents sont réellement exécutés.

## Contrôle automatisé (lecture seule)

~~~powershell
.\CHECK_LIS.ps1
~~~

Couvre automatiquement les sections Cœur, IA (sans appel cloud), Telegram (configuration et
tâche), Notion (configuration), Jarvis (SHA, bridge, venv, config), Android (côté PC et
Syncthing), Windows et Sécurité ci-dessous. Format `COMPONENT | STATUS | DETAILS`, statuts
`OK`, `WARNING`, `FAIL`, `NOT CONFIGURED`, `MANUAL TEST REQUIRED`.

| Code | Verdict |
|---|---|
| 0 | `LIS READY` |
| 1 | `READY WITH WARNINGS` |
| 2 | `NOT READY` |

Les lignes `MANUAL TEST REQUIRED` (vocal Jarvis, partage Android, MacroDroid, connecteurs
ChatGPT, `llm_setup.py --status` qui appelle Gemini) restent à valider avec la checklist
ci-dessous. Rapport : `deployment-kit/reports/check_lis-*.json` (ignoré par Git).

## Cœur
- [ ] Git repo propre sur la branche attendue.
- [ ] Python `.venv` fonctionne.
- [ ] `vault.db` s'ouvre.
- [ ] retrieval lexical fonctionne.
- [ ] retrieval sémantique fonctionne si installé.
- [ ] Security Gate fonctionne en lecture seule.
- [ ] Agency registry charge les profils dynamiquement.

## IA
- [ ] Qwen/Ollama répond localement.
- [ ] `llm_setup.py --status` cohérent.
- [ ] au moins un cloud répond si choisi.
- [ ] une donnée sensitive n'est pas routée au cloud.
- [ ] budgets et journal d'usage actifs.

## Telegram
- [ ] bot whitelisté.
- [ ] question simple.
- [ ] grosse analyse.
- [ ] réponse longue complète/découpée.
- [ ] Agent nommé réellement appliqué.
- [ ] contexte conservé.

## Notion / ChatGPT
- [ ] source non sensible projetée.
- [ ] cockpit/projets visibles.
- [ ] aucune donnée sensible détaillée projetée.
- [ ] connecteurs ChatGPT configurés si choisis.
- [ ] paquet Agency lisible dans ChatGPT.

## Jarvis
- [ ] bridge installé.
- [ ] vraie demande vocale → LIS → réponse.

## Android
- [ ] partage réel.
- [ ] MacroDroid écrit.
- [ ] Syncthing synchronise.
- [ ] PC voit le fichier.
- [ ] ingestion crée la source.

## Windows
- [ ] gateway lancée par tâche.
- [ ] redémarrage automatique testé.
- [ ] Mentor Daily présent.
- [ ] rattrapage configuré.

## Sécurité
- [ ] `.env` ignoré.
- [ ] aucun secret dans Git/logs.
- [ ] test d'injection reste UNTRUSTED.
- [ ] Telegram n'expose pas de shell.

## Rapport final

~~~text
LIFE INTELLIGENCE SYSTEM — INSTALLATION REPORT

Git/GitHub .............. OK
Python/.venv ............ OK
Vault ................... OK
Ingestion ............... OK
Embeddings .............. OK / OPTIONAL
Agency Agents ........... OK
Security Gate ........... OK
Qwen/Ollama ............. OK
Gemini .................. READY / NOT CONFIGURED
OpenRouter .............. READY / NOT CONFIGURED
Anthropic ............... READY / NOT CONFIGURED
Telegram ................ OK
Notion .................. OK / NOT CONFIGURED
ChatGPT connectors ...... OK / NOT CONFIGURED
Jarvis .................. OK / NOT INSTALLED BY CHOICE
Android/MacroDroid ...... OK / NOT CONFIGURED
Syncthing ............... OK / NOT CONFIGURED
Windows Tasks ........... OK
Backup readiness ........ OK

LIS READY
~~~

Un composant non choisi peut être `NOT CONFIGURED`. Un test critique choisi mais échoué interdit `LIS READY`.

`Backup readiness` : `CHECK_LIS.ps1` (section BACKUP) vérifie les scripts et l'âge du
dernier dossier `LIS-BACKUP-*` (WARNING au-delà de 14 jours).
