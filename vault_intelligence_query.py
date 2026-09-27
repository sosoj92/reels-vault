#!/usr/bin/env python3
"""
vault_intelligence_query.py - Couche de lecture stable pour Life Intelligence V3.

Objectif :
- exposer la memoire V3 sans obliger les consommateurs a connaitre le schema SQL ;
- conserver la provenance et le statut epistemique de chaque affirmation ;
- rester strictement read-only cote metier : aucune creation d'entite, claim,
  relation ou action depuis cette couche.

Cette couche est le contrat de lecture destine a l'Assistant IA, au cockpit
Notion et aux futures automatisations.
"""

import json

import vault_intelligence as intelligence


class IntelligenceQueryError(ValueError):
    pass


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


def _entity_dict(row):
    item = dict(row)
    item["metadata"] = _metadata(item.get("metadata"))
    return item


def _source_dict(row):
    item = dict(row)
    item["metadata"] = _metadata(item.get("metadata"))
    return item


def _entity_filters(*, entity_type=None, attention_status=None, q=None):
    clauses = []
    params = []

    if entity_type:
        clauses.append("e.entity_type = ?")
        params.append(entity_type)

    if attention_status:
        if attention_status not in intelligence.ATTENTION_STATUSES:
            raise IntelligenceQueryError(
                f"invalid attention_status: {attention_status}"
            )
        clauses.append("e.attention_status = ?")
        params.append(attention_status)

    q = (q or "").strip()
    if q:
        needle = f"%{q}%"
        clauses.append(
            """
            (
                e.name LIKE ? COLLATE NOCASE
                OR e.canonical_key LIKE ? COLLATE NOCASE
                OR COALESCE(e.summary, '') LIKE ? COLLATE NOCASE
                OR EXISTS (
                    SELECT 1
                      FROM intelligence_entity_aliases a
                     WHERE a.entity_id = e.id
                       AND a.alias LIKE ? COLLATE NOCASE
                )
            )
            """
        )
        params.extend([needle, needle, needle, needle])

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where, params


def list_entities(
    conn,
    *,
    entity_type=None,
    attention_status=None,
    q=None,
    limit=100,
    offset=0,
):
    """
    Liste compacte pour un cockpit ou une recherche assistant.

    Le resultat ne melange pas "asserted" et "verified" : les compteurs
    epistemiques restent explicites.
    """
    intelligence.ensure_schema(conn)

    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    where, params = _entity_filters(
        entity_type=entity_type,
        attention_status=attention_status,
        q=q,
    )

    total = conn.execute(
        f"SELECT COUNT(*) FROM intelligence_entities e{where}",
        params,
    ).fetchone()[0]

    rows = conn.execute(
        f"""
        SELECT
            e.*,
            (
                SELECT COUNT(DISTINCT es.source_id)
                  FROM intelligence_entity_sources es
                 WHERE es.entity_id = e.id
            ) AS source_count,
            (
                SELECT COUNT(*)
                  FROM intelligence_claims c
                 WHERE c.entity_id = e.id
            ) AS claim_count,
            (
                SELECT COUNT(*)
                  FROM intelligence_claims c
                 WHERE c.entity_id = e.id
                   AND c.verification_status = 'verified'
            ) AS verified_claim_count,
            (
                SELECT COUNT(*)
                  FROM intelligence_claims c
                 WHERE c.entity_id = e.id
                   AND c.verification_status = 'contested'
            ) AS contested_claim_count,
            (
                SELECT COUNT(*)
                  FROM intelligence_relations r
                 WHERE r.subject_entity_id = e.id
                    OR r.object_entity_id = e.id
            ) AS relation_count
          FROM intelligence_entities e
          {where}
         ORDER BY
            CASE e.attention_status
                WHEN 'now' THEN 1
                WHEN 'later' THEN 2
                WHEN 'in_place' THEN 3
                WHEN 'dismissed' THEN 4
                ELSE 5
            END,
            e.updated_at DESC,
            e.id DESC
         LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    return {
        "items": [_entity_dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _latest_source_analysis(conn, source_id):
    if not _table_exists(conn, "intelligence_source_analysis"):
        return None

    row = conn.execute(
        """
        SELECT id, provider, model, summary, created_at
          FROM intelligence_source_analysis
         WHERE source_id = ?
           AND superseded_at IS NULL
         ORDER BY created_at DESC, id DESC
         LIMIT 1
        """,
        (source_id,),
    ).fetchone()
    return dict(row) if row else None


def get_entity(conn, entity_id):
    """
    Retourne une vue probante complete d'une entite :
    aliases, sources, analyses actives, claims et relations.

    raw_result des LLM n'est volontairement pas renvoye ici : il reste un
    artefact d'audit, pas une connaissance active.
    """
    intelligence.ensure_schema(conn)

    row = conn.execute(
        "SELECT * FROM intelligence_entities WHERE id = ?",
        (entity_id,),
    ).fetchone()
    if not row:
        return None

    entity = _entity_dict(row)

    aliases = [
        r[0]
        for r in conn.execute(
            """
            SELECT alias
              FROM intelligence_entity_aliases
             WHERE entity_id = ?
             ORDER BY alias COLLATE NOCASE
            """,
            (entity_id,),
        ).fetchall()
    ]

    source_rows = conn.execute(
        """
        SELECT
            s.*,
            es.role,
            es.notes AS link_notes
          FROM intelligence_entity_sources es
          JOIN intelligence_sources s ON s.id = es.source_id
         WHERE es.entity_id = ?
         ORDER BY
            CASE es.role
                WHEN 'primary' THEN 1
                WHEN 'describes' THEN 2
                WHEN 'recommends' THEN 3
                WHEN 'mentions' THEN 4
                WHEN 'criticizes' THEN 5
                ELSE 6
            END,
            s.id DESC
        """,
        (entity_id,),
    ).fetchall()

    sources = []
    for source_row in source_rows:
        source = _source_dict(source_row)
        source["active_analysis"] = _latest_source_analysis(
            conn,
            source["id"],
        )
        sources.append(source)

    claim_rows = conn.execute(
        """
        SELECT
            c.*,
            s.source_url,
            s.title AS source_title,
            vs.source_url AS verification_source_url,
            vs.title AS verification_source_title
          FROM intelligence_claims c
          JOIN intelligence_sources s ON s.id = c.source_id
          LEFT JOIN intelligence_sources vs
                 ON vs.id = c.verification_source_id
         WHERE c.entity_id = ?
         ORDER BY
            CASE c.verification_status
                WHEN 'contested' THEN 1
                WHEN 'verified' THEN 2
                WHEN 'asserted' THEN 3
                WHEN 'unverified' THEN 4
                ELSE 5
            END,
            c.id DESC
        """,
        (entity_id,),
    ).fetchall()
    claims = [dict(r) for r in claim_rows]

    outgoing = conn.execute(
        """
        SELECT
            r.id,
            r.predicate,
            r.notes,
            r.created_at,
            r.source_id,
            s.source_url,
            e.id AS related_entity_id,
            e.name AS related_entity_name,
            e.entity_type AS related_entity_type,
            e.canonical_key AS related_entity_key
          FROM intelligence_relations r
          JOIN intelligence_entities e ON e.id = r.object_entity_id
          LEFT JOIN intelligence_sources s ON s.id = r.source_id
         WHERE r.subject_entity_id = ?
         ORDER BY r.id DESC
        """,
        (entity_id,),
    ).fetchall()

    incoming = conn.execute(
        """
        SELECT
            r.id,
            r.predicate,
            r.notes,
            r.created_at,
            r.source_id,
            s.source_url,
            e.id AS related_entity_id,
            e.name AS related_entity_name,
            e.entity_type AS related_entity_type,
            e.canonical_key AS related_entity_key
          FROM intelligence_relations r
          JOIN intelligence_entities e ON e.id = r.subject_entity_id
          LEFT JOIN intelligence_sources s ON s.id = r.source_id
         WHERE r.object_entity_id = ?
         ORDER BY r.id DESC
        """,
        (entity_id,),
    ).fetchall()

    relations = [
        {"direction": "outgoing", **dict(r)}
        for r in outgoing
    ] + [
        {"direction": "incoming", **dict(r)}
        for r in incoming
    ]

    verification_counts = {
        status: 0 for status in intelligence.VERIFICATION_STATUSES
    }
    for claim in claims:
        status = claim.get("verification_status")
        if status in verification_counts:
            verification_counts[status] += 1

    entity["aliases"] = aliases
    entity["sources"] = sources
    entity["claims"] = claims
    entity["relations"] = relations
    entity["epistemic"] = {
        "source_count": len({s["id"] for s in sources}),
        "claim_count": len(claims),
        "relation_count": len(relations),
        "verification_counts": verification_counts,
    }
    return entity


def overview(conn):
    """
    Snapshot leger pour le cockpit : attention visible + etat epistemique.
    """
    intelligence.ensure_schema(conn)

    attention = {status: 0 for status in intelligence.ATTENTION_STATUSES}
    for row in conn.execute(
        """
        SELECT attention_status, COUNT(*) AS n
          FROM intelligence_entities
         GROUP BY attention_status
        """
    ).fetchall():
        attention[row[0]] = row[1]

    verification = {
        status: 0 for status in intelligence.VERIFICATION_STATUSES
    }
    for row in conn.execute(
        """
        SELECT verification_status, COUNT(*) AS n
          FROM intelligence_claims
         GROUP BY verification_status
        """
    ).fetchall():
        verification[row[0]] = row[1]

    return {
        "entities": conn.execute(
            "SELECT COUNT(*) FROM intelligence_entities"
        ).fetchone()[0],
        "sources": conn.execute(
            "SELECT COUNT(*) FROM intelligence_sources"
        ).fetchone()[0],
        "claims": conn.execute(
            "SELECT COUNT(*) FROM intelligence_claims"
        ).fetchone()[0],
        "relations": conn.execute(
            "SELECT COUNT(*) FROM intelligence_relations"
        ).fetchone()[0],
        "attention": attention,
        "verification": verification,
    }
