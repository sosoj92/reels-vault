#!/usr/bin/env python3
"""
vault_intelligence_actions.py - Transforme certains signaux V3 en actions V2 proposees.

Regles :
- aucune execution externe ;
- proposed_by='life_intelligence' => vault_db force status='proposed' ;
- un signal = au plus une action, grace a intelligence_signal_actions ;
- seules les priorites 1-2 sont materialisees automatiquement ;
- les liens vers content/resource sont reutilises quand ils existent.
"""

import vault_db
import vault_intelligence as intelligence
import vault_intelligence_signals as signals


SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_signal_actions (
    signal_key   TEXT PRIMARY KEY,
    action_id    INTEGER NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intelligence_signal_actions_action
    ON intelligence_signal_actions(action_id);
"""


ACTION_SPECS = {
    "contested_claim": {
        "title": "Examiner un claim contesté",
        "action_type": "approfondir",
    },
    "sensitive_claim_review": {
        "title": "Vérifier un claim sensible",
        "action_type": "rechercher",
    },
    "identity_review": {
        "title": "Confirmer une identité",
        "action_type": "rechercher",
    },
    "source_analysis_needed": {
        "title": "Analyser une source sensible",
        "action_type": "approfondir",
    },
    "now_without_action": {
        "title": "Définir la prochaine action",
        "action_type": "autre",
    },
}


def ensure_schema(conn):
    intelligence.ensure_schema(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _source_content_id(conn, source_id):
    if not source_id:
        return None
    row = conn.execute(
        "SELECT content_id FROM intelligence_sources WHERE id=?",
        (source_id,),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def _resource_id_for_entity(conn, entity_id, content_id=None):
    if not entity_id or not _table_exists(conn, "intelligence_resource_links"):
        return None

    if content_id is not None:
        row = conn.execute(
            """
            SELECT l.resource_id
              FROM intelligence_resource_links l
              JOIN resources r ON r.id = l.resource_id
             WHERE l.entity_id = ?
               AND r.content_id = ?
             ORDER BY l.resource_id
             LIMIT 1
            """,
            (entity_id, content_id),
        ).fetchone()
        if row:
            return row[0]

    row = conn.execute(
        """
        SELECT resource_id
          FROM intelligence_resource_links
         WHERE entity_id = ?
         ORDER BY resource_id
         LIMIT 1
        """,
        (entity_id,),
    ).fetchone()
    return row[0] if row else None


def _existing_action(conn, signal_key):
    row = conn.execute(
        """
        SELECT action_id
          FROM intelligence_signal_actions
         WHERE signal_key = ?
        """,
        (signal_key,),
    ).fetchone()
    if not row:
        return None
    try:
        return vault_db.get_action(conn, row[0])
    except vault_db.VaultError:
        return None


def _description(signal):
    lines = [
        signal["reason"],
        "",
        f"Proposition du Life Intelligence System : {signal['suggested_action']}",
        "",
        f"Signal Vault : {signal['key']}",
    ]
    if signal.get("source_url"):
        lines.append(f"Source : {signal['source_url']}")
    if signal.get("verification_status"):
        lines.append(
            f"Statut épistémique : {signal['verification_status']}"
        )
    return "\n".join(lines)


def _title(signal):
    spec = ACTION_SPECS[signal["kind"]]
    subject = signal.get("entity_name")
    return (
        f"{spec['title']} — {subject}"
        if subject
        else spec["title"]
    )


def propose_signal_action(conn, signal):
    ensure_schema(conn)

    if signal["kind"] not in ACTION_SPECS:
        return {"status": "unsupported", "signal_key": signal["key"]}

    existing = _existing_action(conn, signal["key"])
    if existing:
        return {
            "status": "existing",
            "signal_key": signal["key"],
            "action": existing,
        }

    content_id = _source_content_id(conn, signal.get("source_id"))
    resource_id = _resource_id_for_entity(
        conn,
        signal.get("entity_id"),
        content_id,
    )

    spec = ACTION_SPECS[signal["kind"]]
    action = vault_db.create_action(
        conn,
        {
            "title": _title(signal),
            "description": _description(signal),
            "action_type": spec["action_type"],
            "priority": signal["priority"],
            "content_id": content_id,
            "resource_id": resource_id,
            "proposed_by": "life_intelligence",
            "status": "proposed",
        },
    )

    stamp = intelligence.now()
    conn.execute(
        """
        INSERT INTO intelligence_signal_actions(
            signal_key, action_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(signal_key) DO UPDATE SET
            action_id = excluded.action_id,
            updated_at = excluded.updated_at
        """,
        (signal["key"], action["id"], stamp, stamp),
    )
    conn.commit()

    return {
        "status": "created",
        "signal_key": signal["key"],
        "action": action,
    }


def propose_priority_actions(conn, *, max_priority=2):
    ensure_schema(conn)

    queue = signals.collect_signals(conn, limit=500)
    created = existing = unsupported = skipped_priority = 0
    items = []

    for signal in queue["items"]:
        if signal["priority"] > max_priority:
            skipped_priority += 1
            continue

        result = propose_signal_action(conn, signal)
        items.append(result)

        if result["status"] == "created":
            created += 1
        elif result["status"] == "existing":
            existing += 1
        else:
            unsupported += 1

    return {
        "created": created,
        "existing": existing,
        "unsupported": unsupported,
        "skipped_priority": skipped_priority,
        "items": items,
    }


def list_signal_actions(conn):
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT
            l.signal_key,
            a.*
          FROM intelligence_signal_actions l
          JOIN actions a ON a.id = l.action_id
         ORDER BY
            CASE a.status
                WHEN 'proposed' THEN 0
                WHEN 'approved' THEN 1
                WHEN 'todo' THEN 2
                WHEN 'doing' THEN 3
                WHEN 'done' THEN 4
                WHEN 'dismissed' THEN 5
                ELSE 6
            END,
            COALESCE(a.priority, 5),
            a.id DESC
        """
    ).fetchall()

    return {
        "items": [dict(row) for row in rows],
        "total": len(rows),
    }
