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

import json

import vault_db
import vault_intelligence as intelligence
import vault_intelligence_signals as signals
import vault_orchestrator as orchestrator
from vault_agent_registry import AgentRegistryError


SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_signal_actions (
    signal_key   TEXT PRIMARY KEY,
    action_id    INTEGER NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intelligence_signal_actions_action
    ON intelligence_signal_actions(action_id);

CREATE TABLE IF NOT EXISTS intelligence_action_orchestration (
    action_id    INTEGER PRIMARY KEY REFERENCES actions(id) ON DELETE CASCADE,
    task         TEXT NOT NULL,
    routes_json  TEXT NOT NULL DEFAULT '[]',
    agents_json  TEXT NOT NULL DEFAULT '[]',
    source       TEXT NOT NULL DEFAULT 'agency-agents',
    status       TEXT NOT NULL DEFAULT 'ready',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intelligence_action_orchestration_status
    ON intelligence_action_orchestration(status);
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



def _orchestration_task(signal):
    hints = {
        "contested_claim": "knowledge graph source claim contradiction verification",
        "sensitive_claim_review": "knowledge graph source claim sensitive privacy verification",
        "identity_review": "knowledge graph entity identity resolution provenance",
        "source_analysis_needed": "knowledge graph source analysis sensitive privacy",
        "now_without_action": "workflow orchestration next action automation",
    }
    parts = [
        hints.get(signal.get("kind"), ""),
        ACTION_SPECS.get(signal.get("kind"), {}).get("title", ""),
        signal.get("reason") or "",
        signal.get("suggested_action") or "",
        signal.get("entity_name") or "",
    ]
    return " — ".join(part for part in parts if part)


def _orchestration_metadata(signal):
    task = _orchestration_task(signal)
    try:
        context = orchestrator.build_orchestration_context(task)
        return {
            "task": task,
            "status": "ready",
            "routes": context["routes"],
            "agents": [
                {
                    "name": item["name"],
                    "description": item["description"],
                    "path": item["path"],
                }
                for item in context["agents"]
            ],
        }
    except (AgentRegistryError, OSError) as exc:
        return {
            "task": task,
            "status": "unavailable",
            "routes": [],
            "agents": [],
            "error": str(exc),
        }


def _upsert_action_orchestration(conn, action_id, signal):
    metadata = _orchestration_metadata(signal)
    stamp = intelligence.now()
    conn.execute(
        """
        INSERT INTO intelligence_action_orchestration(
            action_id, task, routes_json, agents_json,
            source, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'agency-agents', ?, ?, ?)
        ON CONFLICT(action_id) DO UPDATE SET
            task = excluded.task,
            routes_json = excluded.routes_json,
            agents_json = excluded.agents_json,
            source = excluded.source,
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (
            action_id,
            metadata["task"],
            json.dumps(metadata["routes"], ensure_ascii=False, sort_keys=True),
            json.dumps(metadata["agents"], ensure_ascii=False, sort_keys=True),
            metadata["status"],
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return metadata


def _description(signal, orchestration=None):
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
    if orchestration and orchestration.get("agents"):
        lines.append(
            "Playbooks de raisonnement : "
            + ", ".join(agent["name"] for agent in orchestration["agents"])
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
        orchestration_meta = _upsert_action_orchestration(
            conn,
            existing["id"],
            signal,
        )
        return {
            "status": "existing",
            "signal_key": signal["key"],
            "action": existing,
            "orchestration": orchestration_meta,
        }

    content_id = _source_content_id(conn, signal.get("source_id"))
    resource_id = _resource_id_for_entity(
        conn,
        signal.get("entity_id"),
        content_id,
    )

    spec = ACTION_SPECS[signal["kind"]]
    orchestration_meta = _orchestration_metadata(signal)
    action = vault_db.create_action(
        conn,
        {
            "title": _title(signal),
            "description": _description(signal, orchestration_meta),
            "action_type": spec["action_type"],
            "priority": signal["priority"],
            "content_id": content_id,
            "resource_id": resource_id,
            "proposed_by": "life_intelligence",
            "status": "proposed",
        },
    )
    _upsert_action_orchestration(conn, action["id"], signal)

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
        "orchestration": orchestration_meta,
    }


def propose_priority_actions(conn, *, max_priority=2):
    ensure_schema(conn)

    queue = signals.collect_signals(conn, limit=500)
    created = existing = unsupported = skipped_priority = review_only = 0
    items = []

    for signal in queue["items"]:
        if signal["priority"] > max_priority:
            skipped_priority += 1
            continue
        if signal["kind"] in signals.REVIEW_ONLY_KINDS:
            # Review interne : pas d'action automatique (ni carte cockpit).
            review_only += 1
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
        "review_only": review_only,
        "items": items,
    }


def list_signal_actions(conn):
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT
            l.signal_key,
            a.*,
            c.source_url AS content_source_url,
            c.title AS content_title,
            r.name AS resource_name,
            o.task AS orchestration_task,
            o.routes_json AS orchestration_routes,
            o.agents_json AS orchestration_agents,
            o.status AS orchestration_status
          FROM intelligence_signal_actions l
          JOIN actions a ON a.id = l.action_id
          LEFT JOIN contents c ON c.id = a.content_id
          LEFT JOIN resources r ON r.id = a.resource_id
          LEFT JOIN intelligence_action_orchestration o ON o.action_id = a.id
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
