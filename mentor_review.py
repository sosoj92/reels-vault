"""
mentor_review.py - File de review du Mentor (V1) + consensus et revue cloud (V2).

- enqueue() : une source dont l'analyse n'est pas assez fiable pour etre
  publiee passe en review_required, avec sa raison exacte
  (cloud_escalation_required, agency_disagreement:..., ...).
- record_consensus() : trace agency_run_1 / agency_run_2 / consensus.
- process_queue() : revue optionnelle par un provider cloud (vault_cloud).
  Sans cle : rien ne change, la file attend. Avec cle : le provider peut
  approuver (insight cree), rejeter, ou laisser a l'humain. Les sujets
  sensibles (sante / finance / droit) restent TOUJOURS a l'humain.
  Seuls des extraits publics de la source sont envoyes ; aucune cle n'est
  stockee ni journalisee.
"""

import json
from datetime import datetime, timezone

import v2_guard

CLOUD_MIN_RELEVANCE = 0.60
CLOUD_MIN_CONFIDENCE = 0.70
ALLOWED_CLASSIFICATIONS = ("opportunity", "convergence", "novelty", "low_signal")
SENSITIVE_ROUTES = {"research_evidence", "finance", "legal"}


def now():
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(conn, v2=True):
    """Table V1 toujours ; colonnes et table V2 seulement si v2 (verrou)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intelligence_mentor_review_queue (
            source_id INTEGER PRIMARY KEY,
            content_id INTEGER,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'review_required',
            analysis_model TEXT,
            relevance_score REAL,
            confidence REAL,
            agency_agents TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    if not v2:
        conn.commit()
        return
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(intelligence_mentor_review_queue)")
    }
    for name, sql_type in (
        ("cloud_provider", "TEXT"),
        ("cloud_model", "TEXT"),
        ("cloud_decision", "TEXT"),
        ("cloud_reviewed_at", "TEXT"),
    ):
        if name not in columns:
            conn.execute(
                f"ALTER TABLE intelligence_mentor_review_queue ADD COLUMN {name} {sql_type}"
            )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intelligence_mentor_consensus (
            source_id INTEGER PRIMARY KEY,
            content_id INTEGER,
            borderline_reasons TEXT NOT NULL DEFAULT '[]',
            agency_run_1 TEXT,
            agency_run_2 TEXT,
            agreement TEXT NOT NULL DEFAULT '{}',
            consensus TEXT NOT NULL DEFAULT '{}',
            decision TEXT NOT NULL CHECK (decision IN ('publish', 'review')),
            review_reason TEXT,
            analysis_model TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()


def enqueue(conn, row, analysis, reason="cloud_escalation_required"):
    timestamp = now()

    conn.execute("""
        INSERT INTO intelligence_mentor_review_queue(
            source_id,
            content_id,
            reason,
            status,
            analysis_model,
            relevance_score,
            confidence,
            agency_agents,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, 'review_required', ?, ?, ?, ?, ?, ?)

        ON CONFLICT(source_id) DO UPDATE SET
            reason=excluded.reason,
            status='review_required',
            analysis_model=excluded.analysis_model,
            relevance_score=excluded.relevance_score,
            confidence=excluded.confidence,
            agency_agents=excluded.agency_agents,
            updated_at=excluded.updated_at
    """, (
        row["source_id"],
        row["content_id"],
        reason,
        analysis["analysis_model"],
        analysis["score"],
        analysis["final_confidence"],
        json.dumps(
            analysis["agency_agents"],
            ensure_ascii=False
        ),
        timestamp,
        timestamp,
    ))


def record_consensus(conn, row, analysis):
    v2_guard.require_v2_writes(conn, "trace de consensus du Mentor")
    consensus = analysis.get("consensus") or {}
    timestamp = now()
    conn.execute(
        """
        INSERT INTO intelligence_mentor_consensus(
            source_id, content_id, borderline_reasons, agency_run_1, agency_run_2,
            agreement, consensus, decision, review_reason, analysis_model,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            borderline_reasons = excluded.borderline_reasons,
            agency_run_1 = excluded.agency_run_1,
            agency_run_2 = excluded.agency_run_2,
            agreement = excluded.agreement,
            consensus = excluded.consensus,
            decision = excluded.decision,
            review_reason = excluded.review_reason,
            analysis_model = excluded.analysis_model,
            updated_at = excluded.updated_at
        """,
        (
            row["source_id"],
            row["content_id"],
            json.dumps(consensus.get("borderline_reasons", []), ensure_ascii=False),
            json.dumps(consensus.get("agency_run_1"), ensure_ascii=False),
            json.dumps(consensus.get("agency_run_2"), ensure_ascii=False),
            json.dumps(consensus.get("agreement", {}), ensure_ascii=False),
            json.dumps(consensus, ensure_ascii=False),
            consensus.get("decision", "review"),
            analysis.get("review_reason"),
            analysis.get("analysis_model"),
            timestamp,
            timestamp,
        ),
    )


# ---------------------------------------------------------------------------
# Revue cloud optionnelle
# ---------------------------------------------------------------------------

REVIEW_SYSTEM = (
    "Tu es un relecteur prudent du Life Intelligence Mentor. Tu juges si une "
    "source sauvegardee merite un insight actionnable. Tu ne dois utiliser que "
    "le texte fourni. Reponds par un unique objet JSON : "
    '{"decision": "publish" | "reject" | "needs_human", '
    '"classification": "opportunity" | "convergence" | "novelty" | "low_signal", '
    '"relevance": 0..1, "confidence": 0..1, "rationale": "une phrase"}. '
    "En cas de doute, reponds needs_human."
)


def _public_excerpt(conn, source_id, max_chars=2500):
    """Seulement des champs publics de la source (jamais les notes manuelles)."""
    row = conn.execute(
        """
        SELECT s.title, s.platform, c.description_original, c.transcript, c.category
          FROM intelligence_sources s
          LEFT JOIN contents c ON c.id = s.content_id
         WHERE s.id = ?
        """,
        (source_id,),
    ).fetchone()
    if not row:
        return None
    title, platform, description, transcript, category = tuple(row)
    text = " ".join(str(transcript or "").split())[:max_chars]
    return {
        "title": title,
        "platform": platform,
        "category": category,
        "description": " ".join(str(description or "").split())[:600],
        "transcript_excerpt": text,
    }


def _parse_decision(text):
    text = str(text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _sensitive(conn, source_id, excerpt):
    import vault_claims

    consensus = conn.execute(
        "SELECT consensus FROM intelligence_mentor_consensus WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    blob = json.dumps(excerpt, ensure_ascii=False)
    if consensus:
        blob += consensus[0] or ""
    return bool(vault_claims.detect_sensitivity(blob, excerpt.get("category")))


def process_queue(conn, provider=None, *, limit=5, dry_run=False):
    """
    Traite les items review_required avec un provider cloud.
    provider=None -> vault_cloud.get_provider() ; si aucun : rien n'est modifie.
    """
    import vault_cloud

    if not dry_run:
        v2_guard.require_v2_writes(conn, "revue cloud de la file de review")
    ensure_schema(conn, v2=v2_guard.v2_writes_allowed(conn))
    provider = provider if provider is not None else vault_cloud.get_provider()
    items = conn.execute(
        """
        SELECT source_id, content_id, reason, relevance_score, confidence, analysis_model
          FROM intelligence_mentor_review_queue
         WHERE status = 'review_required'
         ORDER BY updated_at
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if provider is None:
        return {"status": "no_cloud_provider", "pending": len(items), "processed": []}

    processed = []
    for item in items:
        source_id, content_id, reason, relevance, confidence, model = tuple(item)
        excerpt = _public_excerpt(conn, source_id)
        if excerpt is None:
            continue
        user = json.dumps(
            {
                "source": excerpt,
                "motif_de_review": reason,
                "analyse_locale": {"relevance": relevance, "confidence": confidence,
                                   "model": model},
            },
            ensure_ascii=False,
        )
        try:
            response = provider.complete(REVIEW_SYSTEM, user, max_tokens=1024, timeout=90)
        except vault_cloud.CloudError as exc:
            processed.append({"source_id": source_id, "status": "error",
                              "error": vault_cloud.redact(str(exc))[:200]})
            continue

        data = _parse_decision(response.text)
        status, outcome = "needs_human", None
        if data:
            decision = str(data.get("decision") or "").lower()
            classification = str(data.get("classification") or "").lower()
            try:
                c_rel = float(data.get("relevance"))
                c_conf = float(data.get("confidence"))
            except (TypeError, ValueError):
                c_rel = c_conf = 0.0
            sensitive = _sensitive(conn, source_id, excerpt)
            if decision == "reject":
                status = "cloud_rejected"
            elif (
                decision == "publish"
                and classification in ALLOWED_CLASSIFICATIONS
                and classification != "low_signal"
                and c_rel >= CLOUD_MIN_RELEVANCE
                and c_conf >= CLOUD_MIN_CONFIDENCE
                and not sensitive
            ):
                status = "cloud_approved"
                outcome = {"classification": classification, "relevance": round(c_rel, 3),
                           "confidence": round(c_conf, 3)}
            data = {
                "decision": decision,
                "classification": classification,
                "relevance": c_rel,
                "confidence": c_conf,
                "rationale": str(data.get("rationale") or "")[:500],
                "sensitive_kept_for_human": sensitive and decision == "publish",
            }
        result = {"source_id": source_id, "status": status, "cloud": data,
                  "model": response.model}
        processed.append(result)
        if dry_run:
            continue

        stamp = now()
        conn.execute(
            """
            UPDATE intelligence_mentor_review_queue
               SET status = ?, cloud_provider = ?, cloud_model = ?, cloud_decision = ?,
                   cloud_reviewed_at = ?, updated_at = ?
             WHERE source_id = ?
            """,
            (status, provider.name, response.model,
             json.dumps(data, ensure_ascii=False), stamp, stamp, source_id),
        )
        if status == "cloud_approved":
            title = excerpt.get("title") or f"Source {source_id}"
            conn.execute(
                """
                INSERT OR IGNORE INTO intelligence_mentor_insights_local(
                    source_id, insight_type, observation, context, evidence_json,
                    why_now, relevance_score, suggestion, status, created_at, updated_at,
                    analysis_model, needs_agency
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, 0)
                """,
                (
                    source_id,
                    outcome["classification"],
                    f"La source « {title} » a été validée après revue "
                    f"({provider.name}) : {data['rationale']}",
                    f"Revue suite à : {reason}",
                    json.dumps({"source_id": source_id, "content_id": content_id,
                                "review_reason": reason}, ensure_ascii=False),
                    "Sortie de la file de review après revue cloud.",
                    outcome["relevance"],
                    "Évaluer rapidement si ce signal mérite un test concret.",
                    stamp,
                    stamp,
                    f"cloud:{provider.name}:{response.model}",
                ),
            )
        conn.commit()
    return {"status": "processed", "pending": len(items), "processed": processed}


if __name__ == "__main__":
    import argparse
    import os
    import sqlite3

    parser = argparse.ArgumentParser(description="Revue cloud optionnelle de la file")
    parser.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                     "vault.db"))
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    connection = sqlite3.connect(args.db, timeout=10)
    try:
        print(json.dumps(process_queue(connection, limit=args.limit, dry_run=not args.confirm),
                         ensure_ascii=False, indent=2))
    finally:
        connection.close()
