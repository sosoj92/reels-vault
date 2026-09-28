import argparse
import datetime as dt
import json
import math
import os
import re
import sqlite3
import urllib.request
from pathlib import Path
import mentor_agency
import mentor_consensus
import mentor_decision
import mentor_review
import v2_guard
from collections import Counter


# Chemin absolu : le worker est lance par inbox.bat depuis n'importe quel
# repertoire courant ; un chemin relatif creerait une base vide ailleurs.
ROOT = Path(__file__).resolve().parent
DB = os.environ.get("REELS_VAULT_DB") or str(ROOT / "vault.db")

STOPWORDS = {
    "the","and","for","that","this","with","from","you","your","are","was","were",
    "have","has","had","will","can","not","but","they","their","what","when","how",
    "une","des","les","dans","pour","avec","sur","pas","que","qui","est","sont",
    "être","avoir","plus","tout","comme","nous","vous","ils","elles","ses","aux",
    "du","de","la","le","un","en","au","ça","ce","ces","son","sa","se"
}

OPPORTUNITY_WORDS = {
    "api","automation","automatisation","automate","workflow","agency","agence",
    "prospection","recruitment","recrutement","client","clients","crm","sales",
    "business","tool","tools","outil","outils","app","application","saas",
    "rebuild","agent","agents","ai","ia","nocode","no-code","lead","leads"
}


QWEN_MODEL = "qwen3:1.7b"
OLLAMA_BASE = os.environ.get("VAULT_OLLAMA_URL", "http://localhost:11434").rstrip("/")
QWEN_URL = f"{OLLAMA_BASE}/api/chat"


def ollama_available(timeout=3):
    """Ollama repond et le modele local est installe."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=timeout) as resp:
            models = json.load(resp).get("models", [])
    except Exception:
        return False
    names = {m.get("name") for m in models} | {m.get("model") for m in models}
    return QWEN_MODEL in names

QWEN_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": [
                "opportunity",
                "convergence",
                "novelty",
                "low_signal"
            ]
        },
        "relevance": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
        },
        "needs_agency": {
            "type": "boolean"
        }
    },
    "required": [
        "classification",
        "relevance",
        "confidence",
        "needs_agency"
    ]
}


def qwen_analyze(text, heuristic_outcome, similarity, entities):
    entity_names = [
        e["name"]
        for e in entities
    ]

    prompt = f"""
Analyse cette nouvelle source pour le Life Intelligence System.

Classification d?terministe pr?alable : {heuristic_outcome}
Similarit? maximale avec une source existante : {similarity:.3f}
Entit?s d?j? connues d?tect?es : {entity_names}

R?gles :
- opportunity = information potentiellement actionnable
- convergence = confirme ou rejoint significativement une information existante
- novelty = information r?ellement nouvelle mais pas imm?diatement actionnable
- low_signal = peu utile, r?p?titif ou insuffisant
- needs_agency = true si le cas n?cessite comparaison approfondie,
  raisonnement sp?cialis?, strat?gie ou contexte suppl?mentaire

SOURCE :
{text[:6000]}
""".strip()

    payload = {
        "model": QWEN_MODEL,
        "stream": False,
        "think": False,
        "format": QWEN_SCHEMA,
        "options": {
            "temperature": 0,
            "num_predict": 120
        },
        "messages": [
            {
                "role": "system",
                "content": (
                    "Tu es la couche locale rapide du Mentor Engine. "
                    "N'invente pas d'information et respecte strictement "
                    "le sch?ma JSON fourni."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
    }

    try:
        req = urllib.request.Request(
            QWEN_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json"
            },
        )

        with urllib.request.urlopen(
            req,
            timeout=60
        ) as response:
            raw = json.load(response)

        result = json.loads(
            raw["message"]["content"]
        )

        return {
            "classification": result["classification"],
            "relevance": float(result["relevance"]),
            "confidence": float(result["confidence"]),
            "needs_agency": bool(result["needs_agency"]),
        }

    except Exception:
        return None


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def connect():
    if not Path(DB).exists():
        raise SystemExit(f"Base Vault introuvable : {DB}")
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intelligence_mentor_processing (
            source_id INTEGER PRIMARY KEY,
            content_id INTEGER,
            outcome TEXT NOT NULL,
            relevance_score REAL NOT NULL,
            max_similarity REAL NOT NULL DEFAULT 0,
            matched_source_id INTEGER,
            entity_ids TEXT NOT NULL DEFAULT '[]',
            processed_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS intelligence_mentor_insights_local (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL UNIQUE,
            insight_type TEXT NOT NULL,
            observation TEXT NOT NULL,
            context TEXT,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            why_now TEXT,
            relevance_score REAL NOT NULL,
            suggestion TEXT,
            status TEXT NOT NULL DEFAULT 'proposed',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    processing_cols = {
        r[1]
        for r in conn.execute(
            "PRAGMA table_info(intelligence_mentor_processing)"
        )
    }

    for name, sql_type in (
        ("qwen_classification", "TEXT"),
        ("qwen_relevance", "REAL"),
        ("qwen_confidence", "REAL"),
        ("needs_agency", "INTEGER"),
        ("analysis_model", "TEXT"),
    ):
        if name not in processing_cols:
            conn.execute(
                f"ALTER TABLE intelligence_mentor_processing "
                f"ADD COLUMN {name} {sql_type}"
            )

    insight_cols = {
        r[1]
        for r in conn.execute(
            "PRAGMA table_info(intelligence_mentor_insights_local)"
        )
    }

    for name, sql_type in (
        ("analysis_model", "TEXT"),
        ("needs_agency", "INTEGER"),
    ):
        if name not in insight_cols:
            conn.execute(
                f"ALTER TABLE intelligence_mentor_insights_local "
                f"ADD COLUMN {name} {sql_type}"
            )

    conn.commit()


def tokenize(text):
    words = re.findall(
        r"[a-zA-ZÀ-ÿ0-9][a-zA-ZÀ-ÿ0-9_-]{2,}",
        (text or "").lower()
    )

    return [
        w for w in words
        if w not in STOPWORDS
        and not w.isdigit()
    ]


def text_for(row):
    parts = [
        row["title"],
        row["description_original"],
        row["description_ai"],
        row["description_manual"],
        row["transcript"],
    ]

    return "\n".join(
        str(x).strip()
        for x in parts
        if x and str(x).strip()
    )


def cosine(tokens_a, tokens_b):
    if not tokens_a or not tokens_b:
        return 0.0

    a = Counter(tokens_a)
    b = Counter(tokens_b)

    common = set(a) & set(b)

    dot = sum(a[k] * b[k] for k in common)

    norm_a = math.sqrt(
        sum(v * v for v in a.values())
    )

    norm_b = math.sqrt(
        sum(v * v for v in b.values())
    )

    if not norm_a or not norm_b:
        return 0.0

    return dot / (norm_a * norm_b)


def known_entities(conn, text):
    lower = text.lower()

    rows = conn.execute("""
        SELECT id, entity_type, name, canonical_key
        FROM intelligence_entities
        ORDER BY id
    """).fetchall()

    found = []

    for row in rows:
        name = (row["name"] or "").strip()

        if len(name) < 4:
            continue

        if name.lower() in lower:
            found.append({
                "id": row["id"],
                "type": row["entity_type"],
                "name": name,
                "canonical_key": row["canonical_key"],
            })

    return found


def closest_source(conn, source_id, content_id, tokens):
    rows = conn.execute("""
        SELECT
            s.id AS source_id,
            c.id AS content_id,
            c.title,
            c.description_original,
            c.description_ai,
            c.description_manual,
            c.transcript
        FROM intelligence_sources s
        JOIN contents c
          ON c.id = s.content_id
        WHERE s.id != ?
          AND s.id < ?
        ORDER BY s.id DESC
        LIMIT 60
    """, (
        source_id,
        source_id,
    )).fetchall()

    best_score = 0.0
    best = None

    for row in rows:
        other_tokens = tokenize(text_for(row))

        score = cosine(
            tokens,
            other_tokens
        )

        if score > best_score:
            best_score = score
            best = row

    return best_score, best


def classify(conn, row, v2=True):
    text = text_for(row)
    tokens = tokenize(text)
    token_set = set(tokens)

    entities = known_entities(
        conn,
        text
    )

    similarity, matched = closest_source(
        conn,
        row["source_id"],
        row["content_id"],
        tokens,
    )

    opportunity = bool(
        token_set & OPPORTUNITY_WORDS
    )

    convergence = (
        similarity >= 0.32
    )

    novelty = (
        len(tokens) >= 25
        and similarity <= 0.15
    )

    if opportunity and convergence:
        outcome = "opportunity_convergence"
    elif opportunity:
        outcome = "opportunity"
    elif convergence:
        outcome = "convergence"
    elif novelty:
        outcome = "novelty"
    else:
        outcome = "low_signal"

    score = 0.10
    score += min(len(tokens) / 100, 0.20)

    if entities:
        score += 0.20

    if opportunity:
        score += 0.25

    if convergence:
        score += 0.20

    if novelty:
        score += 0.15

    score = min(round(score, 3), 1.0)

    heuristic_outcome = outcome
    heuristic_score = score

    qwen = qwen_analyze(
        text,
        heuristic_outcome,
        similarity,
        entities,
    )

    needs_agency = False
    analysis_model = "deterministic-v1"

    if qwen:
        qclass = qwen["classification"]
        qrel = qwen["relevance"]
        qconf = qwen["confidence"]

        analysis_model = QWEN_MODEL

        if qconf >= 0.55:
            if (
                opportunity
                and (
                    qclass == "convergence"
                    or (
                        qclass == "opportunity"
                        and convergence
                    )
                )
            ):
                outcome = "opportunity_convergence"
            else:
                outcome = qclass

            score = round(
                min(
                    1.0,
                    0.45 * score
                    + 0.55 * qrel
                ),
                3
            )

        needs_agency = (
            qwen["needs_agency"]
            or qconf < 0.65
        )

    agency = None
    needs_cloud = False

    if needs_agency:
        agency = mentor_agency.analyze(
            text,
            qwen_result=qwen,
            heuristic_outcome=heuristic_outcome,
            similarity=similarity,
        )

        if agency:
            agency_conf = float(
                agency["confidence"]
            )

            agency_rel = float(
                agency["relevance"]
            )

            if agency_conf >= 0.55:
                outcome = agency["classification"]

                score = round(
                    min(
                        1.0,
                        0.35 * score
                        + 0.65 * agency_rel
                    ),
                    3
                )

            needs_cloud = (
                bool(agency["needs_cloud"])
                or agency_conf < 0.65
            )

            analysis_model = (
                f"{QWEN_MODEL}+agency"
            )

        else:
            needs_cloud = True

    decision = mentor_decision.finalize(
        heuristic_score=heuristic_score,
        current_outcome=outcome,
        qwen=qwen,
        agency=agency,
        needs_agency=needs_agency,
    )

    # V2 : cas limite -> seconde analyse Agency independante, publication
    # seulement si les deux concordent (voir mentor_consensus pour les regles).
    # Sur le vrai Vault non encore valide (verrou V2) : comportement V1,
    # une seule analyse Agency.
    borderline = (
        mentor_consensus.borderline_reasons(qwen, agency, decision) if v2 else []
    )
    consensus = None
    review_required = False
    review_reason = None
    if borderline:
        agency_2 = mentor_agency.analyze(
            text,
            qwen_result=qwen,
            heuristic_outcome=heuristic_outcome,
            similarity=similarity,
            variant=2,
        )
        decision = mentor_consensus.build_consensus(
            heuristic_score=heuristic_score,
            current_outcome=outcome,
            qwen=qwen,
            run1=agency,
            run2=agency_2,
            needs_agency=needs_agency,
        )
        consensus = {**decision["consensus"], "borderline_reasons": borderline}
        review_required = decision["review_required"]
        review_reason = decision["review_reason"]
        analysis_model = f"{QWEN_MODEL}+agency x2"

    outcome = decision["outcome"]
    score = decision["relevance_score"]
    needs_cloud = decision["needs_cloud"]
    if needs_cloud and not review_reason:
        review_reason = "cloud_escalation_required"

    return {
        "consensus": consensus,
        "review_required": bool(review_required or needs_cloud),
        "review_reason": review_reason,
        "outcome": outcome,
        "score": score,
        "tokens": len(tokens),
        "entities": entities,
        "similarity": round(similarity, 3),
        "matched": matched,
        "text": text,
        "heuristic_outcome": heuristic_outcome,
        "qwen": qwen,
        "needs_agency": needs_agency,
        "agency": agency,
        "agency_agents": (
            agency.get("agents", [])
            if agency
            else []
        ),
        "needs_cloud": needs_cloud,
        "final_confidence": decision["final_confidence"],
        "publishable": decision["publishable"],
        "anti_noise_reasons": decision["anti_noise_reasons"],
        "analysis_model": analysis_model,
    }


def build_insight(row, analysis):
    title = (
        row["title"]
        or f"Source {row['source_id']}"
    )

    outcome = analysis["outcome"]

    matched = analysis["matched"]

    entity_names = [
        e["name"]
        for e in analysis["entities"]
    ]

    if outcome == "opportunity_convergence":
        observation = (
            f"La source « {title} » contient un signal exploitable "
            "et converge avec une information déjà présente dans le Vault."
        )
        suggestion = (
            "Comparer les deux sources et vérifier si cette convergence "
            "justifie un test, une décision ou une action."
        )

    elif outcome == "opportunity":
        observation = (
            f"La source « {title} » contient un signal potentiellement "
            "actionnable pour les activités ou systèmes suivis."
        )
        suggestion = (
            "Évaluer rapidement si ce signal mérite un test concret "
            "ou un rattachement à une activité active."
        )

    elif outcome == "convergence":
        observation = (
            f"La source « {title} » ressemble fortement à une information "
            "déjà présente dans le Vault."
        )
        suggestion = (
            "Comparer les deux sources pour distinguer répétition, "
            "confirmation ou information réellement nouvelle."
        )

    elif outcome == "novelty":
        observation = (
            f"La source « {title} » apporte un sujet relativement nouveau "
            "par rapport aux sources récentes du Vault."
        )
        suggestion = (
            "Conserver ce signal et surveiller s'il se rattache à "
            "d'autres sources ou contextes ultérieurement."
        )

    else:
        observation = (
            f"La source « {title} » ne dépasse pas le seuil de pertinence."
        )
        suggestion = None

    if entity_names:
        context = (
            "Rattachement détecté : "
            + ", ".join(entity_names)
        )
    else:
        context = (
            "Aucun rattachement canonique direct détecté."
        )

    evidence = {
        "source_id": row["source_id"],
        "content_id": row["content_id"],
        "source_url": row["source_url"],
        "title": row["title"],
        "author": row["author"],
        "matched_source_id": (
            matched["source_id"]
            if matched
            else None
        ),
        "similarity": analysis["similarity"],
        "entity_ids": [
            e["id"]
            for e in analysis["entities"]
        ],
    }

    why_now = (
        "Nouvelle source ingérée et non encore analysée "
        "par le Mentor Worker."
    )

    return {
        "observation": observation,
        "context": context,
        "evidence": evidence,
        "why_now": why_now,
        "suggestion": suggestion,
    }


def pending_sources(conn, limit):
    return conn.execute("""
        SELECT
            s.id AS source_id,
            s.source_url,
            s.platform,
            s.title AS source_title,
            s.author,
            s.content_id,
            s.created_at,

            c.title,
            c.description_original,
            c.description_ai,
            c.description_manual,
            c.transcript

        FROM intelligence_sources s

        JOIN contents c
          ON c.id = s.content_id

        LEFT JOIN intelligence_mentor_processing p
          ON p.source_id = s.id

        WHERE p.source_id IS NULL

        ORDER BY s.id DESC

        LIMIT ?
    """, (limit,)).fetchall()


def source_by_id(conn, source_id):
    """Une source precise, deja traitee ou non (re-analyse a blanc)."""
    return conn.execute("""
        SELECT
            s.id AS source_id,
            s.source_url,
            s.platform,
            s.title AS source_title,
            s.author,
            s.content_id,
            s.created_at,
            c.title,
            c.description_original,
            c.description_ai,
            c.description_manual,
            c.transcript
        FROM intelligence_sources s
        JOIN contents c
          ON c.id = s.content_id
        WHERE s.id = ?
    """, (source_id,)).fetchall()


def run(limit=5, confirm=False, threshold=0.55, source_id=None):
    if confirm and source_id is None and not ollama_available():
        # Sans Ollama, chaque source serait figee en "heuristique seule" et
        # jamais re-analysee. On reporte : elles restent en attente et seront
        # traitees au prochain passage d'inbox.bat.
        print(json.dumps({
            "writes_performed": False,
            "deferred": True,
            "reason": f"Ollama/{QWEN_MODEL} indisponible sur {OLLAMA_BASE} : analyse reportee",
        }, ensure_ascii=False, indent=2))
        return

    conn = connect()

    try:
        # Une analyse a blanc ne doit pas modifier le schema d'une base
        # existante : les migrations n'ont lieu qu'en mode --confirm (ou sur
        # une base neuve, sans table de traitement).
        v2 = v2_guard.v2_writes_allowed(conn)
        fresh = not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='intelligence_mentor_processing'"
        ).fetchone()
        if (confirm and source_id is None) or fresh:
            ensure_schema(conn)
            mentor_review.ensure_schema(conn, v2=v2)

        if source_id is not None:
            # Re-analyse a blanc : jamais d'ecriture, pour ne pas ecraser un
            # traitement existant ni une correction utilisateur.
            confirm = False
            rows = source_by_id(conn, source_id)
        else:
            rows = pending_sources(
                conn,
                limit
            )

        output = []

        for row in rows:
            analysis = classify(
                conn,
                row,
                v2=v2,
            )

            create_insight = (
                analysis["publishable"]
                and analysis["score"] >= threshold
            )

            item = {
                "source_id": row["source_id"],
                "title": (
                    row["title"]
                    or row["source_title"]
                ),
                "outcome": analysis["outcome"],
                "relevance_score": analysis["score"],
                "analysis_model": analysis["analysis_model"],
                "qwen": analysis["qwen"],
                "needs_agency": analysis["needs_agency"],
                "agency": analysis["agency"],
                "agency_agents": analysis["agency_agents"],
                "needs_cloud": analysis["needs_cloud"],
                "final_confidence": analysis["final_confidence"],
                "publishable": analysis["publishable"],
                "anti_noise_reasons": analysis["anti_noise_reasons"],
                "max_similarity": analysis["similarity"],
                "matched_source_id": (
                    analysis["matched"]["source_id"]
                    if analysis["matched"]
                    else None
                ),
                "entities": [
                    e["name"]
                    for e in analysis["entities"]
                ],
                "would_create_insight": create_insight,
                "review_required": analysis["review_required"],
                "review_reason": analysis["review_reason"],
                "consensus": analysis["consensus"],
            }

            output.append(item)

            if not confirm:
                continue

            if analysis["review_required"]:
                mentor_review.enqueue(
                    conn,
                    row,
                    analysis,
                    reason=analysis.get("review_reason") or "cloud_escalation_required",
                )

            if analysis.get("consensus"):
                mentor_review.record_consensus(conn, row, analysis)

            now = utcnow()
            qwen = analysis["qwen"] or {}

            conn.execute("""
                INSERT INTO intelligence_mentor_processing(
                    source_id,
                    content_id,
                    outcome,
                    relevance_score,
                    max_similarity,
                    matched_source_id,
                    entity_ids,
                    processed_at,
                    qwen_classification,
                    qwen_relevance,
                    qwen_confidence,
                    needs_agency,
                    analysis_model
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["source_id"],
                row["content_id"],
                analysis["outcome"],
                analysis["score"],
                analysis["similarity"],
                (
                    analysis["matched"]["source_id"]
                    if analysis["matched"]
                    else None
                ),
                json.dumps(
                    [
                        e["id"]
                        for e in analysis["entities"]
                    ]
                ),
                now,
                qwen.get("classification"),
                qwen.get("relevance"),
                qwen.get("confidence"),
                int(analysis["needs_agency"]),
                analysis["analysis_model"],
            ))

            if create_insight:
                insight = build_insight(
                    row,
                    analysis
                )

                conn.execute("""
                    INSERT OR IGNORE INTO intelligence_mentor_insights_local(
                        source_id,
                        insight_type,
                        observation,
                        context,
                        evidence_json,
                        why_now,
                        relevance_score,
                        suggestion,
                        status,
                        created_at,
                        updated_at,
                        analysis_model,
                        needs_agency
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?)
                """, (
                    row["source_id"],
                    analysis["outcome"],
                    insight["observation"],
                    insight["context"],
                    json.dumps(
                        insight["evidence"],
                        ensure_ascii=False
                    ),
                    insight["why_now"],
                    analysis["score"],
                    insight["suggestion"],
                    now,
                    now,
                    analysis["analysis_model"],
                    int(analysis["needs_agency"]),
                ))

        if confirm:
            conn.commit()

        print(json.dumps({
            "writes_performed": confirm,
            "threshold": threshold,
            "processed_candidates": len(output),
            "items": output,
        }, ensure_ascii=False, indent=2))

    finally:
        conn.close()


REASSESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_mentor_v2_reassessment (
    source_id          INTEGER PRIMARY KEY,
    content_id         INTEGER,
    v1_outcome         TEXT,
    v1_relevance       REAL,
    had_insight        INTEGER NOT NULL DEFAULT 0,
    borderline         INTEGER NOT NULL DEFAULT 0,
    borderline_reasons TEXT NOT NULL DEFAULT '[]',
    v2_outcome         TEXT,
    v2_relevance       REAL,
    v2_confidence      REAL,
    decision           TEXT NOT NULL,
    review_reason      TEXT,
    analysis_model     TEXT,
    reassessed_at      TEXT NOT NULL
)
"""


def reassess(source_ids=None, limit=None, confirm=False, force=False):
    """
    Deploiement V2 : re-evalue en V2 des sources DEJA traitees en V1.

    - cas borderline : double Agency + trace de consensus ;
    - si la V2 conclut a la review : ajout a la file de review avec une
      raison "v2_backfill:..." ;
    - le traitement V1 (intelligence_mentor_processing) et les insights
      existants ne sont JAMAIS modifies : rien ne change dans Notion ;
    - chaque source re-evaluee est tracee (intelligence_mentor_v2_reassessment),
      ce qui rend l'operation idempotente et reprenable.
    """
    if confirm and not ollama_available():
        print(json.dumps({"writes_performed": False, "deferred": True,
                          "reason": "Ollama indisponible"}, ensure_ascii=False))
        return None
    conn = connect()
    try:
        if confirm:
            v2_guard.require_v2_writes(conn, "re-evaluation V2 du Mentor")
            ensure_schema(conn)
            mentor_review.ensure_schema(conn, v2=True)
            conn.execute(REASSESS_SCHEMA)
            conn.commit()
        has_trace = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='intelligence_mentor_v2_reassessment'"
        ).fetchone()
        rows = conn.execute("""
            SELECT
                s.id AS source_id, s.source_url, s.platform, s.title AS source_title,
                s.author, s.content_id, s.created_at, c.title, c.description_original,
                c.description_ai, c.description_manual, c.transcript,
                p.outcome AS v1_outcome, p.relevance_score AS v1_relevance,
                (SELECT COUNT(*) FROM intelligence_mentor_insights_local i
                  WHERE i.source_id = s.id) AS had_insight
            FROM intelligence_sources s
            JOIN contents c ON c.id = s.content_id
            JOIN intelligence_mentor_processing p ON p.source_id = s.id
            ORDER BY s.id
        """).fetchall()
        done = set()
        if has_trace and not force:
            done = {r[0] for r in conn.execute(
                "SELECT source_id FROM intelligence_mentor_v2_reassessment")}
        selected = [
            r for r in rows
            if (source_ids is None or r["source_id"] in source_ids)
            and r["source_id"] not in done
        ]
        if limit is not None:
            selected = selected[:limit]

        items = []
        for row in selected:
            analysis = classify(conn, row, v2=True)
            consensus = analysis.get("consensus")
            decision = (
                "review" if analysis["review_required"]
                else "publishable" if analysis["publishable"] else "not_publishable"
            )
            item = {
                "source_id": row["source_id"],
                "title": row["title"] or row["source_title"],
                "v1_outcome": row["v1_outcome"],
                "v1_relevance": row["v1_relevance"],
                "had_insight": bool(row["had_insight"]),
                "borderline": bool(consensus),
                "borderline_reasons": (consensus or {}).get("borderline_reasons", []),
                "v2_outcome": analysis["outcome"],
                "v2_relevance": analysis["score"],
                "v2_confidence": analysis["final_confidence"],
                "decision": decision,
                "review_reason": analysis["review_reason"],
                "analysis_model": analysis["analysis_model"],
                "agency_run_1": (consensus or {}).get("agency_run_1"),
                "agency_run_2": (consensus or {}).get("agency_run_2"),
            }
            items.append(item)
            if not confirm:
                continue
            if consensus:
                mentor_review.record_consensus(conn, row, analysis)
            if analysis["review_required"]:
                mentor_review.enqueue(
                    conn, row, analysis,
                    reason="v2_backfill:" + (analysis["review_reason"] or "review"),
                )
            conn.execute(
                """
                INSERT INTO intelligence_mentor_v2_reassessment(
                    source_id, content_id, v1_outcome, v1_relevance, had_insight,
                    borderline, borderline_reasons, v2_outcome, v2_relevance,
                    v2_confidence, decision, review_reason, analysis_model, reassessed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    borderline = excluded.borderline,
                    borderline_reasons = excluded.borderline_reasons,
                    v2_outcome = excluded.v2_outcome,
                    v2_relevance = excluded.v2_relevance,
                    v2_confidence = excluded.v2_confidence,
                    decision = excluded.decision,
                    review_reason = excluded.review_reason,
                    analysis_model = excluded.analysis_model,
                    reassessed_at = excluded.reassessed_at
                """,
                (row["source_id"], row["content_id"], row["v1_outcome"], row["v1_relevance"],
                 int(bool(row["had_insight"])), int(bool(consensus)),
                 json.dumps(item["borderline_reasons"]), analysis["outcome"],
                 analysis["score"], analysis["final_confidence"], decision,
                 analysis["review_reason"], analysis["analysis_model"], utcnow()),
            )
            # Commit par source : reprenable si interrompu.
            conn.commit()
        result = {"writes_performed": confirm, "reassessed": len(items), "items": items}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="nombre de sources (defaut : 5 ; avec --reassess : toutes)",
    )

    parser.add_argument(
        "--threshold",
        type=float,
        default=0.55
    )

    parser.add_argument(
        "--confirm",
        action="store_true"
    )

    parser.add_argument(
        "--source-id",
        type=int,
        help="re-analyse a blanc d'une source precise (aucune ecriture)",
    )

    parser.add_argument(
        "--reassess",
        action="store_true",
        help="deploiement V2 : re-evaluer des sources deja traitees en V1",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="avec --reassess : re-evaluer aussi les sources deja re-evaluees",
    )

    args = parser.parse_args()

    if args.reassess:
        reassess(
            source_ids=[args.source_id] if args.source_id is not None else None,
            limit=args.limit,
            confirm=args.confirm,
            force=args.force,
        )
        raise SystemExit(0)

    if args.source_id is not None and args.confirm:
        parser.error("--source-id est une analyse a blanc : --confirm interdit")

    run(
        limit=args.limit if args.limit is not None else 5,
        confirm=args.confirm,
        threshold=args.threshold,
        source_id=args.source_id,
    )
