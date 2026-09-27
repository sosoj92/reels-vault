#!/usr/bin/env python3
"""
vault_intelligence_signals.py - File proactive derivee pour Life Intelligence V3.

La file est calculee a partir de vault.db et ne cree/modifie aucune donnee.
Elle sert a montrer ce qui merite l'attention de l'utilisatrice sans transformer
automatiquement un signal en action.

Signaux initiaux, volontairement conservateurs :
- contested_claim : une affirmation est contestee ;
- sensitive_claim_review : affirmation sensible non verifiee ;
- identity_review : identite provisoire / a revoir ;
- source_analysis_needed : source liee a une entite mais sans analyse active ;
- now_without_action : entite "now" sans action V2 ouverte liee.

Priorite : 1 = critique / a regarder d'abord, 4 = faible.
"""

import json

import vault_analysis as analysis
import vault_intelligence as intelligence


class IntelligenceSignalError(ValueError):
    pass


OPEN_ACTION_STATUSES = ("proposed", "approved", "todo", "doing")


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _metadata(value):
    try:
        parsed = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _source_payload_for_sensitivity(conn, source):
    content_id = source.get("content_id")
    if content_id is None or not _table_exists(conn, "contents"):
        return {
            "category": None,
            "title": source.get("title"),
            "description": None,
            "transcript": None,
        }

    row = conn.execute(
        """
        SELECT
            category,
            title,
            description_manual,
            description_ai,
            description_original,
            transcript
          FROM contents
         WHERE id = ?
        """,
        (content_id,),
    ).fetchone()

    if not row:
        return {
            "category": None,
            "title": source.get("title"),
            "description": None,
            "transcript": None,
        }

    row = dict(row)
    return {
        "category": row.get("category"),
        "title": row.get("title") or source.get("title"),
        "description": (
            row.get("description_manual")
            or row.get("description_ai")
            or row.get("description_original")
        ),
        "transcript": row.get("transcript"),
    }


def _source_is_sensitive(conn, source):
    return analysis._payload_is_sensitive(
        _source_payload_for_sensitivity(conn, source)
    )


def _linked_entities_for_source(conn, source_id):
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT DISTINCT
                e.id,
                e.name,
                e.entity_type,
                e.attention_status
              FROM intelligence_entity_sources es
              JOIN intelligence_entities e ON e.id = es.entity_id
             WHERE es.source_id = ?
               AND es.role <> 'primary'
             ORDER BY e.id
            """,
            (source_id,),
        ).fetchall()
    ]


def _source_has_active_analysis(conn, source_id):
    if not _table_exists(conn, "intelligence_source_analysis"):
        return False
    return conn.execute(
        """
        SELECT 1
          FROM intelligence_source_analysis
         WHERE source_id = ?
           AND superseded_at IS NULL
         LIMIT 1
        """,
        (source_id,),
    ).fetchone() is not None


def _entity_has_open_action(conn, entity_id):
    """
    Une action V2 est consideree comme suite de l'entite uniquement si le pont
    V2->V3 relie explicitement une resource a cette entite.
    """
    required = {"intelligence_resource_links", "actions"}
    if not all(_table_exists(conn, table) for table in required):
        return None

    placeholders = ",".join("?" for _ in OPEN_ACTION_STATUSES)
    row = conn.execute(
        f"""
        SELECT 1
          FROM intelligence_resource_links l
          JOIN actions a ON a.resource_id = l.resource_id
         WHERE l.entity_id = ?
           AND a.status IN ({placeholders})
         LIMIT 1
        """,
        (entity_id, *OPEN_ACTION_STATUSES),
    ).fetchone()
    return row is not None


def _signal(
    *,
    key,
    kind,
    priority,
    reason,
    suggested_action,
    entity_id=None,
    entity_name=None,
    source_id=None,
    source_url=None,
    claim_id=None,
    verification_status=None,
):
    return {
        "key": key,
        "kind": kind,
        "priority": priority,
        "reason": reason,
        "suggested_action": suggested_action,
        "entity_id": entity_id,
        "entity_name": entity_name,
        "source_id": source_id,
        "source_url": source_url,
        "claim_id": claim_id,
        "verification_status": verification_status,
    }


def collect_signals(conn, *, limit=100, priority=None):
    intelligence.ensure_schema(conn)

    limit = max(1, min(int(limit), 500))
    if priority is not None:
        priority = int(priority)
        if priority not in (1, 2, 3, 4):
            raise IntelligenceSignalError("priority must be between 1 and 4")

    signals = []

    # 1. Claims contestes : toujours visibles en premier.
    rows = conn.execute(
        """
        SELECT
            c.id AS claim_id,
            c.entity_id,
            c.verification_status,
            c.claim_text,
            e.name AS entity_name,
            s.id AS source_id,
            s.source_url
          FROM intelligence_claims c
          LEFT JOIN intelligence_entities e ON e.id = c.entity_id
          JOIN intelligence_sources s ON s.id = c.source_id
         WHERE c.verification_status = 'contested'
         ORDER BY c.id DESC
        """
    ).fetchall()

    for row in rows:
        row = dict(row)
        signals.append(
            _signal(
                key=f"contested_claim:{row['claim_id']}",
                kind="contested_claim",
                priority=1,
                reason="Une affirmation dispose d'une preuve de contestation.",
                suggested_action="Examiner les sources avant toute utilisation de cette affirmation.",
                entity_id=row.get("entity_id"),
                entity_name=row.get("entity_name"),
                source_id=row["source_id"],
                source_url=row["source_url"],
                claim_id=row["claim_id"],
                verification_status=row["verification_status"],
            )
        )

    # 2. Claims sensibles asserted/unverified.
    sensitive_cache = {}
    rows = conn.execute(
        """
        SELECT
            c.id AS claim_id,
            c.entity_id,
            c.verification_status,
            e.name AS entity_name,
            s.*
          FROM intelligence_claims c
          LEFT JOIN intelligence_entities e ON e.id = c.entity_id
          JOIN intelligence_sources s ON s.id = c.source_id
         WHERE c.verification_status IN ('asserted', 'unverified')
         ORDER BY c.id DESC
        """
    ).fetchall()

    for row in rows:
        item = dict(row)
        source_id = item["id"]
        if source_id not in sensitive_cache:
            sensitive_cache[source_id] = _source_is_sensitive(conn, item)
        if not sensitive_cache[source_id]:
            continue

        signals.append(
            _signal(
                key=f"sensitive_claim_review:{item['claim_id']}",
                kind="sensitive_claim_review",
                priority=1,
                reason=(
                    "Affirmation issue d'une source sensible et non verifiee "
                    "independamment."
                ),
                suggested_action=(
                    "Verifier avec une source primaire ou fiable avant decision."
                ),
                entity_id=item.get("entity_id"),
                entity_name=item.get("entity_name"),
                source_id=source_id,
                source_url=item.get("source_url"),
                claim_id=item["claim_id"],
                verification_status=item["verification_status"],
            )
        )

    # 3. Identites provisoires explicitement marquees needs_review.
    rows = conn.execute(
        "SELECT id, name, metadata FROM intelligence_entities ORDER BY id DESC"
    ).fetchall()
    for row in rows:
        item = dict(row)
        if _metadata(item.get("metadata")).get("needs_review") is not True:
            continue
        signals.append(
            _signal(
                key=f"identity_review:{item['id']}",
                kind="identity_review",
                priority=2,
                reason="L'identite de cette entite est encore provisoire.",
                suggested_action="Resoudre ou confirmer l'identite avant enrichissement automatique.",
                entity_id=item["id"],
                entity_name=item["name"],
            )
        )

    # 4. Sources liees a des entites sans analyse active.
    rows = conn.execute(
        """
        SELECT DISTINCT s.*
          FROM intelligence_sources s
          JOIN intelligence_entity_sources es ON es.source_id = s.id
         WHERE es.role <> 'primary'
         ORDER BY s.id DESC
        """
    ).fetchall()

    for row in rows:
        source = dict(row)
        if _source_has_active_analysis(conn, source["id"]):
            continue

        linked = _linked_entities_for_source(conn, source["id"])
        sensitive = _source_is_sensitive(conn, source)
        names = ", ".join(item["name"] for item in linked[:3])
        if len(linked) > 3:
            names += f" +{len(linked) - 3}"

        signals.append(
            _signal(
                key=f"source_analysis_needed:{source['id']}",
                kind="source_analysis_needed",
                priority=2 if sensitive else 3,
                reason=(
                    "Source sensible sans analyse active."
                    if sensitive
                    else "Source liee a une entite mais sans analyse active."
                ),
                suggested_action=(
                    "Analyser quand une capacite CAREFUL fiable est disponible."
                    if sensitive
                    else "Analyser en FAST lorsqu'elle devient utile."
                ),
                entity_name=names or None,
                source_id=source["id"],
                source_url=source.get("source_url"),
            )
        )

    # 5. Entites NOW sans action V2 ouverte explicitement reliee.
    rows = conn.execute(
        """
        SELECT id, name
          FROM intelligence_entities
         WHERE attention_status = 'now'
         ORDER BY updated_at DESC, id DESC
        """
    ).fetchall()
    for row in rows:
        item = dict(row)
        has_action = _entity_has_open_action(conn, item["id"])
        if has_action is not False:
            # None = pont/action schema absent : ne pas inventer un manque.
            continue
        signals.append(
            _signal(
                key=f"now_without_action:{item['id']}",
                kind="now_without_action",
                priority=2,
                reason="Entite marquee Maintenant sans action ouverte explicitement liee.",
                suggested_action="Decider de la prochaine action ou changer le statut d'attention.",
                entity_id=item["id"],
                entity_name=item["name"],
            )
        )

    if priority is not None:
        signals = [
            signal for signal in signals
            if signal["priority"] == priority
        ]

    signals.sort(
        key=lambda item: (
            item["priority"],
            item["kind"],
            item["key"],
        )
    )

    counts = {1: 0, 2: 0, 3: 0, 4: 0}
    for signal in signals:
        counts[signal["priority"]] += 1

    return {
        "items": signals[:limit],
        "total": len(signals),
        "counts_by_priority": counts,
        "limit": limit,
        "derived": True,
        "writes_performed": False,
    }
