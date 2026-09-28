#!/usr/bin/env python3
"""
vault_intelligence_audit.py - Audit lisible de la couche Life Intelligence V3.

Lecture seule sur les donnees V3 existantes. Ce script ne promeut rien et
n'ecrit aucune donnee metier. Il sert a verifier la qualite de la migration :
- combien de sources et d'entites existent ;
- combien d'entites sont reutilisees par plusieurs sources ;
- quelles entites restent a verifier ;
- quels types d'entites dominent.
"""

import argparse
import json
import sqlite3
from collections import Counter

import vault_intelligence as intelligence


def audit(conn, top=10):
    intelligence.ensure_schema(conn)

    sources = conn.execute(
        "SELECT COUNT(*) FROM intelligence_sources"
    ).fetchone()[0]
    entities = conn.execute(
        "SELECT COUNT(*) FROM intelligence_entities"
    ).fetchone()[0]
    relations = conn.execute(
        "SELECT COUNT(*) FROM intelligence_relations"
    ).fetchone()[0]
    claims = conn.execute(
        "SELECT COUNT(*) FROM intelligence_claims"
    ).fetchone()[0]

    rows = conn.execute(
        "SELECT entity_type, metadata FROM intelligence_entities"
    ).fetchall()

    by_type = Counter()
    needs_review = []
    for row in rows:
        by_type[row[0]] += 1
        try:
            metadata = json.loads(row[1] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        if metadata.get("needs_review"):
            needs_review.append(1)

    multi_source = conn.execute(
        """
        SELECT e.id, e.name, e.entity_type, e.canonical_key,
               COUNT(DISTINCT es.source_id) AS source_count
          FROM intelligence_entities e
          JOIN intelligence_entity_sources es ON es.entity_id = e.id
         WHERE es.role <> 'primary'
         GROUP BY e.id
        HAVING COUNT(DISTINCT es.source_id) > 1
         ORDER BY source_count DESC, e.name COLLATE NOCASE
         LIMIT ?
        """,
        (top,),
    ).fetchall()

    review_examples = conn.execute(
        """
        SELECT id, name, entity_type, canonical_key
          FROM intelligence_entities
         WHERE json_extract(metadata, '$.needs_review') = 1
         ORDER BY name COLLATE NOCASE
         LIMIT ?
        """,
        (top,),
    ).fetchall()

    return {
        "sources": sources,
        "entities": entities,
        "relations": relations,
        "claims": claims,
        "needs_review": len(needs_review),
        "reused_entities": conn.execute(
            """
            SELECT COUNT(*)
              FROM (
                    SELECT entity_id
                      FROM intelligence_entity_sources
                     WHERE role <> 'primary'
                     GROUP BY entity_id
                    HAVING COUNT(DISTINCT source_id) > 1
              )
            """
        ).fetchone()[0],
        "entities_by_type": dict(sorted(by_type.items())),
        "top_reused_entities": [
            {
                "id": row[0],
                "name": row[1],
                "entity_type": row[2],
                "canonical_key": row[3],
                "source_count": row[4],
            }
            for row in multi_source
        ],
        "review_examples": [
            {
                "id": row[0],
                "name": row[1],
                "entity_type": row[2],
                "canonical_key": row[3],
            }
            for row in review_examples
        ],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Audit lecture seule de Life Intelligence V3"
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        print(json.dumps(audit(conn, top=args.top), ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
