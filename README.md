# Life Intelligence System (LIS)

Système personnel de mémoire et de proactivité, local d'abord, sur Windows.
Il capte ce que tu sauvegardes (vidéos TikTok/Instagram, notes, vocaux, fichiers),
l'analyse, le relie à tes projets, et te le restitue au bon moment — par Telegram,
Notion, la voix (Jarvis) ou une consultation ChatGPT.

> **`vault.db` (SQLite) est la source de vérité.** Tout y est écrit en premier.
> Notion, Telegram, ChatGPT et Jarvis sont des interfaces ou des projections.

Né du tutoriel « Reels Vault » (ingestion de Reels sauvegardés) ; ce tutoriel historique
est conservé en [`docs/legacy/REELS_VAULT_TUTORIAL.md`](docs/legacy/REELS_VAULT_TUTORIAL.md)
(**LEGACY** : Claude Cowork, iPhone/iCloud — plus la procédure de référence).

---

## Architecture actuelle

Détails : [`FINAL_ARCHITECTURE.md`](FINAL_ARCHITECTURE.md).

| Élément | Rôle |
|---|---|
| **vault.db** | mémoire canonique / source de vérité |
| **GitHub** | code + versioning (jamais de données personnelles ni de secrets) |
| **Notion** | cockpit, projection uniquement (jamais un second Vault) |
| **Telegram** | interface rapide (ajouter, demander, suggestions du Mentor) |
| **ChatGPT** | interface d'analyse via connecteurs ; pas la mémoire |
| **Jarvis** | interface vocale du LIS (Voix → Jarvis → Voice Bridge → LIS → réponse) |
| **Mentor** | proactivité (observe, relie, priorise, parle rarement) |
| **Agency Agents** | playbooks de raisonnement indépendants du modèle |
| **Security Gate** | contrôle obligatoire avant toute ressource externe |
| **LLM Router** | routage selon tâche / coût / confidentialité |
| **Android** | MacroDroid + Syncthing (capture mobile) |
| **Planificateur de tâches** | automatisations persistantes (ingestion, passerelle, Mentor) |
| **Claude Code** | construction, maintenance et diagnostic |
| **PowerShell** | administration Windows |

Stack IA :
- **Ollama + Qwen local** (`qwen3:1.7b`) : chemin par défaut et **seul** moteur du sensible ;
- **Gemini** (supporté), **OpenRouter** (réserve, budgets), **Anthropic API** (optionnelle) ;
- ChatGPT via interface/connecteurs ; Mistral hors runtime par défaut.

Pipeline mobile :

```
TikTok / Instagram → Partager → MacroDroid → inbox.txt → Syncthing
  → PC mobile-inbox\inbox.txt → inbox.bat → ingestion → vault.db
```

---

## Installer, vérifier, sauvegarder, restaurer

Tout est dans [`deployment-kit/`](deployment-kit/README.md) — commencer par
[`deployment-kit/00_START_HERE.md`](deployment-kit/00_START_HERE.md). Versions exactes :
[`deployment-kit/lis.lock.json`](deployment-kit/lis.lock.json).

> Sur un Windows neuf, autorise d'abord les scripts locaux :
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
> (ou préfixe chaque commande par `powershell -ExecutionPolicy Bypass -File`).

**Nouvelle installation** (PC Windows neuf)

```powershell
git clone https://github.com/Roseytech/reels-vault.git $HOME\reels-vault
cd $HOME\reels-vault
.\setup_lis.ps1 -DryRun      # plan, aucune modification
.\setup_lis.ps1 -Install     # installe ce qui manque puis lance le Health Check
```

**Health Check** (lecture seule)

```powershell
.\CHECK_LIS.ps1              # 0 = LIS READY, 1 = READY WITH WARNINGS, 2 = NOT READY
```

**Backup**

```powershell
.\backup_lis.ps1                     # vault.db cohérent + fichiers personnels + manifest SHA-256
.\backup_lis.ps1 -IncludeSecrets     # + secrets chiffrés (AES-256, passphrase)
```

**Restauration** (catastrophe / nouveau PC : clone GitHub + backup)

```powershell
.\restore_lis.ps1 -BackupPath E:\LIS-BACKUPS\LIS-BACKUP-... -VerifyOnly
.\restore_lis.ps1 -BackupPath E:\LIS-BACKUPS\LIS-BACKUP-... -RestoreSecrets
```

**Upstreams** (Agency Agents, Jarvis, Reels Vault d'origine) aux SHA exacts du lock :

```powershell
.\setup_upstreams.ps1 -Audit
```

Voir [`UPSTREAMS.md`](UPSTREAMS.md).

---

## Utiliser le LIS au quotidien

- Ajouter : Telegram (lien, note, vocal, fichier), partage Android, `inbox\files`,
  `inbox\sensitive` — [`docs/INGESTION.md`](docs/INGESTION.md).
- Retrouver : Telegram, Notion (Vault Library), ou
  `.\.venv\Scripts\python.exe vault_ask.py "ma question"`.
- Mentor : [`docs/MENTOR_PROACTIVE_V3.md`](docs/MENTOR_PROACTIVE_V3.md).
- LLM Router : [`docs/LLM_ROUTER.md`](docs/LLM_ROUTER.md).
- Agents, Security Gate, auto-amélioration :
  [`docs/AGENCY_SECURITY_SELF_IMPROVEMENT.md`](docs/AGENCY_SECURITY_SELF_IMPROVEMENT.md).
- Notion : [`docs/NOTION_VAULT_LIBRARY.md`](docs/NOTION_VAULT_LIBRARY.md).

## Garde-fous

Aucune action externe sans validation · aucune suppression · secrets jamais versionnés
ni envoyés à un LLM · sensible traité en local uniquement · Telegram : whitelist
utilisateur **et** chat, aucune commande shell · aucune ressource externe sans Security
Gate · `vault.db`, `.env`, `raw/`, `images/`, `mobile-inbox/` ignorés par Git.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

## Licence

[`LICENSE`](LICENSE).
