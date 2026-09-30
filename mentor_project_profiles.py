#!/usr/bin/env python3
"""
mentor_project_profiles.py - Profils persistants et editables des projets.

Le Mentor sait ce qu'est un projet grace aux DONNEES de vault.db, pas au code :
les regles generiques de score restent dans mentor_project_links.py, les
connaissances metier sur chaque projet vivent ici.

Table additive `mentor_project_profiles`, une ligne par projet existant
(reference intelligence_entities ; aucune nouvelle notion de projet).
intelligence_entities.summary / metadata ne sont pas reutilises : texte libre
et JSON d'identite deja ecrits par d'autres modules.

Format des sujets (core_topics, context_topics, specific_tools) : liste de
chaines ou d'objets {"topic": ..., "terms": [...], "provenance": ...}.
`terms` = variantes reconnues (pluriels, traductions) d'une MEME idee.
excluded_topics : objets {"term": ..., "mode": "alone_insufficient"|"block", "note": ...}
  - alone_insufficient : ne compte jamais comme sujet du projet ;
  - block : sa presence dans la source empeche toute proposition (hors mention explicite).

Usage :
    python mentor_project_profiles.py --list
    python mentor_project_profiles.py --export profils.json
    python mentor_project_profiles.py --import profils.json      # edition
"""

import argparse
import json
import sys
from pathlib import Path

import mentor_proactive_store as pstore

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "vault.db"

LIST_FIELDS = ("core_topics", "context_topics", "named_entities", "specific_tools",
               "excluded_topics", "compound_rules", "advisors")
EXCLUSION_MODES = ("alone_insufficient", "block", "defer")
RULE_LEVELS = ("medium",)

# Colonnes ajoutees apres la creation de la table (migration additive, idempotente).
EXTRA_COLUMNS = (
    ("compound_rules", "TEXT NOT NULL DEFAULT '[]'"),
    ("advisors", "TEXT NOT NULL DEFAULT '[]'"),
    ("active", "INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))"),
)
PROFILE_STATUSES = ("complete", "incomplete")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mentor_project_profiles (
    entity_id       INTEGER PRIMARY KEY
                    REFERENCES intelligence_entities(id) ON DELETE CASCADE,
    project_name    TEXT NOT NULL,
    description     TEXT,
    core_topics     TEXT NOT NULL DEFAULT '[]',
    context_topics  TEXT NOT NULL DEFAULT '[]',
    named_entities  TEXT NOT NULL DEFAULT '[]',
    specific_tools  TEXT NOT NULL DEFAULT '[]',
    excluded_topics TEXT NOT NULL DEFAULT '[]',
    profile_status  TEXT NOT NULL DEFAULT 'incomplete'
                    CHECK (profile_status IN ('complete', 'incomplete')),
    profile_source  TEXT NOT NULL,
    notes           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


class ProfileError(ValueError):
    pass


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(mentor_project_profiles)")}
    for name, declaration in EXTRA_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE mentor_project_profiles ADD COLUMN {name} {declaration}")
    conn.commit()


def schema_present(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mentor_project_profiles'"
    ).fetchone() is not None


def _normalize_topics(items, default_provenance):
    topics = []
    for item in items or []:
        if isinstance(item, str):
            item = {"topic": item}
        if not isinstance(item, dict) or not str(item.get("topic") or "").strip():
            raise ProfileError(f"sujet invalide : {item!r}")
        topic = item["topic"].strip()
        # `terms` explicites = seules formes reconnues (ex. « Make » : make.com, pas le verbe).
        terms = [str(t).strip() for t in (item.get("terms") or [topic]) if str(t).strip()]
        topics.append({"topic": topic, "terms": terms,
                       "provenance": item.get("provenance") or default_provenance})
    return topics


def _normalize_exclusions(items):
    """
    alone_insufficient : le terme ne compte jamais comme sujet du projet ;
    block             : sa presence empeche toute proposition (hors mention explicite) ;
    defer             : {"project": canonical_key} - si la source est deja clairement
                        rattachee a ce projet (lien reel, HIGH ou MEDIUM), ce projet-ci s'efface.
    """
    result = []
    for item in items or []:
        if isinstance(item, str):
            item = {"term": item}
        mode = item.get("mode") or "alone_insufficient"
        if mode not in EXCLUSION_MODES:
            raise ProfileError(f"exclusion invalide : {item!r}")
        if mode == "defer":
            if not str(item.get("project") or "").strip():
                raise ProfileError(f"exclusion defer sans projet : {item!r}")
            result.append({"mode": mode, "project": item["project"].strip(),
                           "note": item.get("note")})
            continue
        if not str(item.get("term") or "").strip():
            raise ProfileError(f"exclusion invalide : {item!r}")
        result.append({"term": item["term"].strip(), "mode": mode,
                       "note": item.get("note")})
    return result


def _normalize_rules(items, default_provenance):
    """
    Regle composee : un sujet principal (au moins un de `primary_any`) plus au moins
    `context_min` contextes metier DISTINCTS parmi `context_any` -> niveau `level`.
    La mecanique est generique (mentor_project_links) ; le contenu est une donnee.
    """
    rules = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("name"):
            raise ProfileError(f"regle composee invalide : {item!r}")
        level = item.get("level") or "medium"
        if level not in RULE_LEVELS:
            raise ProfileError(f"niveau de regle non autorise : {level!r}")
        context_min = int(item.get("context_min") or 2)
        primary = _normalize_topics(item.get("primary_any"), default_provenance)
        context = _normalize_topics(item.get("context_any"), default_provenance)
        if not primary or len(context) < context_min or context_min < 1:
            raise ProfileError(f"regle composee incomplete : {item.get('name')!r}")
        rules.append({"name": item["name"], "primary_any": primary, "context_any": context,
                      "context_min": context_min, "level": level, "note": item.get("note")})
    return rules


def normalize(profile):
    source = profile.get("profile_source") or "rosy"
    normalized = {
        "description": profile.get("description"),
        "core_topics": _normalize_topics(profile.get("core_topics"), source),
        "context_topics": _normalize_topics(profile.get("context_topics"), source),
        "specific_tools": _normalize_topics(profile.get("specific_tools"), source),
        "named_entities": [str(n).strip() for n in profile.get("named_entities") or []
                           if str(n).strip()],
        "excluded_topics": _normalize_exclusions(profile.get("excluded_topics")),
        "compound_rules": _normalize_rules(profile.get("compound_rules"), source),
        "advisors": [str(a).strip() for a in profile.get("advisors") or [] if str(a).strip()],
        "active": 0 if profile.get("active") in (False, 0, "0") else 1,
        "profile_source": source,
        "notes": profile.get("notes"),
    }
    status = profile.get("profile_status")
    if status is None:
        status = "complete" if normalized["description"] and normalized["core_topics"] \
            else "incomplete"
    if status not in PROFILE_STATUSES:
        raise ProfileError(f"profile_status invalide : {status!r}")
    normalized["profile_status"] = status
    return normalized


def upsert_profile(conn, entity_id, profile, *, overwrite=True, at=None):
    """Cree ou remplace le profil d'un projet existant. Retourne 'created'/'updated'/'kept'."""
    ensure_schema(conn)
    entity = conn.execute(
        "SELECT id, name FROM intelligence_entities WHERE id = ?", (entity_id,)
    ).fetchone()
    if entity is None:
        raise ProfileError(f"entite {entity_id} absente : un profil reference un projet existant")
    data = normalize(profile)
    at = at or pstore.now_iso()
    existing = conn.execute(
        "SELECT entity_id FROM mentor_project_profiles WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    if existing and not overwrite:
        return "kept"
    columns = ["project_name", "description", *LIST_FIELDS, "active", "profile_status",
               "profile_source", "notes"]
    values = [
        entity[1], data["description"],
        *(pstore.dumps(data[field]) for field in LIST_FIELDS),
        data["active"], data["profile_status"], data["profile_source"], data["notes"],
    ]
    if existing:
        conn.execute(
            "UPDATE mentor_project_profiles SET "
            + ", ".join(f"{c} = ?" for c in columns)
            + ", updated_at = ? WHERE entity_id = ?",
            (*values, at, entity_id),
        )
        outcome = "updated"
    else:
        conn.execute(
            f"INSERT INTO mentor_project_profiles ({', '.join(columns)}, entity_id, "
            f"created_at, updated_at) VALUES ({', '.join('?' for _ in columns)}, ?, ?, ?)",
            (*values, entity_id, at, at),
        )
        outcome = "created"
    conn.commit()
    return outcome


def get_profile(conn, entity_id):
    if not schema_present(conn):
        return None
    columns = {row[1] for row in conn.execute("PRAGMA table_info(mentor_project_profiles)")}
    if any(name not in columns for name, _ in EXTRA_COLUMNS):
        # Base plus ancienne en lecture (dry-run) : valeurs par defaut.
        row = conn.execute(
            "SELECT * FROM mentor_project_profiles WHERE entity_id = ?", (entity_id,)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item.setdefault("compound_rules", "[]")
        item.setdefault("advisors", "[]")
        item.setdefault("active", 1)
        for field in LIST_FIELDS:
            item[field] = pstore.loads(item[field], [])
        return item
    row = conn.execute(
        "SELECT * FROM mentor_project_profiles WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    for field in LIST_FIELDS:
        item[field] = pstore.loads(item[field], [])
    return item


def list_profiles(conn):
    if not schema_present(conn):
        return []
    return [get_profile(conn, row[0]) for row in conn.execute(
        "SELECT entity_id FROM mentor_project_profiles ORDER BY entity_id").fetchall()]


def export_profiles(conn):
    return [
        {k: v for k, v in p.items() if k not in ("created_at", "updated_at")}
        for p in list_profiles(conn)
    ]


def import_profiles(conn, profiles):
    return {p["entity_id"]: upsert_profile(conn, p["entity_id"], p) for p in profiles}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Profils des projets du Mentor")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true")
    group.add_argument("--export", metavar="FICHIER")
    group.add_argument("--import", dest="import_file", metavar="FICHIER")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    from vault_db import connect

    conn = connect(args.db)
    try:
        if args.list:
            for p in list_profiles(conn):
                print(f"#{p['entity_id']:<4} {p['project_name']:<35} {p['profile_status']:<11}"
                      f" core {len(p['core_topics']):>2}  contexte {len(p['context_topics']):>2}"
                      f"  noms {len(p['named_entities'])}  outils {len(p['specific_tools'])}"
                      f"  exclusions {len(p['excluded_topics'])}")
        elif args.export:
            Path(args.export).write_text(
                json.dumps(export_profiles(conn), ensure_ascii=False, indent=2),
                encoding="utf-8")
            print(f"Profils exportes : {args.export}")
        else:
            data = json.loads(Path(args.import_file).read_text(encoding="utf-8"))
            print(json.dumps(import_profiles(conn, data), indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
