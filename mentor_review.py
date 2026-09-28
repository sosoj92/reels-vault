
import json
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(conn):
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
    conn.commit()


def enqueue(conn, row, analysis):
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
        "cloud_escalation_required",
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
