#!/usr/bin/env python3
"""
vault_intelligence.py - Couche Life Intelligence V3 du Vault.

Cette couche est additive : elle utilise le meme vault.db mais ne modifie aucune
table V2. Elle introduit les concepts sources -> entites -> relations, ainsi que
la tracabilite des affirmations et de leur verification.

Principes :
- une source = origine/preuve ;
- une entite = objet persistant et deduplique ;
- plusieurs sources peuvent enrichir une meme entite ;
- les relations entre entites sont des donnees de premier rang ;
- une affirmation conserve la source qui l'a formulee et, le cas echeant,
  la source qui la verifie ou la conteste.
"""

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

INTELLIGENCE_SCHEMA_VERSION = 1

ATTENTION_STATUSES = ("now", "later", "in_place", "dismissed")
VERIFICATION_STATUSES = ("asserted", "unverified", "verified", "contested")
SOURCE_ROLES = ("mentions", "describes", "recommends", "criticizes", "primary", "relevant_to")

INTELLIGENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_entities (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type      TEXT NOT NULL,
    canonical_key    TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    url              TEXT,
    summary          TEXT,
    attention_status TEXT NOT NULL DEFAULT 'later'
                     CHECK (attention_status IN ('now', 'later', 'in_place', 'dismissed')),
    metadata         TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intelligence_entity_aliases (
    entity_id  INTEGER NOT NULL REFERENCES intelligence_entities(id) ON DELETE CASCADE,
    alias      TEXT NOT NULL COLLATE NOCASE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (entity_id, alias)
);

CREATE TABLE IF NOT EXISTS intelligence_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url  TEXT NOT NULL UNIQUE,
    platform    TEXT,
    source_type TEXT,
    title       TEXT,
    author      TEXT,
    content_id  INTEGER REFERENCES contents(id) ON DELETE SET NULL,
    captured_at TEXT,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intelligence_entity_sources (
    entity_id  INTEGER NOT NULL REFERENCES intelligence_entities(id) ON DELETE CASCADE,
    source_id  INTEGER NOT NULL REFERENCES intelligence_sources(id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'mentions',
    notes      TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (entity_id, source_id, role)
);

CREATE TABLE IF NOT EXISTS intelligence_relations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_entity_id INTEGER NOT NULL REFERENCES intelligence_entities(id) ON DELETE CASCADE,
    predicate         TEXT NOT NULL,
    object_entity_id  INTEGER NOT NULL REFERENCES intelligence_entities(id) ON DELETE CASCADE,
    source_id         INTEGER REFERENCES intelligence_sources(id) ON DELETE SET NULL,
    notes             TEXT,
    created_at        TEXT NOT NULL,
    CHECK (subject_entity_id <> object_entity_id)
);

CREATE TABLE IF NOT EXISTS intelligence_claims (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id              INTEGER REFERENCES intelligence_entities(id) ON DELETE SET NULL,
    source_id              INTEGER NOT NULL REFERENCES intelligence_sources(id) ON DELETE CASCADE,
    claim_text             TEXT NOT NULL,
    verification_status    TEXT NOT NULL DEFAULT 'asserted'
                           CHECK (verification_status IN ('asserted', 'unverified', 'verified', 'contested')),
    verification_source_id INTEGER REFERENCES intelligence_sources(id) ON DELETE SET NULL,
    notes                  TEXT,
    verified_at            TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intelligence_entities_type
    ON intelligence_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_intelligence_entities_status
    ON intelligence_entities(attention_status);
CREATE INDEX IF NOT EXISTS idx_intelligence_sources_content
    ON intelligence_sources(content_id);
CREATE INDEX IF NOT EXISTS idx_intelligence_entity_sources_source
    ON intelligence_entity_sources(source_id);
CREATE INDEX IF NOT EXISTS idx_intelligence_relations_subject
    ON intelligence_relations(subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_intelligence_relations_object
    ON intelligence_relations(object_entity_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_intelligence_relation_unique
    ON intelligence_relations(
        subject_entity_id,
        predicate,
        object_entity_id,
        COALESCE(source_id, 0)
    );
CREATE INDEX IF NOT EXISTS idx_intelligence_claims_entity
    ON intelligence_claims(entity_id);
CREATE INDEX IF NOT EXISTS idx_intelligence_claims_status
    ON intelligence_claims(verification_status);
"""


class IntelligenceError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value):
    if value is None:
        return "{}"
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except json.JSONDecodeError as exc:
            raise IntelligenceError("metadata must be valid JSON") from exc
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _merge_metadata(existing, incoming):
    base = json.loads(existing or "{}")
    if incoming is None:
        return json.dumps(base, ensure_ascii=False, sort_keys=True)
    if isinstance(incoming, str):
        try:
            incoming = json.loads(incoming)
        except json.JSONDecodeError as exc:
            raise IntelligenceError("metadata must be valid JSON") from exc
    if not isinstance(incoming, dict):
        raise IntelligenceError("metadata must be an object")
    base.update(incoming)
    return json.dumps(base, ensure_ascii=False, sort_keys=True)


def ensure_schema(conn):
    """Cree la couche V3 sans modifier les tables V2 existantes."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(INTELLIGENCE_SCHEMA)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    current = conn.execute(
        "SELECT value FROM meta WHERE key = 'intelligence_schema_version'"
    ).fetchone()
    # N'ecrit que si necessaire : les lecteurs (Ask, contexte) peuvent ainsi
    # utiliser une connexion en lecture seule.
    if current is None or current[0] != str(INTELLIGENCE_SCHEMA_VERSION):
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('intelligence_schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(INTELLIGENCE_SCHEMA_VERSION),),
        )
        conn.commit()
    return conn


def connect(path=None):
    """Ouvre vault.db via la V2 puis initialise uniquement les tables V3."""
    from vault_db import connect as v2_connect

    conn = v2_connect(path)
    return ensure_schema(conn)


def get_entity_by_key(conn, canonical_key):
    row = conn.execute(
        "SELECT * FROM intelligence_entities WHERE canonical_key = ?",
        (canonical_key,),
    ).fetchone()
    return dict(row) if row else None


def upsert_entity(
    conn,
    *,
    entity_type,
    canonical_key,
    name,
    url=None,
    summary=None,
    attention_status=None,
    metadata=None,
):
    """Cree ou enrichit une entite stable sans dupliquer sa canonical_key."""
    if not entity_type or not canonical_key or not name:
        raise IntelligenceError("entity_type, canonical_key and name are required")
    if attention_status is not None and attention_status not in ATTENTION_STATUSES:
        raise IntelligenceError(f"invalid attention_status: {attention_status}")

    existing = get_entity_by_key(conn, canonical_key)
    stamp = now()

    if existing:
        merged_metadata = _merge_metadata(existing["metadata"], metadata)
        conn.execute(
            """
            UPDATE intelligence_entities
               SET name = ?,
                   url = COALESCE(?, url),
                   summary = COALESCE(?, summary),
                   attention_status = COALESCE(?, attention_status),
                   metadata = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (
                name,
                url,
                summary,
                attention_status,
                merged_metadata,
                stamp,
                existing["id"],
            ),
        )
        conn.commit()
        return existing["id"]

    cur = conn.execute(
        """
        INSERT INTO intelligence_entities(
            entity_type, canonical_key, name, url, summary, attention_status,
            metadata, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_type,
            canonical_key,
            name,
            url,
            summary,
            attention_status or "later",
            _json(metadata),
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return cur.lastrowid


def add_alias(conn, entity_id, alias):
    alias = (alias or "").strip()
    if not alias:
        raise IntelligenceError("alias is required")
    conn.execute(
        """
        INSERT OR IGNORE INTO intelligence_entity_aliases(entity_id, alias, created_at)
        VALUES (?, ?, ?)
        """,
        (entity_id, alias, now()),
    )
    conn.commit()


def upsert_source(
    conn,
    *,
    source_url,
    platform=None,
    source_type=None,
    title=None,
    author=None,
    content_id=None,
    captured_at=None,
    metadata=None,
):
    if not source_url:
        raise IntelligenceError("source_url is required")

    row = conn.execute(
        "SELECT * FROM intelligence_sources WHERE source_url = ?",
        (source_url,),
    ).fetchone()
    stamp = now()

    if row:
        row = dict(row)
        merged_metadata = _merge_metadata(row["metadata"], metadata)
        conn.execute(
            """
            UPDATE intelligence_sources
               SET platform = COALESCE(?, platform),
                   source_type = COALESCE(?, source_type),
                   title = COALESCE(?, title),
                   author = COALESCE(?, author),
                   content_id = COALESCE(?, content_id),
                   captured_at = COALESCE(?, captured_at),
                   metadata = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (
                platform,
                source_type,
                title,
                author,
                content_id,
                captured_at,
                merged_metadata,
                stamp,
                row["id"],
            ),
        )
        conn.commit()
        return row["id"]

    cur = conn.execute(
        """
        INSERT INTO intelligence_sources(
            source_url, platform, source_type, title, author, content_id,
            captured_at, metadata, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_url,
            platform,
            source_type,
            title,
            author,
            content_id,
            captured_at,
            _json(metadata),
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return cur.lastrowid


def link_entity_source(conn, *, entity_id, source_id, role="mentions", notes=None):
    if role not in SOURCE_ROLES:
        raise IntelligenceError(f"invalid source role: {role}")

    conn.execute(
        """
        INSERT INTO intelligence_entity_sources(entity_id, source_id, role, notes, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(entity_id, source_id, role)
        DO UPDATE SET notes = COALESCE(excluded.notes, intelligence_entity_sources.notes)
        """,
        (entity_id, source_id, role, notes, now()),
    )
    conn.commit()


def add_relation(
    conn,
    *,
    subject_entity_id,
    predicate,
    object_entity_id,
    source_id=None,
    notes=None,
):
    if not predicate:
        raise IntelligenceError("predicate is required")
    if subject_entity_id == object_entity_id:
        raise IntelligenceError("a relation cannot target the same entity")

    conn.execute(
        """
        INSERT OR IGNORE INTO intelligence_relations(
            subject_entity_id, predicate, object_entity_id, source_id, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            subject_entity_id,
            predicate,
            object_entity_id,
            source_id,
            notes,
            now(),
        ),
    )
    conn.commit()

    row = conn.execute(
        """
        SELECT id
          FROM intelligence_relations
         WHERE subject_entity_id = ?
           AND predicate = ?
           AND object_entity_id = ?
           AND COALESCE(source_id, 0) = COALESCE(?, 0)
        """,
        (subject_entity_id, predicate, object_entity_id, source_id),
    ).fetchone()
    return row[0]


def record_claim(
    conn,
    *,
    source_id,
    claim_text,
    entity_id=None,
    verification_status="asserted",
    verification_source_id=None,
    notes=None,
):
    if verification_status not in VERIFICATION_STATUSES:
        raise IntelligenceError(f"invalid verification_status: {verification_status}")
    if not claim_text:
        raise IntelligenceError("claim_text is required")
    if verification_status in ("verified", "contested") and verification_source_id is None:
        raise IntelligenceError(
            "verified or contested claims require a verification_source_id"
        )

    stamp = now()
    cur = conn.execute(
        """
        INSERT INTO intelligence_claims(
            entity_id, source_id, claim_text, verification_status,
            verification_source_id, notes, verified_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_id,
            source_id,
            claim_text,
            verification_status,
            verification_source_id,
            notes,
            stamp if verification_status in ("verified", "contested") else None,
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return cur.lastrowid


def github_repo_identity(url):
    """
    Valide une URL GitHub explicite et renvoie owner/repo + cle canonique.
    Aucun repo n'est invente a partir d'un nom ou d'un texte ambigu.
    """
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in (
        "github.com",
        "www.github.com",
    ):
        raise IntelligenceError("an explicit github.com repository URL is required")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise IntelligenceError("GitHub URL does not identify owner/repo")

    owner = parts[0]
    repo = re.sub(r"\.git$", "", parts[1], flags=re.IGNORECASE)

    if not owner or not repo:
        raise IntelligenceError("GitHub URL does not identify owner/repo")

    return {
        "owner": owner,
        "repo": repo,
        "canonical_key": f"github:{owner.lower()}/{repo.lower()}",
        "canonical_url": f"https://github.com/{owner}/{repo}",
    }


def upsert_github_repo(
    conn,
    *,
    url,
    summary=None,
    attention_status=None,
    metadata=None,
):
    identity = github_repo_identity(url)
    repo_metadata = dict(metadata or {})
    repo_metadata.update(
        {
            "owner": identity["owner"],
            "repo": identity["repo"],
        }
    )

    return upsert_entity(
        conn,
        entity_type="github_repo",
        canonical_key=identity["canonical_key"],
        name=f"{identity['owner']}/{identity['repo']}",
        url=identity["canonical_url"],
        summary=summary,
        attention_status=attention_status,
        metadata=repo_metadata,
    )
