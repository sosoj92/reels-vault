# 05 — Jarvis et interface vocale

Jarvis fait partie de l'installation complète comme interface vocale.

## Composants
- `external/jarvis-assistant-vocal`
- `integrations/jarvis/`
- `install_jarvis_voice_bridge.py`
- `vault_voice_bridge.py`

`setup_upstreams.ps1` récupère Jarvis **au SHA exact du lock** et réapplique le bridge.
`setup_lis.ps1 -Install` crée aussi la `.venv` de Jarvis (Python 3.13, `requirements.txt`
de Jarvis). `config.yaml` et les jetons Google de Jarvis sont des secrets : restaurés par
`restore_lis.ps1 -RestoreSecrets`, ou à créer depuis `config.example.yaml`.

Vérification (lecture seule) puis réapplication explicite :

~~~powershell
.\.venv\Scripts\python.exe install_jarvis_voice_bridge.py --check
.\.venv\Scripts\python.exe install_jarvis_voice_bridge.py
~~~

Le bridge copie tous les modules `integrations/jarvis/*.py` dans `tools/` de Jarvis
(life_intelligence, mentor_calendar, calendar_state_sync, replanning_*, dynamic_planner,
weekly_horizon), ajoute le routage vocal et le résumé Mentor/agenda du brief, de façon
idempotente (un fichier n'est réécrit que si son contenu change), sans modifier les secrets.

## Doctrine
Jarvis n'est ni mémoire ni source de vérité ni LLM imposé.

~~~text
Voix → Jarvis → Voice Bridge → LIS → Vault / Mentor / Agency / LLM Router → réponse
~~~

## Test obligatoire
Faire une vraie demande vocale qui atteint le LIS et produit une réponse basée sur le contexte réel.
