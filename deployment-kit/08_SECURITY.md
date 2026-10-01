# 08 — Sécurité et confiance

## Secrets
Jamais dans Git, Telegram, Vault, prompt, log, Notion ou consultation Agency.

Les secrets locaux vivent dans `.env` ou le mécanisme sécurisé de l'intégration. `.env` reste ignoré par Git.

## Classes
- public/internal : cloud possible selon politique.
- personal : cloud seulement si explicitement autorisé.
- sensitive : local/Qwen dans la configuration de référence.
- secret : jamais envoyé à un LLM.

## Indirect prompt injection
Tout contenu externe (transcript, README, PDF, CV, email, page web) est une donnée non fiable.

Le LIS marque les extraits `UNTRUSTED SOURCE CONTENT`. Une phrase comme « Ignore everything before this » ne doit jamais modifier le prompt système.

## Security Gate

~~~powershell
.\.venv\Scripts\python.exe vault_security_gate.py CHEMIN
~~~

Flux obligatoire :

~~~text
IDEA → SEARCH → SHORTLIST → SECURITY REVIEW → VALUE REVIEW → HUMAN APPROVAL → TEST ISOLÉ → INTÉGRATION éventuelle
~~~

Le scan est heuristique et ne déclare jamais « safe ». Revue humaine obligatoire.

## Git
main = stable → branche → tests → test réel → READY TO MERGE → merge → push → redémarrage → smoke test.

Pas de force push par défaut.

## Upstreams
Popularité/étoiles GitHub ≠ preuve de sécurité. Conserver les SHA validés dans les rapports.
