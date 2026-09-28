# Life Intelligence — V1

État au 2026-09-28. Branche `feature/vault-v3-intelligence`.

## Rôles

| Couche | Rôle |
|---|---|
| `vault.db` (SQLite) | source de vérité |
| Notion | interface humaine (Mentor Insights, cockpit) |
| Google Calendar | projection temporelle (Calendar Planning V1, outils Jarvis) |
| Jarvis | runtime vocal / orchestrateur |
| Agency Agents | playbooks de raisonnement (lentilles), routés par `vault_orchestrator.py` |
| Ollama `qwen3:1.7b` | traitement local (`think=false`). `qwen3:4b` : trop lent, jamais en interactif |
| Cloud (OpenAI / Claude) | **non requis**. Les cas qui l'exigeraient partent en `review_required` |

## Pipeline d'ingestion

```
téléphone → MacroDroid → Syncthing → mobile-inbox\inbox.txt → inbox.bat
  → ingest.py → vault_refresh.py → mentor_worker.py --confirm → mentor_notion_sync.py --confirm
```

- `inbox.bat` dérive `ROOT` de son propre emplacement et fait `pushd` dessus : aucune base
  vide ne peut être créée dans le répertoire courant de l'appelant.
- Tous les scripts ouvrent `vault.db` par chemin absolu (surcharge possible : `REELS_VAULT_DB`).
- `mentor_worker.py` : si Ollama/qwen3:1.7b est indisponible, l'analyse est **reportée**
  (aucune écriture, code retour 0) au lieu de figer les sources en « heuristique seule ».
- `mentor_worker.py --source-id N` : ré-analyse à blanc d'une source (jamais d'écriture).
- Escalade : domaine sensible (`research_evidence`, `finance`, `legal`) + Agency demande une
  vérification, ou confiance Agency < 0.55 → `intelligence_mentor_review_queue` (`review_required`),
  pas d'insight publié.
- `mentor_notion_sync.py` : idempotent (`notion_page_id`), commit page par page ; colonne
  « Modèle » = `analysis_model` réel.

## Ask Life Intelligence (retrieval-first)

```
Jarvis (voix) → router_demande_agents() → ask_life_intelligence / consulter_agents_vault
  → vault_voice_bridge.py --ask [--mode consult] → vault_ask.ask()
      1. vault_intelligence_context.search_saved_sources()   ~0,1–0,3 s, déterministe
      2. réponse citée (titres, @auteur, extrait, URL) + « pourquoi utile » (classement Mentor)
      3. prudence épistémique si sujet sensible ; note « contradictions non disponibles »
      4. synthèse qwen3:1.7b OPTIONNELLE, échéance 25 s, jamais sur santé/finance/droit
```

Recherche (`search_saved_sources`) :
- champs pondérés : titre 5 > descriptions (manuelle, IA, originale) 3,5 > catégorie/sous-thèmes/tags 3
  = ressources 3 > auteur 1,5 > transcript 1 ;
- mots de rappel retirés (« sauvegardé », « trouvé », « mes vidéos », « Vault »…) ;
- idf sur le corpus, racinisation française légère, accents pliés, petite table FR/EN
  (TDAH↔ADHD, prospection↔leads…) ; les variantes d'un même terme ne s'additionnent pas ;
- noms génériques (« outils », « astuces »…) à poids réduit ;
- termes courts (RTK, IA, SEO) : mot exact uniquement ;
- un mot isolé dans un transcript ne suffit pas ; seuil relatif au meilleur résultat ;
- robuste aux connexions `sqlite3.Row` **et** tuples, fonctionne en lecture seule.

Entités / claims / relations : enrichissement uniquement (`linked_entities`, `entities`).
`intelligence_claims` et `intelligence_relations` sont vides : la détection de contradictions est
déclarée `unavailable_no_structured_claims`, sans faux positif.

Réglages Jarvis (`config.yaml`, facultatifs) :
- `integrations.life_intelligence_vault` : chemin du Vault (défaut `~/reels-vault`)
- `integrations.life_intelligence_llm` : `false` pour désactiver la synthèse Qwen
- `integrations.life_intelligence_llm_timeout` : échéance Qwen en secondes (défaut 25, max 45)

Variables côté Vault : `VAULT_OLLAMA_URL`, `VAULT_ASK_MODEL`, `VAULT_ASK_LLM_TIMEOUT`.

API locale : `POST /intelligence/ask` `{"question": "...", "mode": "ask", "llm": false}`.

Ajouter un provider cloud plus tard : une fonction de même signature que
`vault_ask.synthesize_local` dans `vault_ask.SYNTHESIZERS` ; le pipeline ne change pas.

Ollama tourne sur CPU ici : `num_ctx` est fixé à 4096 partout (défaut Ollama, identique au
Mentor) car tout changement de `num_ctx` force un rechargement du modèle (5 à 9 s).

## Commandes utiles

```powershell
.\.venv\Scripts\python.exe vault_ask.py "Qu'est-ce que j'avais sauvegardé sur RTK ?" --no-llm
.\.venv\Scripts\python.exe vault_voice_bridge.py "Consulte mon Vault sur le TDAH" --ask --mode consult --json
.\.venv\Scripts\python.exe mentor_worker.py --source-id 64
.\.venv\Scripts\python.exe mentor_notion_sync.py --limit 20          # à blanc
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe install_jarvis_voice_bridge.py            # recopie l'outil dans Jarvis
```

## Tests V1 (2026-09-28)

| # | Test | Résultat |
|---|---|---|
| 1 | « …sauvegardé sur les outils pour économiser des tokens ? » | RTK #1, Astra-Flash #2, Can I Vibe Code It #3 ; 0,2 s sans LLM, ~12–26 s avec synthèse |
| 2 | « Retrouve ce que j'avais trouvé sur la prospection LinkedIn » | Apify+Claude, Prospection Claude/Exa/LinkedIn, LinkedIn Agent Skill, 50 prospects Google Maps |
| 3 | « Consulte mon Vault sur le TDAH » | TSG (étude sur rats) + guanfacine/clonidine (ADHD) ; avertissement ; lentilles Research Synthesist / Statistician / Psychologist ; pas de synthèse LLM |
| 4 | Question sans résultat (tarte aux pommes) | « aucune source suffisamment pertinente », rien d'inventé, 2,4 s |
| 5 | Hors Vault | 10 phrases testées (météo, lumière, « j'ai trouvé mes clés »…) : non capturées |
| 6 | `mentor_worker --source-id 64` (modèle déchargé avant chaque passage, ×2) | opportunity / 0.612 / 0.75 / `qwen3:1.7b+agency` / needs_cloud=false / publishable=true, identique aux 2 passages — voir limite ci-dessous |
| 7 | `mentor_notion_sync` | 0 en attente, 53 insights = 53 pages distinctes ; reprise après erreur sans doublon (test) |
| 8 | `inbox.bat` ×2 sur copie jetable, cwd externe, faux Notion | code 0 (32,7 s puis 12,2 s), aucune base parasite, vrai Vault et vrai inbox intacts |
| — | Chaînage ingest → refresh → mentor → notion (copie, fiche synthétique) | PASS ; source 65 importée, analysée `qwen3:1.7b+agency`, envoyée en review |
| — | Ollama injoignable | Ask répond en 2,3 s (réponse citée) ; Mentor reporte l'analyse |
| — | Playbooks Agency absents | le Mentor envoie en review au lieu de planter (bug trouvé et corrigé en validation) |
| — | Calendar Planning V1 | 6 outils lecture seule OK, tables inchangées |
| — | Suites automatisées | Vault 143/143, Jarvis 181/181 |

### Limite connue : instabilité de qwen3:1.7b

Sur la source 64, l'avis Agency varie selon l'état du cache d'Ollama : premier appel après
chargement → relevance 0.65 / confiance 0.75 (publiable) ; même prompt rejoué à chaud →
0.45 / 0.45 (→ review). La fonction de décision est déterministe (testée avec les valeurs
validées) et l'échec est prudent (review, jamais d'insight fragile publié), mais le verdict
d'une source limite n'est pas reproductible avec ce modèle.
