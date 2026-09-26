#!/usr/bin/env python3
"""
vault_intelligence_bridge.py - Pont non destructif V2 -> Life Intelligence V3.

Ce module ne remplace pas la V2 et n'ecrit jamais dans ses tables metier.
Il lit :
- contents : chaque contenu devient une source V3 ;
- resources : chaque ressource explicite peut devenir une entite V3.

Aucune entite n'est extraite par IA ici. Le pont ne promeut que des donnees
qui existent deja explicitement dans la V2.

Identite :
- repository GitHub explicite -> github:owner/repo ;
- ressource avec URL -> url:<url canonique> ;
- ressource sans URL -> named:<type>:<nom normalise>, marquee needs_review.

La table intelligence_resource_links conserve la correspondance entre une
ressource V2 et l'entite V3 obtenue, afin de rendre le pont idempotent.
"""

import argparse
import json
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

import vault_intelligence as intelligence

BRIDGE_SCHEMA_VERSION = 1

BRIDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_resource_links (
    resource_id INTEGER PRIMARY KEY REFERENCES resources(id) ON DELETE CASCADE,
    entity_id   INTEGER NOT NULL REFERENCES intelligence_entities(id) ON DELETE CASCADE,
    linked_at   TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intelligence_resource_links_entity
    ON intelligence_resource_links(entity_id);
"""

RESOURCE_ENTITY_TYPES = {
    "outil": "tool",
    "logiciel": "software",
    "application": "app",
    "site": "website",
    "personne": "person",
    "livre": "book",
    "formation": "training",
    "service": "service",
    "méthode": "method",
    "methode": "method",
    "lieu": "place",
    "autre": "other",
}

# Pour les ressources sans URL, le type V2 n'est pas assez fiable pour faire
# partie seul de l'identite. "Notion [outil]" et "Notion [logiciel]" doivent
# pouvoir converger. En revanche, on evite de fusionner un lieu, une personne
# ou un livre homonyme avec une solution numerique.
NAME_IDENTITY_GROUPS = {
    "tool": "solution",
    "software": "solution",
    "app": "solution",
    "website": "solution",
    "service": "solution",
    "person": "person",
    "place": "place",
    "book": "book",
    "training": "training",
    "method": "method",
    "other": "other",
}


def _fold(value):
    value = unicodedata.normalize("NFD", value or "")
    return "".join(c for c in value if unicodedata.category(c) != "Mn").lower()


def _slug(value):
    value = _fold(value)
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "unknown"


def canonical_web_url(url):
    """Normalise une URL d'identite : pas de tracking, fragment ni slash final."""
    value = (url or "").strip()
    if not value:
        return None

    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return value

    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            "",
        )
    )


def ensure_bridge_schema(conn):
    intelligence.ensure_schema(conn)
    conn.executescript(BRIDGE_SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('intelligence_bridge_schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(BRIDGE_SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def _resource_entity_type(resource):
    key = _fold(resource.get("type") or "autre")
    return RESOURCE_ENTITY_TYPES.get(key, "other")


def resource_identity(resource):
    """
    Produit une identite stable a partir d'une ressource V2 explicite.

    Un repo GitHub n'est cree que lorsqu'une URL github.com permet d'identifier
    owner/repo. Sans URL, on ne devine jamais un repository.
    """
    name = (resource.get("name") or "").strip()
    url = canonical_web_url(resource.get("url"))
    entity_type = _resource_entity_type(resource)

    if not name:
        raise intelligence.IntelligenceError("resource name is required")

    if url:
        parsed = urlsplit(url)
        if parsed.netloc.lower() in ("github.com", "www.github.com"):
            try:
                github = intelligence.github_repo_identity(url)
            except intelligence.IntelligenceError:
                pass
            else:
                return {
                    "entity_type": "github_repo",
                    "canonical_key": github["canonical_key"],
                    "name": f"{github['owner']}/{github['repo']}",
                    "url": github["canonical_url"],
                    "identity_basis": "github_url",
                    "needs_review": False,
                    "github": github,
                }

        return {
            "entity_type": entity_type,
            "canonical_key": f"url:{url}",
            "name": name,
            "url": url,
            "identity_basis": "url",
            "needs_review": False,
            "github": None,
        }

    identity_group = NAME_IDENTITY_GROUPS.get(entity_type, "other")
    provisional_type = "solution" if identity_group == "solution" else entity_type
    return {
        "entity_type": provisional_type,
        "canonical_key": f"named:{identity_group}:{_slug(name)}",
        "name": name,
        "url": None,
        "identity_basis": "name_group",
        "needs_review": True,
        "github": None,
    }


def _source_metadata(conn, content_id):
    subthemes = [
        row[0]
        for row in conn.execute(
            """
            SELECT s.name
              FROM subthemes s
              JOIN content_subthemes cs ON cs.subtheme_id = s.id
             WHERE cs.content_id = ?
             ORDER BY s.name
            """,
            (content_id,),
        ).fetchall()
    ] if _table_exists(conn, "subthemes") and _table_exists(conn, "content_subthemes") else []

    tags = [
        row[0]
        for row in conn.execute(
            """
            SELECT t.name
              FROM tags t
              JOIN content_tags ct ON ct.tag_id = t.id
             WHERE ct.content_id = ?
             ORDER BY t.name
            """,
            (content_id,),
        ).fetchall()
    ] if _table_exists(conn, "tags") and _table_exists(conn, "content_tags") else []

    return {
        "legacy_content_id": content_id,
        "category": _content_value(conn, content_id, "category"),
        "subthemes": subthemes,
        "tags": tags,
    }


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _column_exists(conn, table, column):
    if not _table_exists(conn, table):
        return False
    return column in {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _content_value(conn, content_id, column):
    if not _column_exists(conn, "contents", column):
        return None
    row = conn.execute(
        f"SELECT {column} FROM contents WHERE id = ?",
        (content_id,),
    ).fetchone()
    return row[0] if row else None


def _get_resource_link(conn, resource_id):
    row = conn.execute(
        "SELECT entity_id FROM intelligence_resource_links WHERE resource_id = ?",
        (resource_id,),
    ).fetchone()
    return row[0] if row else None


def _save_resource_link(conn, resource_id, entity_id):
    stamp = intelligence.now()
    conn.execute(
        """
        INSERT INTO intelligence_resource_links(resource_id, entity_id, linked_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(resource_id)
        DO UPDATE SET entity_id = excluded.entity_id, updated_at = excluded.updated_at
        """,
        (resource_id, entity_id, stamp, stamp),
    )
    conn.commit()


def promote_resource(conn, *, resource, source_id):
    """
    Promeut UNE ressource V2 en entite V3 et la relie a sa source.

    La liaison legacy resource_id -> entity_id rend l'operation idempotente.
    """
    ensure_bridge_schema(conn)

    resource = dict(resource)
    resource_id = resource["id"]
    identity = resource_identity(resource)

    # La correspondance V2 -> V3 vit dans intelligence_resource_links.
    # On n'enregistre pas un unique legacy_resource_id/type dans l'entite :
    # une meme entite peut provenir de nombreuses ressources V2.
    metadata = {
        "identity_basis": identity["identity_basis"],
        "needs_review": identity["needs_review"],
    }

    if identity["github"]:
        entity_id = intelligence.upsert_github_repo(
            conn,
            url=identity["url"],
            summary=resource.get("description"),
            metadata=metadata,
        )
    else:
        entity_id = intelligence.upsert_entity(
            conn,
            entity_type=identity["entity_type"],
            canonical_key=identity["canonical_key"],
            name=identity["name"],
            url=identity["url"],
            summary=resource.get("description"),
            metadata=metadata,
        )

    previous_entity_id = _get_resource_link(conn, resource_id)
    _save_resource_link(conn, resource_id, entity_id)

    intelligence.link_entity_source(
        conn,
        entity_id=entity_id,
        source_id=source_id,
        role="mentions",
        notes="Promoted from explicit V2 resource",
    )

    return {
        "resource_id": resource_id,
        "entity_id": entity_id,
        "canonical_key": identity["canonical_key"],
        "entity_type": identity["entity_type"],
        "needs_review": identity["needs_review"],
        "relinked": previous_entity_id not in (None, entity_id),
    }


def promote_content(conn, content_id, include_resources=True):
    """
    Transforme un contenu V2 en source V3 puis promeut ses ressources explicites.

    Ne modifie aucune ligne de contents/resources.
    """
    ensure_bridge_schema(conn)

    row = conn.execute(
        "SELECT * FROM contents WHERE id = ?",
        (content_id,),
    ).fetchone()
    if not row:
        raise intelligence.IntelligenceError(f"V2 content {content_id} not found")

    content = dict(row)
    source_url = (content.get("source_url") or "").strip()
    if not source_url:
        raise intelligence.IntelligenceError(
            f"V2 content {content_id} has no source_url"
        )

    source_id = intelligence.upsert_source(
        conn,
        source_url=source_url,
        platform=content.get("platform"),
        source_type=content.get("content_type") or "content",
        title=content.get("title"),
        author=content.get("author"),
        content_id=content_id,
        captured_at=content.get("ingested_at") or content.get("created_at"),
        metadata=_source_metadata(conn, content_id),
    )

    promoted = []
    if include_resources and _table_exists(conn, "resources"):
        resources = conn.execute(
            "SELECT * FROM resources WHERE content_id = ? ORDER BY id",
            (content_id,),
        ).fetchall()
        for resource in resources:
            promoted.append(
                promote_resource(
                    conn,
                    resource=resource,
                    source_id=source_id,
                )
            )

    return {
        "content_id": content_id,
        "source_id": source_id,
        "resources_promoted": promoted,
    }


def promote_all_contents(conn, limit=None):
    """
    Migration V2 -> V3 idempotente. Elle n'est jamais lancee automatiquement.
    """
    ensure_bridge_schema(conn)

    sql = "SELECT id FROM contents ORDER BY id"
    params = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)

    results = []
    for row in conn.execute(sql, params).fetchall():
        results.append(promote_content(conn, row[0], include_resources=True))

    return {
        "contents_promoted": len(results),
        "resources_promoted": sum(
            len(item["resources_promoted"])
            for item in results
        ),
        "items": results,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Pont non destructif V2 -> Life Intelligence V3"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--content-id", type=int)
    group.add_argument("--all", action="store_true")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="avec --all, ne promouvoir que les N premiers contenus",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="base SQLite a utiliser ; par defaut vault.db",
    )
    args = parser.parse_args()

    conn = intelligence.connect(args.db)
    try:
        if args.content_id is not None:
            result = promote_content(conn, args.content_id)
        else:
            result = promote_all_contents(conn, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
