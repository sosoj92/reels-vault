# 05 — Jarvis et interface vocale

Jarvis fait partie de l'installation complète comme interface vocale.

## Composants
- `external/jarvis-assistant-vocal`
- `integrations/jarvis/`
- `install_jarvis_voice_bridge.py`
- `vault_voice_bridge.py`

`setup_upstreams.ps1` récupère Jarvis et réapplique le bridge lorsque possible.

Réapplication explicite :

~~~powershell
.\.venv\Scripts\python.exe install_jarvis_voice_bridge.py
~~~

Le bridge copie l'outil Life Intelligence dans Jarvis et ajoute le routage prévu, sans modifier les secrets.

## Doctrine
Jarvis n'est ni mémoire ni source de vérité ni LLM imposé.

~~~text
Voix → Jarvis → Voice Bridge → LIS → Vault / Mentor / Agency / LLM Router → réponse
~~~

## Test obligatoire
Faire une vraie demande vocale qui atteint le LIS et produit une réponse basée sur le contexte réel.
