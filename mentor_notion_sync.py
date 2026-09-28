import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone

from vault_notion_cockpit import NotionCockpitClient


# Chemin absolu : un chemin relatif creerait une base vide si le script est
# lance depuis un autre repertoire courant (inbox.bat, planificateur...).
DB = os.environ.get("REELS_VAULT_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "vault.db"
)
DATA_SOURCE_ID = "8707f9af-9959-4512-b583-a84613c413ec"


def now():
    return datetime.now(timezone.utc).isoformat()


def connect():
    if not os.path.exists(DB):
        raise SystemExit(f"Base Vault introuvable : {DB}")
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn):
    cols = {
        r[1]
        for r in conn.execute(
            "PRAGMA table_info(intelligence_mentor_insights_local)"
        )
    }

    for name in (
        "notion_page_id",
        "notion_url",
        "notion_synced_at",
    ):
        if name not in cols:
            conn.execute(
                f"ALTER TABLE intelligence_mentor_insights_local "
                f"ADD COLUMN {name} TEXT"
            )

    conn.commit()


def rich(text):
    text = str(text or "").strip()
    if not text:
        return []

    return [{
        "type": "text",
        "text": {
            "content": text[:2000]
        }
    }]


def notion_type(insight_type):
    return {
        "opportunity": "🎯 Opportunité",
        "opportunity_convergence": "🧩 Convergence",
        "convergence": "🧩 Convergence",
        "novelty": "💡 Suggestion",
    }.get(
        insight_type,
        "💡 Suggestion"
    )


def pending(conn, limit=20):
    return conn.execute("""
        SELECT
            i.*,
            s.title AS source_title,
            s.source_url
        FROM intelligence_mentor_insights_local i
        LEFT JOIN intelligence_sources s
          ON s.id = i.source_id
        WHERE i.notion_page_id IS NULL
        ORDER BY i.id
        LIMIT ?
    """, (limit,)).fetchall()


def _row_value(row, key):
    return row[key] if key in row.keys() else None


def props(row):
    title = (
        row["source_title"]
        or row["observation"]
        or f"Mentor Insight — source {row['source_id']}"
    )

    return {
        "Insight": {
            "title": rich(title[:150])
        },

        "Type": {
            "select": {
                "name": notion_type(
                    row["insight_type"]
                )
            }
        },

        "Pertinence": {
            "number": row["relevance_score"]
        },

        "Contexte": {
            "rich_text": rich(
                row["context"]
            )
        },

        "Pourquoi maintenant": {
            "rich_text": rich(
                row["why_now"]
            )
        },

        "Preuves": {
            "rich_text": rich(
                row["evidence_json"]
            )
        },

        "Suggestion": {
            "rich_text": rich(
                row["suggestion"]
            )
        },

        "Statut": {
            "select": {
                "name": row["status"] or "proposed"
            }
        },

        "Mentor managed": {
            "checkbox": True
        },

        "Modèle": {
            "rich_text": rich(
                _row_value(row, "analysis_model") or "deterministic-v1"
            )
        },

        "Vault refs": {
            "rich_text": rich(
                f"source:{row['source_id']}"
            )
        },
    }


def run(limit=20, confirm=False):
    conn = connect()

    try:
        ensure_schema(conn)

        rows = pending(
            conn,
            limit
        )

        preview = [
            {
                "local_insight_id": r["id"],
                "source_id": r["source_id"],
                "type": r["insight_type"],
                "relevance": r["relevance_score"],
                "title": r["source_title"],
            }
            for r in rows
        ]

        if not confirm:
            print(json.dumps({
                "writes_performed": False,
                "pending_count": len(rows),
                "items": preview,
            }, ensure_ascii=False, indent=2))
            return

        client = NotionCockpitClient(
            data_source_id=DATA_SOURCE_ID
        )

        created = 0

        for row in rows:
            page = client.create_page(
                props(row)
            )

            conn.execute("""
                UPDATE intelligence_mentor_insights_local
                SET notion_page_id = ?,
                    notion_url = ?,
                    notion_synced_at = ?
                WHERE id = ?
            """, (
                page["id"],
                page.get("url"),
                now(),
                row["id"],
            ))

            # Commit page par page : si une creation echoue plus loin, les
            # pages deja creees restent marquees et ne seront pas dupliquees.
            conn.commit()

            created += 1

        conn.commit()

        print(json.dumps({
            "writes_performed": True,
            "created": created,
            "items": preview,
        }, ensure_ascii=False, indent=2))

    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--limit",
        type=int,
        default=20
    )

    parser.add_argument(
        "--confirm",
        action="store_true"
    )

    args = parser.parse_args()

    run(
        limit=args.limit,
        confirm=args.confirm
    )
