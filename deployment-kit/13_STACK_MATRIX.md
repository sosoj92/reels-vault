# 13 — Stack finale

| Brique | Rôle | Statut |
|---|---|---|
| Git | versioning local | Oui |
| GitHub | code/branches | Oui |
| Claude Code | installation/maintenance/diagnostic | Oui pour ce playbook |
| PowerShell | administration Windows | Oui |
| Python + .venv | runtime (3.14.4 validé, voir `lis.lock.json`) | Oui |
| lis.lock.json | versions et SHA exacts | Oui |
| CHECK_LIS / setup_lis / backup_lis / restore_lis | Health Check, installation, sauvegarde, restauration | Oui |
| SQLite / vault.db | mémoire canonique | Oui |
| yt-dlp / gallery-dl / FFmpeg | ingestion média | Oui pour vidéo |
| faster-whisper | transcription locale | Oui |
| multilingual-e5-small | retrieval sémantique | Recommandé |
| Mentor | proactivité | Oui |
| Agency Agents | playbooks experts | Oui |
| Security Gate | inspection ressources | Oui |
| lis_improvement | auto-amélioration supervisée | Oui |
| LLM Router | routage IA | Oui |
| Ollama/Qwen | local/sensible | Référence |
| Gemini API | cloud | Option |
| OpenRouter | multi-modèle/réserve | Référence actuelle |
| Anthropic API | cloud | Option |
| Mistral | historique/hors runtime | Non |
| Telegram | interface rapide/mobile | Référence |
| Notion | cockpit | Référence |
| ChatGPT | analyse riche + connecteurs | Très utile |
| Jarvis | interface vocale | Installation complète |
| MacroDroid | capture Android | Installation complète Android |
| Syncthing | Android ↔ PC | Installation complète Android |
| Task Scheduler | automatisation Windows | Oui |
| Tailscale | non retenu | Non |

## Configuration de référence Rosy
Windows ; Ollama/Qwen local ; OpenRouter actif ; Gemini supporté ; Anthropic optionnel ; Telegram actif ; Notion cockpit ; ChatGPT via connecteurs ; Jarvis bridge ; Android MacroDroid + Syncthing ; Tailscale hors stack.
