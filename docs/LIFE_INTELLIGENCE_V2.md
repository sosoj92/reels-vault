# Life Intelligence — V2

Branche `feature/life-intelligence-v2`, partie de `main` avec la V1 mergée (PR #1).
La V1 reste décrite dans `docs/LIFE_INTELLIGENCE_V1.md`. Tout ce qu'elle garantit tient toujours.

Principes conservés : Vault = source de vérité, retrieval-first, fonctionnement 100 % local possible,
repli déterministe immédiat, aucune publication fragile, aucun cloud obligatoire.

## A. Claims structurés avec provenance — `vault_claims.py`

Étend l'extraction V3 (`vault_analysis.py`) sans la remplacer. Chaque affirmation est stockée dans
`intelligence_claims` (texte, provenance JSON), avec en 1:1 `intelligence_claim_structure` :

| Champ | Contenu |
|---|---|
| `source_id`, `content_id`, `extraction_id` | origine exacte |
| `subject` / `subject_key` / `subject_entity_id` | sujet (entité liée si elle existe déjà ; jamais créée) |
| `predicate` / `predicate_key` | relation ; familles normalisées FR/EN (`reduce`, `cost`, `treat`…) |
| `object_value` / `object_number` / `object_unit` | valeur, nombre et unité parsés |
| `polarity` | affirmative / negative |
| `claim_kind` | fact, hypothesis, advice, opinion, prediction, personal_experience |
| `modality` | asserted, probable, possible, conditional |
| `confidence`, `evidence_type` | confiance plafonnée par règles ; study, measurement, testimony, promotional… |
| `evidence_excerpt`, `evidence_field`, `evidence_match` | **texte réel** de la source, champ exact (dont `description_ai` = résumé IA, marqué seconde main), exact/fuzzy |
| `scope`, `temporal_ref`, `temporal_year`, `observed_at` | périmètre/population, date citée, date d'observation |
| `sensitivity`, `validation_status`, `review_reasons` | santé/finance/droit ; `accepted` ou `needs_review` et pourquoi |
| `provenance` | extracteur, modèle, version, empreinte de la source, date |

Le modèle **propose**, un validateur déterministe **décide** :
- l'extrait cité doit exister dans la source, sinon rejet ;
- un chiffre absent de l'extrait est rejeté ; avec un chiffre, chaque mot de l'objet doit être dans l'extrait (« 80 000 likes » ne devient pas « 80 000 utilisateurs ») ;
- un « fait » porteur de marqueurs d'opinion, conseil, prédiction, expérience ou hypothèse est reclassé, jamais l'inverse ;
- la modalité ne peut pas être plus forte que l'extrait ;
- santé / finance / droit : toujours `needs_review`, confiance ≤ 0,5, `verification_status = unverified` ;
- structure incomplète, sujet ou objet non ancré, confiance < 0,6 : `needs_review` ;
- rien n'est supprimé : une ré-extraction marque les anciens claims `superseded_at` (sauf validation humaine) ;
- idempotent (empreinte du texte + version) ; un JSON tronqué par la limite de tokens garde uniquement les claims complets.

Extracteurs : `ollama` (qwen3:1.7b, `num_ctx` 4096 comme le Mentor), `cloud` (via `vault_cloud`), `static` (tests).

## B. Convergence / contradiction / remplacement / réactivation — `vault_claim_reasoning.py`

Table `intelligence_claim_assessments` (claim_ids, source_ids, détail des vérifications, statut).
Une **contradiction** exige que toutes ces vérifications passent : même sujet, même prédicat, même
périmètre (ou absent des deux), temporalité compatible, modalités comparables (faits/expériences
assertés ou probables), **et** valeurs incompatibles (polarité opposée, ou chiffres à plus de 25 % d'écart).

| Situation | Résultat |
|---|---|
| Mots différents, relation différente | rien |
| Populations/périmètres explicitement différents | rien (pas les mêmes affirmations) |
| Dates différentes, ou source plus récente de ≥ 180 jours | `possible_supersession` (review) |
| Conditions partielles, opinions divergentes | `possible_tension` (review) |
| Toutes les conditions + valeurs incompatibles | `contradiction` (review, jamais tranchée automatiquement) |
| Valeurs compatibles, sources **et auteurs** indépendants | `convergence` |
| Ancien claim (≥ 180 j) rejoint par une source récente (≤ 30 j), ou entité en « now » | `reactivation` |

Rien n'est modifié dans les claims eux-mêmes. Ask affiche les évaluations qui touchent les sources trouvées.

## C. Fiabilité du Mentor — `mentor_consensus.py`

| Règle | Valeur |
|---|---|
| Cas **borderline** si au moins une condition | score final à ±0,10 du seuil 0,60 ; confiance Agency à ±0,10 de 0,60 ; Qwen et Agency classent différemment ; Agency demande une vérification que la décision n'escalade pas |
| Seconde analyse | `mentor_agency.analyze(variant=2)` : second avis sans verdict précédent (pas d'ancrage), spécialistes en ordre inverse, source en tête (pas de réutilisation du cache KV d'Ollama) |
| **Accord suffisant** | même classification ; écart de pertinence ≤ 0,15 ; écart de confiance ≤ 0,20 ; les deux confiances ≥ 0,55 |
| **Publication** | accord suffisant **et** règle V1 (`mentor_decision`) appliquée aux valeurs conservatrices : pertinence = min, confiance = min, needs_cloud = OU |
| Sinon | `review_required` avec la raison exacte (`agency_disagreement:…`, `consensus_below_publication_rule:…`) |

Trace : `intelligence_mentor_consensus` (agency_run_1, agency_run_2, accord, consensus, décision, raison).
Hors borderline : une seule analyse, comportement V1. Jamais dans le chemin d'Ask.

## D1. Adapter cloud — `vault_cloud.py`

- Interface `CloudProvider` (`available()`, `complete()`), registre `PROVIDERS`. Ajouter un fournisseur = une classe.
- `ClaudeProvider` : SDK officiel `anthropic`, modèle `claude-opus-5` (`VAULT_CLAUDE_MODEL`), thinking adaptatif, effort `medium`, fallbacks serveur (`server-side-fallback-2026-07-01`), refus traité comme une erreur.
- Clé **uniquement** via `ANTHROPIC_API_KEY` (processus ou variable Windows). Jamais dans le code ni la base ; messages d'erreur expurgés.
- Sans clé : `get_provider()` renvoie `None`, rien ne casse.
- Utilisations :
  - Ask : `VAULT_ASK_SYNTHESIZER` = `local` (défaut), `cloud` ou `auto` (local puis cloud) ; jamais sur les sujets sensibles ; la réponse citée déterministe reste en tête ;
  - file de review : `python mentor_review.py --confirm`, ou `life_intelligence_batch.py --cloud-review`. Seuls des extraits publics sont envoyés ; les sujets sensibles restent à l'humain ;
  - extraction de claims : `--extractor cloud`.

## D2. Recherche hybride — `vault_embeddings.py`

- Modèle local `multilingual-e5-small` (ONNX int8, 118 Mo, CPU), installé par `setup_semantic_model.ps1` (SHA-256 vérifié), non versionné.
- Index dans `vault.db` : `intelligence_embeddings` (chunk tête + jusqu'à 3 chunks de transcript), `intelligence_embedding_state` (empreinte par source → incrémental), `intelligence_embedding_calibration`.
- Mise à jour automatique à chaque `vault_refresh` (non bloquante) ; `python vault_embeddings.py [--rebuild|--status]`.
- Fusion : le lexical reste le moteur ; le sémantique réordonne (0,75 lexical + 0,25 sémantique).
- Une source trouvée **uniquement** par le sens n'est admise que si son z-score dépasse un seuil **calibré par index** : max(2,4 ; meilleure de 20 questions hors sujet + 0,15). Elle est présentée à part (« rapprochement de sens, à vérifier »).
- Replis lexicaux : modèle absent, `VAULT_SEMANTIC=off`, index vide, erreur ou échéance dépassée (8 s).

## Traitements lourds hors interactif — `life_intelligence_batch.py` / `.bat`

Claims (N sources), raisonnement, index sémantique, et revue cloud seulement avec `--cloud-review`.
Chaque étape est isolée.

## Résultats (validation du 2026-09-28, i3-8130U 2 cœurs, 8 Go, CPU uniquement)

**Tests automatisés :** Vault 205/205 (143 V1 + 62 V2 : claims 19, raisonnement 12, consensus 7,
cloud 12, sémantique 12), Jarvis 181/181.

| Critère V2 | Résultat |
|---|---|
| 1. Claim factuel simple | PASS — sujet/prédicat/objet/nombre/unité, extrait réel, provenance complète |
| 2. Opinion | PASS — reclassée `opinion`, confiance ≤ 0,6, `unverified` |
| 3. Deux sources concordantes | PASS — `convergence` avec claim_ids et source_ids exacts ; même auteur ≠ indépendant |
| 4. Affirmations incompatibles | PASS — `contradiction` + détail des 5 vérifications, statut `needs_review` |
| 5. Populations ou dates différentes | PASS — aucune contradiction (portée différente → rien ; dates → `possible_supersession`) |
| 6. Ancien claim + source récente | PASS — `possible_supersession`, sens ancien → récent |
| 7. Mentor borderline | PASS — double Agency ; désaccord → review ; accord → publication selon les seuils |
| 8. Cloud absent | PASS — Ask répond, la file de review attend intacte |
| 9. Adapter cloud mocké | PASS — approbation, rejet, sujet sensible gardé pour l'humain, aucune clé en base ni dans les erreurs, note manuelle jamais envoyée |
| 10. Recherche sémantique | PASS — paraphrase sans mot commun retrouvée ; questions hors sujet → rien |
| 11. Embeddings indisponibles | PASS — modèle absent, désactivé, index vide, backend en panne ou trop lent → lexical |
| 12. Non-régression V1 | PASS — 8 scénarios V1 (RTK rang 1, LinkedIn, TDAH prudent, aucune invention, hors Vault non capté, Mentor, Notion idempotent, `inbox.bat` sans base parasite), Calendar Planning, chaînage complet |

**Sur copie du Vault réel (64 sources) :**
- index complet 34 s (15 s machine au repos), incrémental < 0,1 s ;
- seuil calibré z = 2,906 ;
- tokens → RTK rang 1 ; LinkedIn 4/4 ; TDAH prudent ; immobilier → villa SPV ;
- 4 questions hors Vault → aucune source ;
- 2 paraphrases retrouvées ;
- « football » : 3 faux positifs lexicaux V1 écartés par le veto sémantique.

**Mesures de performance :**

| Opération | Durée |
|---|---|
| Recherche lexicale, en processus | 0,2–0,4 s |
| Recherche hybride, en processus | 0,2–0,5 s (1,6 s au premier appel : chargement ONNX) |
| Bridge `--ask` de bout en bout, sans LLM | 3,5 s |
| Bridge `--ask`, avec synthèse Qwen | 25,7 s |
| Outil Jarvis : TDAH / aucune source / Ollama coupé | 2,3 s / 2,1 s / 4,3 s |
| Mentor, cas borderline (2 analyses Agency) | ~90 s par source (batch) |
| Claims qwen3:1.7b | 107–194 s par source (batch) — ~2 h pour 64 sources |

**Source 64 (RTK), comportement V2 :** borderline (score 0,612, à 0,012 du seuil). Analyse 1 :
opportunity 0,65/0,75. Second avis indépendant : novelty 0,70/0,60. Désaccord de classification :
`review_required` (`agency_disagreement:classification_disagreement`), identique sur 2 passages.
C'est voulu : la V1 publiait cette source sur la seule analyse « à froid », non reproductible.
L'insight existant de la V1 n'est pas modifié (la source n'est pas re-traitée).

## Limites connues

- qwen3:1.7b extrait lentement et parfois approximativement (ex. « laisser l'IA l'installer » → « offre d'installer l'IA »). Le validateur bloque les chiffres, citations et objets non ancrés, mais pas toutes les erreurs de sens.
- Sur 24 claims réels extraits (5 sources), 0 convergence/contradiction : normal, les sujets sont distincts. Le moteur ne produira des évaluations qu'une fois le Vault largement extrait.
- Le sémantique pur est volontairement conservateur (≈ 2 paraphrases sur 10 admises seules) ; il améliore surtout le classement et écarte des faux positifs lexicaux.
- « prix de l'essence » renvoie encore une source qui parle de prix de serveurs : correspondance lexicale partielle réelle, pas une invention.
- Aucun appel cloud réel n'a été testé (aucune clé sur cette machine) : l'adapter Claude est testé par mocks uniquement.
