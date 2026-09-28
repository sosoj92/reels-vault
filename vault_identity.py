#!/usr/bin/env python3
"""
vault_identity.py - Resolution et fusion d'identites pour Life Intelligence V3.

Objectif : permettre de passer d'une entite provisoire ("named:solution:notion")
a une identite canonique verifiee, sans perdre aucune provenance.

Le module sait :
- enrichir une entite existante avec une identite canonique ;
- fusionner deux entites qui s'averent etre la meme chose ;
- conserver aliases, sources, claims, relations et liens V2 ;
- journaliser les resolutions/fusions.
"""

import json

import vault_intelligence as intelligence

IDENTITY_SCHEMA_VERSION = 1

IDENTITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_identity_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    action             TEXT NOT NULL,
    kept_entity_id     INTEGER NOT NULL,
    merged_entity_id   INTEGER,
    old_canonical_key  TEXT,
    new_canonical_key  TEXT,
    evidence_source_id INTEGER,
    detail             TEXT,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_identity_events_kept
    ON intelligence_identity_events(kept_entity_id);
"""


def ensure_identity_schema(conn):
    intelligence.ensure_schema(conn)
    conn.executescript(IDENTITY_SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('identity_schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(IDENTITY_SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def _entity(conn, entity_id):
    row = conn.execute(
        "SELECT * FROM intelligence_entities WHERE id = ?",
        (entity_id,),
    ).fetchone()
    if not row:
        raise intelligence.IntelligenceError(f"entity {entity_id} not found")
    return dict(row)


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    ).fetchone() is not None


def _metadata(value):
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def _merged_metadata(primary, secondary):
    """
    Fusion conservatrice : les valeurs deja etablies sur primary gagnent.
    Les metadonnees secondaires completent seulement les trous.
    """
    result = dict(secondary)
    result.update(primary)
    return result


def _event(
    conn,
    *,
    action,
    kept_entity_id,
    merged_entity_id=None,
    old_canonical_key=None,
    new_canonical_key=None,
    evidence_source_id=None,
    detail=None,
):
    conn.execute(
        """
        INSERT INTO intelligence_identity_events(
            action, kept_entity_id, merged_entity_id,
            old_canonical_key, new_canonical_key,
            evidence_source_id, detail, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action,
            kept_entity_id,
            merged_entity_id,
            old_canonical_key,
            new_canonical_key,
            evidence_source_id,
            detail,
            intelligence.now(),
        ),
    )


def add_alias(conn, entity_id, alias):
    alias = (alias or "").strip()
    if not alias:
        return
    intelligence.add_alias(conn, entity_id, alias)


def find_by_alias(conn, alias):
    alias = (alias or "").strip()
    if not alias:
        return []
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT e.*
              FROM intelligence_entities e
              JOIN intelligence_entity_aliases a ON a.entity_id = e.id
             WHERE a.alias = ? COLLATE NOCASE
             ORDER BY e.id
            """,
            (alias,),
        ).fetchall()
    ]


def merge_entities(
    conn,
    *,
    keep_id,
    merge_id,
    evidence_source_id=None,
    reason=None,
):
    """
    Fusion transactionnelle et non destructive de provenance.

    keep_id survit. merge_id est supprime apres transfert de toutes ses
    references utiles.
    """
    ensure_identity_schema(conn)

    if keep_id == merge_id:
        return keep_id

    keep = _entity(conn, keep_id)
    merged = _entity(conn, merge_id)

    try:
        conn.execute("BEGIN")

        # Le nom de l'entite absorbee devient un alias du survivant.
        conn.execute(
            """
            INSERT OR IGNORE INTO intelligence_entity_aliases(entity_id, alias, created_at)
            VALUES (?, ?, ?)
            """,
            (keep_id, merged["name"], intelligence.now()),
        )

        # Copier les aliases existants.
        for row in conn.execute(
            "SELECT alias, created_at FROM intelligence_entity_aliases WHERE entity_id = ?",
            (merge_id,),
        ).fetchall():
            conn.execute(
                """
                INSERT OR IGNORE INTO intelligence_entity_aliases(entity_id, alias, created_at)
                VALUES (?, ?, ?)
                """,
                (keep_id, row[0], row[1]),
            )

        # Sources : conserver toutes les provenances.
        for row in conn.execute(
            """
            SELECT source_id, role, notes, created_at
              FROM intelligence_entity_sources
             WHERE entity_id = ?
            """,
            (merge_id,),
        ).fetchall():
            conn.execute(
                """
                INSERT INTO intelligence_entity_sources(
                    entity_id, source_id, role, notes, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(entity_id, source_id, role)
                DO UPDATE SET notes = COALESCE(
                    intelligence_entity_sources.notes,
                    excluded.notes
                )
                """,
                (keep_id, row[0], row[1], row[2], row[3]),
            )

        # Claims rattaches a l'entite absorbee.
        conn.execute(
            "UPDATE intelligence_claims SET entity_id = ? WHERE entity_id = ?",
            (keep_id, merge_id),
        )

        # Liens vers les ressources V2 si le pont a deja ete initialise.
        if _table_exists(conn, "intelligence_resource_links"):
            conn.execute(
                "UPDATE intelligence_resource_links SET entity_id = ?, updated_at = ? "
                "WHERE entity_id = ?",
                (keep_id, intelligence.now(), merge_id),
            )

        # Relations : les recreer apres redirection, en supprimant les self-links.
        relation_rows = conn.execute(
            """
            SELECT subject_entity_id, predicate, object_entity_id,
                   source_id, notes, created_at
              FROM intelligence_relations
             WHERE subject_entity_id = ? OR object_entity_id = ?
            """,
            (merge_id, merge_id),
        ).fetchall()

        conn.execute(
            "DELETE FROM intelligence_relations "
            "WHERE subject_entity_id = ? OR object_entity_id = ?",
            (merge_id, merge_id),
        )

        for row in relation_rows:
            subject_id = keep_id if row[0] == merge_id else row[0]
            object_id = keep_id if row[2] == merge_id else row[2]
            if subject_id == object_id:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO intelligence_relations(
                    subject_entity_id, predicate, object_entity_id,
                    source_id, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (subject_id, row[1], object_id, row[3], row[4], row[5]),
            )

        keep_meta = _metadata(keep["metadata"])
        merge_meta = _metadata(merged["metadata"])
        combined_meta = _merged_metadata(keep_meta, merge_meta)

        # Si le survivant est deja resolu, une entite provisoire absorbee
        # ne doit jamais le remettre en needs_review.
        if keep_meta.get("needs_review") is False:
            combined_meta["needs_review"] = False

        conn.execute(
            """
            UPDATE intelligence_entities
               SET url = COALESCE(url, ?),
                   summary = COALESCE(summary, ?),
                   metadata = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (
                merged.get("url"),
                merged.get("summary"),
                json.dumps(combined_meta, ensure_ascii=False, sort_keys=True),
                intelligence.now(),
                keep_id,
            ),
        )

        _event(
            conn,
            action="merge",
            kept_entity_id=keep_id,
            merged_entity_id=merge_id,
            old_canonical_key=merged["canonical_key"],
            new_canonical_key=keep["canonical_key"],
            evidence_source_id=evidence_source_id,
            detail=reason,
        )

        conn.execute(
            "DELETE FROM intelligence_entities WHERE id = ?",
            (merge_id,),
        )
        conn.commit()
        return keep_id

    except Exception:
        conn.rollback()
        raise


def resolve_entity(
    conn,
    *,
    entity_id,
    canonical_key,
    canonical_name,
    canonical_url=None,
    entity_type=None,
    aliases=None,
    evidence_source_id=None,
    metadata=None,
):
    """
    Attache une identite canonique verifiee a une entite.

    Si canonical_key existe deja sur une autre entite, les deux sont fusionnees
    automatiquement vers l'entite deja canonique.
    """
    ensure_identity_schema(conn)

    current = _entity(conn, entity_id)
    existing = conn.execute(
        "SELECT id FROM intelligence_entities WHERE canonical_key = ?",
        (canonical_key,),
    ).fetchone()

    if existing and existing[0] != entity_id:
        target_id = merge_entities(
            conn,
            keep_id=existing[0],
            merge_id=entity_id,
            evidence_source_id=evidence_source_id,
            reason="canonical identity collision",
        )
        current = _entity(conn, target_id)
        entity_id = target_id

    old_key = current["canonical_key"]
    old_name = current["name"]

    current_meta = _metadata(current["metadata"])
    resolution_meta = dict(metadata or {})
    resolution_meta.update(
        {
            "needs_review": False,
            "identity_basis": "resolved",
            "resolved_at": intelligence.now(),
        }
    )
    merged_meta = _merged_metadata(resolution_meta, current_meta)

    try:
        conn.execute("BEGIN")

        if old_name and old_name.lower() != canonical_name.lower():
            conn.execute(
                """
                INSERT OR IGNORE INTO intelligence_entity_aliases(entity_id, alias, created_at)
                VALUES (?, ?, ?)
                """,
                (entity_id, old_name, intelligence.now()),
            )

        for alias in aliases or []:
            alias = (alias or "").strip()
            if alias:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO intelligence_entity_aliases(entity_id, alias, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (entity_id, alias, intelligence.now()),
                )

        conn.execute(
            """
            UPDATE intelligence_entities
               SET canonical_key = ?,
                   name = ?,
                   url = COALESCE(?, url),
                   entity_type = COALESCE(?, entity_type),
                   metadata = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (
                canonical_key,
                canonical_name,
                canonical_url,
                entity_type,
                json.dumps(merged_meta, ensure_ascii=False, sort_keys=True),
                intelligence.now(),
                entity_id,
            ),
        )

        if evidence_source_id is not None:
            conn.execute(
                """
                INSERT INTO intelligence_entity_sources(
                    entity_id, source_id, role, notes, created_at
                ) VALUES (?, ?, 'primary', ?, ?)
                ON CONFLICT(entity_id, source_id, role)
                DO UPDATE SET notes = COALESCE(
                    intelligence_entity_sources.notes,
                    excluded.notes
                )
                """,
                (
                    entity_id,
                    evidence_source_id,
                    "Primary identity evidence",
                    intelligence.now(),
                ),
            )

        _event(
            conn,
            action="resolve",
            kept_entity_id=entity_id,
            old_canonical_key=old_key,
            new_canonical_key=canonical_key,
            evidence_source_id=evidence_source_id,
            detail=f"{old_name} -> {canonical_name}",
        )

        conn.commit()
        return entity_id

    except Exception:
        conn.rollback()
        raise
