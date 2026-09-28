#!/usr/bin/env python3
"""
vault_resolution.py - Resolver automatique prudent pour Life Intelligence V3.

But :
1. exploiter d'abord les preuves deja presentes dans vault.db ;
2. appliquer automatiquement seulement les rapprochements locaux non ambigus ;
3. placer les cas non resolus dans une file de recherche pour l'Assistant IA.

Important :
- resoudre une IDENTITE ne verifie pas les affirmations faites a son sujet ;
- aucun nom seul n'est transforme en URL ou repository invente ;
- les personnes, lieux, livres, formations et methodes ne sont jamais fusionnes
  automatiquement par simple egalite de nom.
"""

import argparse
import json
import re
import unicodedata

import vault_identity as identity
import vault_intelligence as intelligence

RESOLUTION_SCHEMA_VERSION = 1

RESOLUTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_resolution_candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id           INTEGER NOT NULL,
    candidate_entity_id INTEGER NOT NULL,
    evidence_kind       TEXT NOT NULL,
    evidence            TEXT NOT NULL DEFAULT '{}',
    safe_to_apply       INTEGER NOT NULL DEFAULT 0
                        CHECK (safe_to_apply IN (0, 1)),
    status              TEXT NOT NULL DEFAULT 'proposed'
                        CHECK (status IN ('proposed', 'ambiguous', 'applied', 'dismissed')),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (entity_id, candidate_entity_id, evidence_kind)
);

CREATE INDEX IF NOT EXISTS idx_resolution_candidates_entity
    ON intelligence_resolution_candidates(entity_id);
CREATE INDEX IF NOT EXISTS idx_resolution_candidates_status
    ON intelligence_resolution_candidates(status);
"""

AUTO_MERGE_ENTITY_TYPES = {"solution"}


def ensure_resolution_schema(conn):
    identity.ensure_identity_schema(conn)
    conn.executescript(RESOLUTION_SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('resolution_schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(RESOLUTION_SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def _fold(value):
    value = unicodedata.normalize("NFD", value or "")
    value = "".join(c for c in value if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _metadata(row):
    try:
        value = json.loads(row["metadata"] or "{}")
    except (json.JSONDecodeError, TypeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _unresolved_entities(conn):
    rows = conn.execute(
        "SELECT * FROM intelligence_entities ORDER BY id"
    ).fetchall()
    result = []
    for row in rows:
        row = dict(row)
        if _metadata(row).get("needs_review") is True:
            result.append(row)
    return result


def _linked_resource_names(conn, entity_id):
    """
    Retourne les noms V2 explicitement relies a cette entite.
    Si le pont V2 n'existe pas, renvoie simplement [].
    """
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if not {"resources", "intelligence_resource_links"} <= tables:
        return []

    rows = conn.execute(
        """
        SELECT r.name
          FROM resources r
          JOIN intelligence_resource_links l ON l.resource_id = r.id
         WHERE l.entity_id = ?
         ORDER BY r.id
        """,
        (entity_id,),
    ).fetchall()

    names = []
    seen = set()
    for row in rows:
        name = (row[0] or "").strip()
        key = _fold(name)
        if key and key not in seen:
            seen.add(key)
            names.append(name)
    return names


def _canonical_entities_for_name(conn, normalized_name, exclude_entity_id):
    """
    Cherche des entites deja ancrees par URL/repository dont une ressource V2
    porte exactement le meme nom normalise.
    """
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if not {"resources", "intelligence_resource_links"} <= tables:
        return []

    rows = conn.execute(
        """
        SELECT DISTINCT e.*
          FROM resources r
          JOIN intelligence_resource_links l ON l.resource_id = r.id
          JOIN intelligence_entities e ON e.id = l.entity_id
         WHERE e.id <> ?
           AND (
                e.url IS NOT NULL
                OR e.canonical_key LIKE 'url:%'
                OR e.canonical_key LIKE 'github:%'
           )
         ORDER BY e.id
        """,
        (exclude_entity_id,),
    ).fetchall()

    matches = []
    for row in rows:
        row = dict(row)
        resource_names = _linked_resource_names(conn, row["id"])
        if any(_fold(name) == normalized_name for name in resource_names):
            matches.append(row)
    return matches


def _upsert_candidate(
    conn,
    *,
    entity_id,
    candidate_entity_id,
    evidence_kind,
    evidence,
    safe_to_apply,
    status,
):
    stamp = intelligence.now()
    conn.execute(
        """
        INSERT INTO intelligence_resolution_candidates(
            entity_id, candidate_entity_id, evidence_kind, evidence,
            safe_to_apply, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(entity_id, candidate_entity_id, evidence_kind)
        DO UPDATE SET
            evidence = excluded.evidence,
            safe_to_apply = excluded.safe_to_apply,
            status = CASE
                WHEN intelligence_resolution_candidates.status = 'applied'
                    THEN 'applied'
                ELSE excluded.status
            END,
            updated_at = excluded.updated_at
        """,
        (
            entity_id,
            candidate_entity_id,
            evidence_kind,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            1 if safe_to_apply else 0,
            status,
            stamp,
            stamp,
        ),
    )


def discover_local_candidates(conn):
    """
    Decouvre les rapprochements possibles uniquement a partir des donnees V2/V3.

    Regle auto-safe actuelle :
    - entite provisoire de type solution ;
    - meme nom explicite dans les resources V2 ;
    - exactement UNE entite canonique correspondante avec URL/repository.

    Tout autre cas reste a rechercher/revoir.
    """
    ensure_resolution_schema(conn)

    proposed = ambiguous = unresolved_without_candidate = 0

    for entity in _unresolved_entities(conn):
        names = _linked_resource_names(conn, entity["id"])
        candidate_map = {}

        for name in names:
            normalized = _fold(name)
            if not normalized:
                continue
            for candidate in _canonical_entities_for_name(
                conn,
                normalized,
                entity["id"],
            ):
                candidate_map[candidate["id"]] = {
                    "candidate": candidate,
                    "matched_name": name,
                    "normalized_name": normalized,
                }

        candidates = list(candidate_map.values())

        safe = (
            entity["entity_type"] in AUTO_MERGE_ENTITY_TYPES
            and len(candidates) == 1
        )

        if len(candidates) == 1:
            status = "proposed"
            proposed += 1
        elif len(candidates) > 1:
            status = "ambiguous"
            ambiguous += 1
        else:
            unresolved_without_candidate += 1
            continue

        for item in candidates:
            candidate = item["candidate"]
            _upsert_candidate(
                conn,
                entity_id=entity["id"],
                candidate_entity_id=candidate["id"],
                evidence_kind="exact_legacy_resource_name",
                evidence={
                    "matched_name": item["matched_name"],
                    "normalized_name": item["normalized_name"],
                    "candidate_canonical_key": candidate["canonical_key"],
                    "candidate_url": candidate.get("url"),
                },
                safe_to_apply=safe,
                status=status,
            )

    conn.commit()

    return {
        "proposed_entities": proposed,
        "ambiguous_entities": ambiguous,
        "unresolved_without_local_candidate": unresolved_without_candidate,
    }


def apply_safe_candidates(conn):
    """
    Applique les resolutions locales marquees safe_to_apply=1.

    Une collision avec une entite canonique existante passe par vault_identity,
    qui preserve sources, aliases, claims, relations et liens V2.
    """
    ensure_resolution_schema(conn)

    rows = conn.execute(
        """
        SELECT *
          FROM intelligence_resolution_candidates
         WHERE status = 'proposed'
           AND safe_to_apply = 1
         ORDER BY id
        """
    ).fetchall()

    applied = []
    skipped = []

    for candidate_row in rows:
        candidate_row = dict(candidate_row)

        source_entity = conn.execute(
            "SELECT * FROM intelligence_entities WHERE id = ?",
            (candidate_row["entity_id"],),
        ).fetchone()
        target_entity = conn.execute(
            "SELECT * FROM intelligence_entities WHERE id = ?",
            (candidate_row["candidate_entity_id"],),
        ).fetchone()

        if source_entity is None or target_entity is None:
            skipped.append(candidate_row["id"])
            continue

        source_entity = dict(source_entity)
        target_entity = dict(target_entity)

        # Revalider juste avant mutation : pas d'auto-merge hors "solution".
        if source_entity["entity_type"] not in AUTO_MERGE_ENTITY_TYPES:
            skipped.append(candidate_row["id"])
            continue

        resolved_id = identity.resolve_entity(
            conn,
            entity_id=source_entity["id"],
            canonical_key=target_entity["canonical_key"],
            canonical_name=target_entity["name"],
            canonical_url=target_entity.get("url"),
            entity_type=target_entity["entity_type"],
            aliases=[source_entity["name"]],
            metadata={
                "resolved_via": "exact_legacy_resource_name",
            },
        )

        conn.execute(
            """
            UPDATE intelligence_resolution_candidates
               SET status = 'applied',
                   updated_at = ?
             WHERE id = ?
            """,
            (intelligence.now(), candidate_row["id"]),
        )
        conn.commit()

        applied.append(
            {
                "candidate_id": candidate_row["id"],
                "from_entity_id": source_entity["id"],
                "resolved_entity_id": resolved_id,
                "canonical_key": target_entity["canonical_key"],
            }
        )

    return {
        "applied": len(applied),
        "skipped": len(skipped),
        "items": applied,
    }


def research_queue(conn, limit=50):
    """
    File destinee au Research Synthesist / Assistant IA.

    Elle contient seulement les identites encore non resolues apres les regles
    locales. Aucun score pseudo-precis : on expose la raison et les preuves.
    """
    ensure_resolution_schema(conn)

    queue = []
    for entity in _unresolved_entities(conn):
        candidate_rows = conn.execute(
            """
            SELECT *
              FROM intelligence_resolution_candidates
             WHERE entity_id = ?
               AND status IN ('proposed', 'ambiguous')
             ORDER BY id
            """,
            (entity["id"],),
        ).fetchall()

        candidates = []
        for row in candidate_rows:
            row = dict(row)
            target = conn.execute(
                "SELECT canonical_key, name, url, entity_type "
                "FROM intelligence_entities WHERE id = ?",
                (row["candidate_entity_id"],),
            ).fetchone()
            if target:
                candidates.append(
                    {
                        "entity_id": row["candidate_entity_id"],
                        "canonical_key": target[0],
                        "name": target[1],
                        "url": target[2],
                        "entity_type": target[3],
                        "evidence_kind": row["evidence_kind"],
                    }
                )

        source_count = conn.execute(
            """
            SELECT COUNT(DISTINCT source_id)
              FROM intelligence_entity_sources
             WHERE entity_id = ?
               AND role <> 'primary'
            """,
            (entity["id"],),
        ).fetchone()[0]

        if len(candidates) > 1:
            reason = "ambiguous_local_candidates"
        elif len(candidates) == 1:
            reason = "manual_or_external_confirmation_required"
        else:
            reason = "primary_source_research_required"

        queue.append(
            {
                "entity_id": entity["id"],
                "name": entity["name"],
                "entity_type": entity["entity_type"],
                "canonical_key": entity["canonical_key"],
                "source_count": source_count,
                "resource_names": _linked_resource_names(conn, entity["id"]),
                "reason": reason,
                "local_candidates": candidates,
            }
        )

    queue.sort(
        key=lambda item: (
            -item["source_count"],
            item["name"].lower(),
        )
    )
    return queue[:limit]


def run_local_resolution(conn):
    discovery = discover_local_candidates(conn)
    application = apply_safe_candidates(conn)
    return {
        "discovery": discovery,
        "application": application,
        "research_queue": research_queue(conn, limit=20),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Resolver prudent Life Intelligence V3"
    )
    parser.add_argument("--db", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--discover", action="store_true")
    action.add_argument("--apply-safe", action="store_true")
    action.add_argument("--queue", action="store_true")
    action.add_argument("--run", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    conn = intelligence.connect(args.db)
    try:
        if args.discover:
            result = discover_local_candidates(conn)
        elif args.apply_safe:
            result = apply_safe_candidates(conn)
        elif args.queue:
            result = research_queue(conn, limit=args.limit)
        else:
            result = run_local_resolution(conn)

        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
