"""Fixtures V2 : mini Vault synthetique en memoire (jamais le vrai Vault)."""

import sqlite3

import vault_intelligence as vi
from vault_db import SCHEMA as V2_SCHEMA

NOW = "2026-09-28T00:00:00+00:00"


def make_db(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(V2_SCHEMA)
    vi.ensure_schema(conn)
    return conn


def add_source(conn, url, title, *, author="auteur.test", transcript=None,
               description=None, category=None, captured_at=None, tags=()):
    cur = conn.execute(
        """
        INSERT INTO contents(
            source_url, platform, author, title, description_original,
            transcript, category, created_at, updated_at
        ) VALUES (?, 'tiktok', ?, ?, ?, ?, ?, ?, ?)
        """,
        (url, author, title, description, transcript, category, NOW, NOW),
    )
    content_id = cur.lastrowid
    for tag in tags:
        conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (tag,)).fetchone()[0]
        conn.execute(
            "INSERT INTO content_tags(content_id, tag_id) VALUES (?, ?)", (content_id, tag_id)
        )
    source_id = vi.upsert_source(
        conn,
        source_url=url,
        platform="tiktok",
        source_type="video",
        title=title,
        author=author,
        content_id=content_id,
        captured_at=captured_at or NOW,
    )
    conn.commit()
    return source_id


def claim(claim_text, subject, predicate, obj, evidence, *, kind="fact",
          modality="asserted", confidence=0.85, polarity="affirmative",
          evidence_type="unspecified", scope=None):
    item = {
        "claim_text": claim_text,
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "polarity": polarity,
        "kind": kind,
        "modality": modality,
        "confidence": confidence,
        "evidence_type": evidence_type,
        "evidence": evidence,
    }
    if scope:
        item["scope"] = scope
    return item
