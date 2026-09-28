import sqlite3
from pathlib import Path

# Chemin absolu : jamais de base vide creee dans le repertoire courant.
conn = sqlite3.connect(Path(__file__).resolve().parent / "vault.db")
conn.execute("PRAGMA foreign_keys = ON")

try:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intelligence_action_planning (
            action_id INTEGER PRIMARY KEY
                REFERENCES actions(id) ON DELETE CASCADE,

            notion_page_id TEXT,
            notion_url TEXT,

            duration_minutes INTEGER
                CHECK (duration_minutes IS NULL OR duration_minutes > 0),

            rigidity TEXT NOT NULL DEFAULT 'flexible'
                CHECK (rigidity IN ('fixed', 'flexible', 'optional')),

            deadline_type TEXT NOT NULL DEFAULT 'none'
                CHECK (deadline_type IN ('hard', 'target', 'none')),

            energy TEXT
                CHECK (energy IS NULL OR energy IN ('low', 'medium', 'high')),

            splittable INTEGER NOT NULL DEFAULT 1
                CHECK (splittable IN (0, 1)),

            planning_status TEXT NOT NULL DEFAULT 'unplanned'
                CHECK (
                    planning_status IN (
                        'unplanned',
                        'proposed',
                        'planned',
                        'reschedule',
                        'done'
                    )
                ),

            calendar_event_ids TEXT NOT NULL DEFAULT '[]',
            context TEXT,
            synced_at TEXT
        )
    """)

    conn.commit()

    print("=== ACTION PLANNING SCHEMA OK ===")
    print(
        conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='intelligence_action_planning'"
        ).fetchone()[0]
    )

except Exception:
    conn.rollback()
    raise

finally:
    conn.close()
