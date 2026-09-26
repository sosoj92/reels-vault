#!/usr/bin/env python3
"""
vault_db.py - Coeur du Reels Vault : acces a vault.db (SQLite), source de verite.

Tout ce qui lit ou ecrit les donnees du Vault passe par ce module :
migrate_to_db.py, vault_api.py et les connecteurs de connectors/.
Il n'utilise que la bibliotheque standard Python.

Regles metier appliquees ici (et donc partout) :
- description effective = description_manual > description_ai > description_original ;
- description_original n'est jamais modifiee apres import ;
- un champ modifie par l'utilisateur (titre, categorie, sous-themes, tags) est
  verrouille : ni la migration ni une IA ne peuvent l'ecraser ensuite ;
- une source indisponible ne supprime jamais une fiche ;
- une action n'est envoyee vers un outil externe qu'apres validation.
"""

import json
import os
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

RACINE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("REELS_VAULT_DB") or RACINE / "vault.db")
SCHEMA_VERSION = 2

SOURCE_STATUSES = ("available", "unavailable", "unknown")
ACTION_STATUSES = ("proposed", "approved", "todo", "doing", "done", "dismissed", "archived")
# Statuts a partir desquels une action est consideree comme validee par l'utilisateur.
ACTION_STATUSES_VALIDATED = ("approved", "todo", "doing", "done")
ACTION_TYPES = (
    "tester", "rechercher", "comparer", "utiliser", "intégrer", "partager",
    "acheter", "approfondir", "lire", "regarder", "autre",
)
ACTION_TYPES_SUGGESTED = ACTION_TYPES  # compatibilite
RESOURCE_TYPES = (
    "outil", "logiciel", "application", "site", "personne", "livre",
    "formation", "service", "méthode", "lieu", "autre",
)
# Champs qu'une modification humaine verrouille contre les ecrasements automatiques.
LOCKABLE_FIELDS = ("title", "category", "subthemes", "tags")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS contents (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url           TEXT NOT NULL UNIQUE,
    platform             TEXT,
    content_type         TEXT,
    author               TEXT,
    title                TEXT,
    description_original TEXT,
    description_ai       TEXT,
    description_manual   TEXT,
    transcript           TEXT,
    category             TEXT,
    source_status        TEXT NOT NULL DEFAULT 'unknown'
                         CHECK (source_status IN ('available', 'unavailable', 'unknown')),
    source_file          TEXT UNIQUE,
    duration_s           INTEGER,
    ingested_at          TEXT,
    source_checked_at    TEXT,
    locked_fields        TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subthemes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,
    category TEXT
);

CREATE TABLE IF NOT EXISTS content_subthemes (
    content_id  INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    subtheme_id INTEGER NOT NULL REFERENCES subthemes(id) ON DELETE CASCADE,
    PRIMARY KEY (content_id, subtheme_id)
);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS content_tags (
    content_id INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (content_id, tag_id)
);

CREATE TABLE IF NOT EXISTS media (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'image',
    position   INTEGER,
    UNIQUE (content_id, path)
);

CREATE TABLE IF NOT EXISTS resources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id  INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    url         TEXT,
    type        TEXT NOT NULL DEFAULT 'autre',
    description TEXT,
    edited_by_user INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (content_id, name)
);

CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id        INTEGER REFERENCES contents(id) ON DELETE SET NULL,
    title             TEXT NOT NULL,
    description       TEXT,
    action_type       TEXT,
    status            TEXT NOT NULL DEFAULT 'proposed'
                      CHECK (status IN ('proposed', 'approved', 'todo', 'doing', 'done', 'dismissed', 'archived')),
    priority          INTEGER CHECK (priority BETWEEN 1 AND 4),
    due_date          TEXT,
    proposed_by       TEXT NOT NULL DEFAULT 'user',
    resource_id       INTEGER REFERENCES resources(id) ON DELETE SET NULL,
    external_provider TEXT,
    external_task_id  TEXT,
    external_url      TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    ended_at   TEXT NOT NULL,
    summary    TEXT,
    stats      TEXT,
    created_at TEXT NOT NULL
);

-- Correspondances avec les outils externes (tout type d'objet, tout fournisseur).
CREATE TABLE IF NOT EXISTS external_links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type  TEXT NOT NULL CHECK (entity_type IN ('content', 'action', 'resource', 'note')),
    entity_id    INTEGER NOT NULL,
    provider     TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    external_url TEXT,
    synced_at    TEXT NOT NULL,
    UNIQUE (entity_type, entity_id, provider)
);

-- Historique des modifications (descriptions, titre, classification) : rien n'est perdu.
CREATE TABLE IF NOT EXISTS content_revisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    edited_by  TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Journal des exports / envois vers les connecteurs (statut de synchronisation).
CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider    TEXT NOT NULL,
    operation   TEXT NOT NULL,
    entity_type TEXT,
    entity_id   INTEGER,
    status      TEXT NOT NULL CHECK (status IN ('ok', 'error', 'not_configured')),
    detail      TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contents_created  ON contents(created_at);
CREATE INDEX IF NOT EXISTS idx_resources_content ON resources(content_id);
CREATE INDEX IF NOT EXISTS idx_notes_content     ON notes(content_id);
CREATE INDEX IF NOT EXISTS idx_actions_content   ON actions(content_id);
CREATE INDEX IF NOT EXISTS idx_actions_status    ON actions(status);
"""


class VaultError(Exception):
    """Erreur metier (donnee absente, valeur invalide, champ verrouille...)."""

    def __init__(self, message, code="invalid"):
        super().__init__(message)
        self.code = code  # "not_found", "invalid", "locked", "not_validated"


# ------------------------------------------------------------------ utilitaires

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fold(texte):
    """Minuscules sans accents, pour une recherche tolerante."""
    texte = unicodedata.normalize("NFD", texte or "")
    return "".join(c for c in texte if unicodedata.category(c) != "Mn").lower()


def connect(path=None):
    """Ouvre vault.db et cree/complete le schema si besoin."""
    # check_same_thread=False : l'API ouvre une connexion par requete, que FastAPI peut
    # utiliser depuis un autre thread du pool ; elle n'est jamais partagee entre requetes.
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.create_function("fold", 1, fold, deterministic=True)
    conn.executescript(SCHEMA)
    _upgrade(conn)
    conn.commit()
    return conn


# Colonnes ajoutees apres la v1 : ajoutees a une base existante sans perte de donnees.
_COLONNES_V2 = {
    "actions": [("resource_id", "INTEGER REFERENCES resources(id) ON DELETE SET NULL"),
                ("external_url", "TEXT")],
    "resources": [("edited_by_user", "INTEGER NOT NULL DEFAULT 0")],
}


def _upgrade(conn):
    for table, colonnes in _COLONNES_V2.items():
        existantes = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for nom, decl in colonnes:
            if nom not in existantes:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {nom} {decl}")
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(SCHEMA_VERSION),))


def _one(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _all(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _check(valeur, autorisees, champ):
    if valeur is not None and valeur not in autorisees:
        raise VaultError(f"{champ} invalide : {valeur!r} (attendu : {', '.join(autorisees)})")


# ------------------------------------------------------------------ taxonomie

def _id_nom(conn, table, nom, extra=None):
    row = conn.execute(f"SELECT id FROM {table} WHERE name = ?", (nom,)).fetchone()
    if row:
        return row["id"]
    if table == "subthemes":
        cur = conn.execute("INSERT INTO subthemes(name, category) VALUES (?, ?)", (nom, extra))
    else:
        cur = conn.execute(f"INSERT INTO {table}(name) VALUES (?)", (nom,))
    return cur.lastrowid


def ensure_subtheme(conn, nom, category=None):
    sid = _id_nom(conn, "subthemes", nom, category)
    if category:
        conn.execute("UPDATE subthemes SET category = ? WHERE id = ? AND category IS NULL", (category, sid))
    return sid


def set_subthemes(conn, content_id, noms, category=None):
    conn.execute("DELETE FROM content_subthemes WHERE content_id = ?", (content_id,))
    for nom in dict.fromkeys(n.strip() for n in noms if n and n.strip()):
        conn.execute(
            "INSERT OR IGNORE INTO content_subthemes VALUES (?, ?)",
            (content_id, ensure_subtheme(conn, nom, category)),
        )


def set_tags(conn, content_id, noms):
    conn.execute("DELETE FROM content_tags WHERE content_id = ?", (content_id,))
    for nom in dict.fromkeys(n.strip() for n in noms if n and n.strip()):
        conn.execute(
            "INSERT OR IGNORE INTO content_tags VALUES (?, ?)",
            (content_id, _id_nom(conn, "tags", nom)),
        )


def get_taxonomy(conn):
    """Categories -> sous-themes, avec les compteurs de contenus, et les tags."""
    cats = {}
    for r in _all(conn, "SELECT category, COUNT(*) n FROM contents WHERE category IS NOT NULL GROUP BY category"):
        cats[r["category"]] = {"name": r["category"], "count": r["n"], "subthemes": []}
    for r in _all(conn, """
        SELECT s.name, s.category, COUNT(cs.content_id) n FROM subthemes s
        LEFT JOIN content_subthemes cs ON cs.subtheme_id = s.id
        GROUP BY s.id ORDER BY s.name"""):
        cat = cats.setdefault(r["category"] or "", {"name": r["category"], "count": 0, "subthemes": []})
        cat["subthemes"].append({"name": r["name"], "count": r["n"]})
    tags = _all(conn, """
        SELECT t.name, COUNT(ct.content_id) count FROM tags t
        LEFT JOIN content_tags ct ON ct.tag_id = t.id
        GROUP BY t.id ORDER BY count DESC, t.name""")
    return {"categories": list(cats.values()), "tags": tags}


# ------------------------------------------------------------------ contenus

def _locked(row):
    return set(json.loads(row["locked_fields"] or "[]"))


def upsert_content_from_import(conn, data):
    """
    Cree ou complete un contenu a partir des fichiers (raw/, index.md).
    Idempotent. Ne touche jamais description_manual, les notes, les actions,
    ni les champs verrouilles par l'utilisateur. description_original n'est
    ecrite que si elle est vide.
    Retourne (id, "created" | "updated" | "unchanged").
    """
    t = now()
    row = conn.execute(
        "SELECT * FROM contents WHERE source_url = ? OR (source_file IS NOT NULL AND source_file = ?)",
        (data["source_url"], data.get("source_file")),
    ).fetchone()

    if row is None:
        cur = conn.execute(
            """INSERT INTO contents (source_url, platform, content_type, author, title,
                   description_original, description_ai, transcript, category, source_file,
                   duration_s, ingested_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data["source_url"], data.get("platform"), data.get("content_type"), data.get("author"),
             data.get("title"), data.get("description_original"), data.get("description_ai"),
             data.get("transcript"), data.get("category"), data.get("source_file"),
             data.get("duration_s"), data.get("ingested_at"), t, t),
        )
        cid = cur.lastrowid
        set_subthemes(conn, cid, data.get("subthemes", []), data.get("category"))
        set_tags(conn, cid, data.get("tags", []))
        _import_resources(conn, cid, data.get("resources", []))
        _import_media(conn, cid, data.get("media", []))
        return cid, "created"

    cid, locked = row["id"], _locked(row)
    avant = _snapshot(conn, cid)
    maj = {}
    for champ in ("platform", "content_type", "author", "transcript", "source_file",
                  "duration_s", "ingested_at", "description_ai"):
        if data.get(champ) not in (None, "") and data[champ] != row[champ]:
            maj[champ] = data[champ]
    if not row["description_original"] and data.get("description_original"):
        maj["description_original"] = data["description_original"]
    for champ in ("title", "category"):
        if champ not in locked and data.get(champ) and data[champ] != row[champ]:
            maj[champ] = data[champ]
    if maj:
        cols = ", ".join(f"{c} = ?" for c in maj)
        conn.execute(f"UPDATE contents SET {cols} WHERE id = ?", (*maj.values(), cid))

    categorie = maj.get("category", row["category"])
    if "subthemes" not in locked and data.get("subthemes"):
        set_subthemes(conn, cid, data["subthemes"], categorie)
    if "tags" not in locked and data.get("tags"):
        set_tags(conn, cid, data["tags"])
    _import_resources(conn, cid, data.get("resources", []))
    _import_media(conn, cid, data.get("media", []))

    if _snapshot(conn, cid) == avant:
        return cid, "unchanged"
    conn.execute("UPDATE contents SET updated_at = ? WHERE id = ?", (t, cid))
    return cid, "updated"


def _snapshot(conn, cid):
    return (
        _one(conn, "SELECT * FROM contents WHERE id = ?", (cid,)),
        _all(conn, "SELECT subtheme_id FROM content_subthemes WHERE content_id = ? ORDER BY 1", (cid,)),
        _all(conn, "SELECT tag_id FROM content_tags WHERE content_id = ? ORDER BY 1", (cid,)),
        _all(conn, "SELECT id, type, description FROM resources WHERE content_id = ? ORDER BY 1", (cid,)),
        _all(conn, "SELECT id FROM media WHERE content_id = ? ORDER BY 1", (cid,)),
    )


def _import_resources(conn, cid, ressources):
    """
    Ajoute les ressources manquantes. Une ressource existante n'est completee
    (type encore 'autre', description vide) que si l'utilisateur ne l'a pas modifiee.
    """
    t = now()
    for r in ressources:
        conn.execute(
            """INSERT OR IGNORE INTO resources (content_id, name, url, type, description, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (cid, r["name"], r.get("url"), r.get("type") or "autre", r.get("description"), t, t),
        )
        if r.get("type") and r["type"] != "autre":
            conn.execute("""UPDATE resources SET type = ?, updated_at = ? WHERE content_id = ? AND name = ?
                            AND type = 'autre' AND edited_by_user = 0""", (r["type"], t, cid, r["name"]))
        if r.get("description"):
            conn.execute("""UPDATE resources SET description = ?, updated_at = ? WHERE content_id = ? AND name = ?
                            AND description IS NULL AND edited_by_user = 0""", (r["description"], t, cid, r["name"]))


def _import_media(conn, cid, medias):
    for pos, chemin in enumerate(medias, 1):
        conn.execute(
            "INSERT OR IGNORE INTO media (content_id, path, kind, position) VALUES (?, ?, 'image', ?)",
            (cid, chemin, pos),
        )


def _enrich(conn, row, full=False):
    """Transforme une ligne contents en objet JSON stable."""
    c = dict(row)
    cid = c["id"]
    c["locked_fields"] = json.loads(c["locked_fields"] or "[]")
    if c["description_manual"]:
        c["description"], c["description_source"] = c["description_manual"], "manual"
    elif c["description_ai"]:
        c["description"], c["description_source"] = c["description_ai"], "ai"
    else:
        c["description"], c["description_source"] = c["description_original"], "original"
    c["subthemes"] = [r["name"] for r in conn.execute(
        "SELECT s.name FROM subthemes s JOIN content_subthemes cs ON cs.subtheme_id = s.id "
        "WHERE cs.content_id = ? ORDER BY s.name", (cid,))]
    c["tags"] = [r["name"] for r in conn.execute(
        "SELECT t.name FROM tags t JOIN content_tags ct ON ct.tag_id = t.id "
        "WHERE ct.content_id = ? ORDER BY t.name", (cid,))]
    c["resources_count"] = conn.execute("SELECT COUNT(*) FROM resources WHERE content_id = ?", (cid,)).fetchone()[0]
    c["actions_count"] = conn.execute("SELECT COUNT(*) FROM actions WHERE content_id = ? AND status != 'dismissed'",
                                      (cid,)).fetchone()[0]
    vignette = conn.execute("SELECT path FROM media WHERE content_id = ? ORDER BY position LIMIT 1", (cid,)).fetchone()
    c["thumbnail"] = vignette["path"] if vignette else None
    if full:
        c["revisions"] = _all(conn, "SELECT field, old_value, new_value, edited_by, created_at FROM content_revisions "
                                    "WHERE content_id = ? ORDER BY id DESC", (cid,))
        c["resources"] = _all(conn, "SELECT * FROM resources WHERE content_id = ? ORDER BY id", (cid,))
        c["notes"] = _all(conn, "SELECT * FROM notes WHERE content_id = ? ORDER BY created_at", (cid,))
        c["actions"] = [_with_lifecycle(a) for a in
                        _all(conn, "SELECT * FROM actions WHERE content_id = ? ORDER BY id", (cid,))]
        c["media"] = _all(conn, "SELECT path, kind, position FROM media WHERE content_id = ? ORDER BY position", (cid,))
        c["external_links"] = _all(conn,
            "SELECT provider, external_id, external_url, synced_at FROM external_links "
            "WHERE entity_type = 'content' AND entity_id = ?", (cid,))
    else:
        c.pop("transcript", None)
    return c


def get_content(conn, cid, full=True):
    row = conn.execute("SELECT * FROM contents WHERE id = ?", (cid,)).fetchone()
    if not row:
        raise VaultError(f"contenu {cid} introuvable", "not_found")
    return _enrich(conn, row, full)


def list_contents(conn, limit=50, offset=0, category=None, subtheme=None, tag=None,
                  platform=None, since=None, order="recent"):
    where, params = [], []
    if category:
        where.append("c.category = ?"); params.append(category)
    if platform:
        where.append("c.platform = ?"); params.append(platform)
    if since:
        where.append("c.created_at > ?"); params.append(since)
    if subtheme:
        where.append("c.id IN (SELECT cs.content_id FROM content_subthemes cs "
                     "JOIN subthemes s ON s.id = cs.subtheme_id WHERE s.name = ?)"); params.append(subtheme)
    if tag:
        where.append("c.id IN (SELECT ct.content_id FROM content_tags ct "
                     "JOIN tags t ON t.id = ct.tag_id WHERE t.name = ?)"); params.append(tag)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    tri = "c.created_at DESC, c.id DESC" if order == "recent" else "c.id"
    total = conn.execute(f"SELECT COUNT(*) FROM contents c {clause}", params).fetchone()[0]
    rows = conn.execute(f"SELECT c.* FROM contents c {clause} ORDER BY {tri} LIMIT ? OFFSET ?",
                        (*params, limit, offset)).fetchall()
    return {"total": total, "items": [_enrich(conn, r) for r in rows]}


def search(conn, q, limit=50):
    """Recherche plein texte tolerante (accents, casse) : tous les mots doivent apparaitre."""
    mots = [m for m in fold(q).split() if m]
    if not mots:
        return {"query": q, "total": 0, "items": []}
    doc = """fold(
        coalesce(c.title,'') || ' ' || coalesce(c.author,'') || ' ' || coalesce(c.category,'') || ' ' ||
        coalesce(c.description_original,'') || ' ' || coalesce(c.description_ai,'') || ' ' ||
        coalesce(c.description_manual,'') || ' ' || coalesce(c.transcript,'') || ' ' ||
        coalesce((SELECT group_concat(s.name, ' ') FROM subthemes s JOIN content_subthemes cs
                  ON cs.subtheme_id = s.id WHERE cs.content_id = c.id), '') || ' ' ||
        coalesce((SELECT group_concat(t.name, ' ') FROM tags t JOIN content_tags ct
                  ON ct.tag_id = t.id WHERE ct.content_id = c.id), '') || ' ' ||
        coalesce((SELECT group_concat(r.name || ' ' || coalesce(r.description,''), ' ')
                  FROM resources r WHERE r.content_id = c.id), '') || ' ' ||
        coalesce((SELECT group_concat(n.text, ' ') FROM notes n WHERE n.content_id = c.id), ''))"""
    cond = " AND ".join(f"instr({doc}, ?) > 0" for _ in mots)
    rows = conn.execute(
        f"SELECT c.* FROM contents c WHERE {cond} ORDER BY c.created_at DESC LIMIT ?",
        (*mots, limit),
    ).fetchall()
    return {"query": q, "total": len(rows), "items": [_enrich(conn, r) for r in rows]}


def _revision(conn, cid, field, old, new, edited_by):
    if isinstance(old, list) or isinstance(new, list):
        old, new = json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False)
    if old != new:
        conn.execute("INSERT INTO content_revisions (content_id, field, old_value, new_value, edited_by, created_at) "
                     "VALUES (?,?,?,?,?,?)", (cid, field, old, new, edited_by, now()))


def update_content(conn, cid, changes, edited_by="user"):
    """
    Modifie un contenu. edited_by = "user" verrouille les champs modifies ;
    edited_by = "ai" ne peut ni toucher description_manual ni un champ verrouille.
    Chaque modification est historisee dans content_revisions.
    """
    row = conn.execute("SELECT * FROM contents WHERE id = ?", (cid,)).fetchone()
    if not row:
        raise VaultError(f"contenu {cid} introuvable", "not_found")
    _check(edited_by, ("user", "ai"), "edited_by")
    locked = _locked(row)
    permis = {"title", "description_manual", "description_ai", "category", "subthemes",
              "tags", "source_status"}
    inconnus = set(changes) - permis
    if inconnus:
        raise VaultError(f"champ(s) non modifiable(s) : {', '.join(sorted(inconnus))}")
    if "source_status" in changes:
        _check(changes["source_status"], SOURCE_STATUSES, "source_status")
    if len(changes.get("subthemes") or []) > 3:
        raise VaultError("3 sous-themes maximum")
    if edited_by == "ai":
        if "description_manual" in changes:
            raise VaultError("une IA ne peut pas ecrire description_manual (utiliser description_ai)", "locked")
        bloques = locked & set(changes)
        if bloques:
            raise VaultError(f"champ(s) verrouille(s) par l'utilisateur : {', '.join(sorted(bloques))}", "locked")

    if "description_manual" in changes and not (changes["description_manual"] or "").strip():
        changes = {**changes, "description_manual": None}
    avant = _enrich(conn, row, full=False)
    for champ, valeur in changes.items():
        _revision(conn, cid, champ, avant.get(champ), valeur, edited_by)

    t = now()
    cols = {k: v for k, v in changes.items() if k not in ("subthemes", "tags")}
    if "source_status" in cols:
        cols["source_checked_at"] = t
    if edited_by == "user":
        locked |= {k for k in changes if k in LOCKABLE_FIELDS}
        cols["locked_fields"] = json.dumps(sorted(locked))
    cols["updated_at"] = t
    conn.execute(f"UPDATE contents SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                 (*cols.values(), cid))
    categorie = changes.get("category", row["category"])
    if "subthemes" in changes:
        set_subthemes(conn, cid, changes["subthemes"], categorie)
    if "tags" in changes:
        set_tags(conn, cid, changes["tags"])
    conn.commit()
    return get_content(conn, cid)


def revert_description(conn, cid):
    """Revient a la description IA (ou originale) : description_manual est videe, l'ancienne reste dans l'historique."""
    return update_content(conn, cid, {"description_manual": None}, "user")


def unreviewed_contents(conn, limit=100):
    """Contenus ajoutes depuis le dernier bilan."""
    return list_contents(conn, limit=limit, since=last_review_at(conn))


def get_stats(conn):
    """Indicateurs du tableau de bord."""
    def q(sql, p=()):
        return conn.execute(sql, p).fetchone()[0]

    since = last_review_at(conn)
    par_statut = {s: 0 for s in ACTION_STATUSES}
    for r in conn.execute("SELECT status, COUNT(*) n FROM actions GROUP BY status"):
        par_statut[r["status"]] = r["n"]
    sans_action = """NOT EXISTS (SELECT 1 FROM actions a WHERE a.content_id = c.id AND a.status != 'dismissed')"""
    return {
        "contents": q("SELECT COUNT(*) FROM contents"),
        "contents_unavailable": q("SELECT COUNT(*) FROM contents WHERE source_status = 'unavailable'"),
        "resources": q("SELECT COUNT(*) FROM resources"),
        "resources_distinct": q("SELECT COUNT(DISTINCT fold(name)) FROM resources"),
        "actions_by_status": par_statut,
        "actions_proposed": par_statut["proposed"],
        "actions_validated": sum(par_statut[s] for s in ("approved", "todo", "doing")),
        "actions_done": par_statut["done"],
        "contents_without_action": q(f"SELECT COUNT(*) FROM contents c WHERE {sans_action}"),
        "contents_unexploited": q(f"""SELECT COUNT(*) FROM contents c WHERE {sans_action}
                                      AND NOT EXISTS (SELECT 1 FROM notes n WHERE n.content_id = c.id)"""),
        "new_since_last_review": q("SELECT COUNT(*) FROM contents WHERE created_at > ?", (since or "0000",)),
        "last_review_at": since,
        "categories": q("SELECT COUNT(DISTINCT category) FROM contents WHERE category IS NOT NULL"),
        "tags": q("SELECT COUNT(*) FROM tags"),
    }


def list_contents_without_action(conn, limit=20):
    rows = conn.execute("""SELECT c.* FROM contents c WHERE NOT EXISTS
        (SELECT 1 FROM actions a WHERE a.content_id = c.id AND a.status != 'dismissed')
        ORDER BY c.created_at DESC LIMIT ?""", (limit,)).fetchall()
    return {"total": len(rows), "items": [_enrich(conn, r) for r in rows]}


# ------------------------------------------------------------------ notes

def add_note(conn, cid, text):
    get_content(conn, cid, full=False)
    t = now()
    cur = conn.execute("INSERT INTO notes (content_id, text, created_at, updated_at) VALUES (?,?,?,?)",
                       (cid, text, t, t))
    conn.commit()
    return _one(conn, "SELECT * FROM notes WHERE id = ?", (cur.lastrowid,))


def update_note(conn, nid, text):
    if not conn.execute("UPDATE notes SET text = ?, updated_at = ? WHERE id = ?", (text, now(), nid)).rowcount:
        raise VaultError(f"note {nid} introuvable", "not_found")
    conn.commit()
    return _one(conn, "SELECT * FROM notes WHERE id = ?", (nid,))


def delete_note(conn, nid):
    """Supprime une note personnelle (uniquement sur demande explicite de l'utilisateur)."""
    if not conn.execute("DELETE FROM notes WHERE id = ?", (nid,)).rowcount:
        raise VaultError(f"note {nid} introuvable", "not_found")
    conn.commit()
    return {"deleted": nid}


# ------------------------------------------------------------------ ressources

def list_resources(conn, type=None, content_id=None, q=None, limit=200):
    where, params = [], []
    if type:
        where.append("r.type = ?"); params.append(type)
    if content_id:
        where.append("r.content_id = ?"); params.append(content_id)
    if q:
        where.append("instr(fold(r.name || ' ' || coalesce(r.description,'')), ?) > 0"); params.append(fold(q))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    items = _all(conn, f"""SELECT r.*, c.title AS content_title FROM resources r
                           JOIN contents c ON c.id = r.content_id {clause}
                           ORDER BY r.created_at DESC, r.id DESC LIMIT ?""", (*params, limit))
    return {"total": len(items), "items": items}


def resource_catalog(conn, type=None, q=None):
    """
    Ressources regroupees par nom (une meme ressource citee dans plusieurs contenus),
    avec les contenus sources et les actions associees.
    """
    groupes = {}
    for r in _all(conn, """SELECT r.*, c.title AS content_title, c.category AS content_category
                           FROM resources r JOIN contents c ON c.id = r.content_id ORDER BY r.created_at, r.id"""):
        cle = fold(r["name"]).strip()
        g = groupes.setdefault(cle, {
            "key": cle, "name": r["name"], "types": [], "descriptions": [], "url": None,
            "contents": [], "resource_ids": [], "actions": [],
            "first_seen": r["created_at"], "last_seen": r["created_at"],
        })
        g["resource_ids"].append(r["id"])
        if r["type"] not in g["types"]:
            g["types"].append(r["type"])
        if r["description"] and r["description"] not in g["descriptions"]:
            g["descriptions"].append(r["description"])
        g["url"] = g["url"] or r["url"]
        g["contents"].append({"id": r["content_id"], "title": r["content_title"],
                              "category": r["content_category"], "resource_id": r["id"]})
        g["last_seen"] = max(g["last_seen"], r["created_at"])
    types = {}
    for g in groupes.values():
        if len(g["types"]) > 1 and "autre" in g["types"]:
            g["types"].remove("autre")
        g["type"] = g["types"][0]
        types[g["type"]] = types.get(g["type"], 0) + 1
        g["mentions"] = len({c["id"] for c in g["contents"]})
        m = ",".join("?" * len(g["resource_ids"]))
        g["actions"] = _all(conn, f"""SELECT id, title, status, action_type, priority FROM actions
                                      WHERE resource_id IN ({m}) ORDER BY id""", g["resource_ids"])
    items = list(groupes.values())
    if type:
        items = [g for g in items if type in g["types"]]
    if q:
        f = fold(q)
        items = [g for g in items if f in fold(g["name"] + " " + " ".join(g["descriptions"]))]
    items.sort(key=lambda g: (-g["mentions"], fold(g["name"])))
    return {"total": len(items), "types": types, "items": items}


def get_resource(conn, rid):
    r = _one(conn, """SELECT r.*, c.title AS content_title, c.source_url AS content_source_url
                      FROM resources r JOIN contents c ON c.id = r.content_id WHERE r.id = ?""", (rid,))
    if not r:
        raise VaultError(f"ressource {rid} introuvable", "not_found")
    r["actions"] = _all(conn, "SELECT id, title, status, action_type FROM actions WHERE resource_id = ?", (rid,))
    return r


def add_resource(conn, cid, data):
    """Ajout manuel d'une ressource a un contenu (protegee ensuite contre la migration)."""
    get_content(conn, cid, full=False)
    _check(data.get("type") or "autre", RESOURCE_TYPES, "type")
    t = now()
    try:
        cur = conn.execute(
            """INSERT INTO resources (content_id, name, url, type, description, edited_by_user, created_at, updated_at)
               VALUES (?,?,?,?,?,1,?,?)""",
            (cid, data["name"].strip(), data.get("url"), data.get("type") or "autre", data.get("description"), t, t))
    except sqlite3.IntegrityError:
        raise VaultError(f"la ressource {data['name']!r} existe deja pour ce contenu")
    conn.commit()
    return get_resource(conn, cur.lastrowid)


def update_resource(conn, rid, changes):
    get_resource(conn, rid)
    inconnus = set(changes) - {"name", "url", "type", "description"}
    if inconnus:
        raise VaultError(f"champ(s) non modifiable(s) : {', '.join(sorted(inconnus))}")
    if "type" in changes:
        _check(changes["type"], RESOURCE_TYPES, "type")
    changes = {**changes, "edited_by_user": 1, "updated_at": now()}
    conn.execute(f"UPDATE resources SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?",
                 (*changes.values(), rid))
    conn.commit()
    return get_resource(conn, rid)


# ------------------------------------------------------------------ actions

_ACTION_SELECT = """SELECT a.*, c.title AS content_title, c.source_url AS content_source_url,
                           c.category AS content_category, r.name AS resource_name
                    FROM actions a LEFT JOIN contents c ON c.id = a.content_id
                    LEFT JOIN resources r ON r.id = a.resource_id"""

# Cycle de vie affiche a l'utilisateur, derive du statut stocke et de l'envoi externe :
#   proposed -> approved -> sent -> completed, ou ignored (et archived).
# "sent" = action validee (approved / todo / doing) dont une tache externe existe.
LIFECYCLES = ("proposed", "approved", "sent", "completed", "ignored", "archived")
_LIFECYCLE_SQL = {
    "proposed": "a.status = 'proposed'",
    "approved": "a.status IN ('approved', 'todo', 'doing') AND a.external_task_id IS NULL",
    "sent": "a.status IN ('approved', 'todo', 'doing') AND a.external_task_id IS NOT NULL",
    "completed": "a.status = 'done'",
    "ignored": "a.status = 'dismissed'",
    "archived": "a.status = 'archived'",
}


def lifecycle(a):
    s = a["status"]
    if s in ("approved", "todo", "doing"):
        return "sent" if a.get("external_task_id") else "approved"
    return {"done": "completed", "dismissed": "ignored"}.get(s, s)


def _with_lifecycle(a):
    a["lifecycle"] = lifecycle(a)
    return a


def list_actions(conn, status=None, content_id=None, since=None, limit=200, resource_id=None, lifecycle=None):
    where, params = [], []
    if lifecycle:
        etats = [x.strip() for x in lifecycle.split(",") if x.strip()]
        for x in etats:
            _check(x, LIFECYCLES, "lifecycle")
        where.append("(" + " OR ".join(f"({_LIFECYCLE_SQL[x]})" for x in etats) + ")")
    if status:
        statuts = [s.strip() for s in status.split(",") if s.strip()]
        for s in statuts:
            _check(s, ACTION_STATUSES, "status")
        where.append(f"a.status IN ({','.join('?' * len(statuts))})"); params += statuts
    if content_id:
        where.append("a.content_id = ?"); params.append(content_id)
    if resource_id:
        where.append("a.resource_id = ?"); params.append(resource_id)
    if since:
        where.append("(a.created_at > ? OR a.updated_at > ?)"); params += [since, since]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    items = _all(conn, f"""{_ACTION_SELECT} {clause}
                           ORDER BY coalesce(a.priority, 5), a.created_at DESC, a.id DESC LIMIT ?""",
                 (*params, limit))
    return {"total": len(items), "items": [_with_lifecycle(a) for a in items]}


def lifecycle_counts(conn):
    return {x: conn.execute(f"SELECT COUNT(*) FROM actions a WHERE {_LIFECYCLE_SQL[x]}").fetchone()[0]
            for x in LIFECYCLES}


def get_action(conn, aid):
    a = _one(conn, f"{_ACTION_SELECT} WHERE a.id = ?", (aid,))
    if not a:
        raise VaultError(f"action {aid} introuvable", "not_found")
    return _with_lifecycle(a)


def set_action_status(conn, aid, status):
    """Transition simple (valider, ignorer, terminer...) ; ne declenche jamais d'envoi externe."""
    return update_action(conn, aid, {"status": status})


def _check_action_refs(conn, data):
    """Verifie contenu / ressource / type ; une action liee a une ressource herite de son contenu."""
    if data.get("content_id") is not None:
        get_content(conn, data["content_id"], full=False)
    if data.get("resource_id") is not None:
        r = get_resource(conn, data["resource_id"])
        if data.get("content_id") is None:
            data["content_id"] = r["content_id"]
    if data.get("action_type") is not None:
        _check(data["action_type"], ACTION_TYPES, "action_type")
    return data


def create_action(conn, data):
    """Une action proposee par une IA est toujours creee au statut 'proposed'."""
    data = _check_action_refs(conn, dict(data))
    proposed_by = data.get("proposed_by") or "user"
    status = data.get("status") or "proposed"
    if proposed_by != "user":
        status = "proposed"
    _check(status, ACTION_STATUSES, "status")
    t = now()
    cur = conn.execute(
        """INSERT INTO actions (content_id, resource_id, title, description, action_type, status, priority,
               due_date, proposed_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (data.get("content_id"), data.get("resource_id"), data["title"], data.get("description"),
         data.get("action_type"), status, data.get("priority"), data.get("due_date"), proposed_by, t, t),
    )
    conn.commit()
    return get_action(conn, cur.lastrowid)


def update_action(conn, aid, changes):
    actuelle = get_action(conn, aid)
    permis = {"title", "description", "action_type", "status", "priority", "due_date", "content_id", "resource_id"}
    inconnus = set(changes) - permis
    if inconnus:
        raise VaultError(f"champ(s) non modifiable(s) : {', '.join(sorted(inconnus))}")
    if "status" in changes:
        _check(changes["status"], ACTION_STATUSES, "status")
    changes = dict(changes)
    if changes.get("resource_id") is not None and "content_id" not in changes:
        changes["content_id"] = None
        changes = _check_action_refs(conn, changes)
        if changes["content_id"] is None:
            changes["content_id"] = actuelle["content_id"]
    else:
        _check_action_refs(conn, changes)
    changes["updated_at"] = now()
    conn.execute(f"UPDATE actions SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?",
                 (*changes.values(), aid))
    conn.commit()
    return get_action(conn, aid)


# ------------------------------------------------------------------ liens externes / synchronisation

def get_external_link(conn, entity_type, entity_id, provider):
    return _one(conn, "SELECT * FROM external_links WHERE entity_type = ? AND entity_id = ? AND provider = ?",
                (entity_type, entity_id, provider))


def record_external_link(conn, entity_type, entity_id, provider, external_id, external_url=None):
    """Memorise l'identifiant externe (evite les doublons, permet les mises a jour)."""
    t = now()
    conn.execute(
        """INSERT INTO external_links (entity_type, entity_id, provider, external_id, external_url, synced_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT (entity_type, entity_id, provider)
           DO UPDATE SET external_id = excluded.external_id,
                         external_url = coalesce(excluded.external_url, external_links.external_url),
                         synced_at = excluded.synced_at""",
        (entity_type, entity_id, provider, external_id, external_url, t),
    )
    if entity_type == "action":
        conn.execute("""UPDATE actions SET external_provider = ?, external_task_id = ?,
                        external_url = coalesce(?, external_url), updated_at = ? WHERE id = ?""",
                     (provider, external_id, external_url, t, entity_id))
    conn.commit()


def remove_external_link(conn, entity_type, entity_id, provider):
    """Retire une correspondance devenue invalide (objet supprime chez le fournisseur)."""
    conn.execute("DELETE FROM external_links WHERE entity_type = ? AND entity_id = ? AND provider = ?",
                 (entity_type, entity_id, provider))
    if entity_type == "action":
        conn.execute("""UPDATE actions SET external_provider = NULL, external_task_id = NULL, external_url = NULL,
                        updated_at = ? WHERE id = ? AND external_provider = ?""", (now(), entity_id, provider))
    conn.commit()


def list_sync_log(conn, entity_type, entity_id, limit=20):
    return _all(conn, """SELECT provider, operation, status, detail, created_at FROM sync_log
                         WHERE entity_type = ? AND entity_id = ? ORDER BY id DESC LIMIT ?""",
                (entity_type, entity_id, limit))


def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (f"setting:{key}",)).fetchone()
    return r["value"] if r and r["value"] is not None else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (f"setting:{key}", value))
    conn.commit()


def log_sync(conn, provider, operation, status, entity_type=None, entity_id=None, detail=None):
    conn.execute("""INSERT INTO sync_log (provider, operation, entity_type, entity_id, status, detail, created_at)
                    VALUES (?,?,?,?,?,?,?)""",
                 (provider, operation, entity_type, entity_id, status, detail, now()))
    conn.commit()


def sync_status(conn):
    """Par fournisseur : derniere tentative, derniere reussite, dernier statut, objets lies."""
    res = {}

    def entree(p):
        return res.setdefault(p, {"provider": p, "last_attempt": None, "last_success": None, "operations": 0,
                                  "linked": 0, "last_status": None, "last_detail": None})

    for r in _all(conn, """SELECT provider, max(created_at) last_attempt,
                                  max(CASE WHEN status = 'ok' THEN created_at END) last_success,
                                  COUNT(*) operations FROM sync_log GROUP BY provider"""):
        entree(r["provider"]).update(r)
    for r in _all(conn, "SELECT provider, COUNT(*) n, max(synced_at) s FROM external_links GROUP BY provider"):
        e = entree(r["provider"])
        e["linked"] = r["n"]
        e["last_success"] = max(filter(None, [e["last_success"], r["s"]]), default=None)
    for p, e in res.items():
        dernier = _one(conn, "SELECT status, detail FROM sync_log WHERE provider = ? ORDER BY id DESC LIMIT 1", (p,))
        if dernier:
            e["last_status"], e["last_detail"] = dernier["status"], dernier["detail"]
    return res


# ------------------------------------------------------------------ bilans

def last_review_at(conn):
    return conn.execute("SELECT max(ended_at) FROM reviews").fetchone()[0]


OUTIL_TYPES = ("outil", "logiciel", "application", "site", "service")


def review_since_last(conn, until=None):
    """Bilan de tout ce qui s'est passe entre le dernier bilan et maintenant."""
    since = last_review_at(conn) or "0000"
    until = until or now()
    rng = (since, until)

    nouveaux = _all(conn, "SELECT id, title, category, platform, author, created_at FROM contents "
                          "WHERE created_at > ? AND created_at <= ? ORDER BY created_at", rng)
    ids = [c["id"] for c in nouveaux]
    ressources = _all(conn, """SELECT r.id, r.name, r.type, r.description, r.content_id, c.title AS content_title,
                                      (SELECT COUNT(*) FROM actions a WHERE a.resource_id = r.id
                                       AND a.status != 'dismissed') AS actions_count
                               FROM resources r JOIN contents c ON c.id = r.content_id
                               WHERE r.created_at > ? AND r.created_at <= ? ORDER BY r.name""", rng)
    par_type = {}
    for r in ressources:
        par_type[r["type"]] = par_type.get(r["type"], 0) + 1

    # Themes recurrents : tags et sous-themes presents dans au moins 2 nouveaux contenus.
    recurrents = []
    if ids:
        marque = ",".join("?" * len(ids))
        for table, lien, cle in (("tags", "content_tags", "tag_id"), ("subthemes", "content_subthemes", "subtheme_id")):
            recurrents += [
                {"kind": table[:-1], **r} for r in _all(conn, f"""
                    SELECT x.name, COUNT(*) count FROM {lien} l JOIN {table} x ON x.id = l.{cle}
                    WHERE l.content_id IN ({marque}) GROUP BY x.id HAVING COUNT(*) >= 2
                    ORDER BY count DESC, x.name""", ids)
            ]

    def actions(statuts, champ="created_at"):
        m = ",".join("?" * len(statuts))
        return _all(conn, f"""{_ACTION_SELECT} WHERE a.status IN ({m}) AND a.{champ} > ? AND a.{champ} <= ?
                              ORDER BY coalesce(a.priority, 5)""", (*statuts, *rng))

    def ouvertes(types):
        m = ",".join("?" * len(types))
        return _all(conn, f"""{_ACTION_SELECT} WHERE a.status IN ('proposed', 'approved', 'todo', 'doing')
                              AND a.action_type IN ({m}) ORDER BY coalesce(a.priority, 5)""", types)

    a_traiter = _all(conn, """SELECT coalesce(action_type, 'autre') action_type, COUNT(*) count FROM actions
                              WHERE status IN ('approved', 'todo', 'doing')
                              GROUP BY 1 ORDER BY count DESC""")
    sans_action = _all(conn, """SELECT id, title, category FROM contents c
                                WHERE NOT EXISTS (SELECT 1 FROM actions a WHERE a.content_id = c.id
                                                  AND a.status != 'dismissed')
                                ORDER BY created_at DESC""")
    non_exploites = [c for c in sans_action
                     if not conn.execute("SELECT 1 FROM notes WHERE content_id = ?", (c["id"],)).fetchone()]
    outils = [r for r in ressources if r["type"] in OUTIL_TYPES]
    stats = {
        "since": None if since == "0000" else since,
        "until": until,
        "new_contents": nouveaux,
        "new_resources": ressources,
        "new_tools": outils,
        "new_resources_by_type": par_type,
        "recurring_ideas": recurrents,
        "actions_proposed": actions(("proposed",)),
        "actions_validated": actions(ACTION_STATUSES_VALIDATED, "updated_at"),
        "actions_done": actions(("done",), "updated_at"),
        "open_actions_by_type": a_traiter,
        "contents_without_action": sans_action,
        "unexploited_contents": non_exploites,
        # Blocs de lecture du bilan (interface REVIEW).
        "blocks": {
            "discovered": nouveaux,
            "tools_to_test": ouvertes(("tester", "comparer", "utiliser")),
            "tools_without_action": [r for r in outils if not r["actions_count"]],
            "ideas_to_exploit": ouvertes(("intégrer", "acheter")),
            "to_share": ouvertes(("partager",)),
            "to_deepen": ouvertes(("approfondir", "rechercher", "lire", "regarder")),
            "priority_actions": _all(conn, f"""{_ACTION_SELECT} WHERE a.status IN ('approved', 'todo', 'doing')
                                               AND a.priority IN (1, 2) ORDER BY a.priority, a.due_date"""),
        },
    }
    stats["counts"] = {
        "new_contents": len(nouveaux),
        "new_resources": len(ressources),
        "new_tools": len(outils),
        "recurring_ideas": len(recurrents),
        "actions_proposed": len(stats["actions_proposed"]),
        "actions_validated": len(stats["actions_validated"]),
        "actions_done": len(stats["actions_done"]),
        "contents_without_action": len(sans_action),
        "unexploited_contents": len(non_exploites),
    }
    stats["summary_text"] = review_text(stats)
    return stats


def review_text(stats):
    c = stats["counts"]
    lignes = [
        f"{c['new_contents']} nouveau(x) contenu(s)",
        f"{c['new_tools']} nouvel(s) outil(s) / {c['new_resources']} ressource(s)",
        f"{c['recurring_ideas']} theme(s) recurrent(s)",
        f"{c['actions_proposed']} action(s) proposee(s), {c['actions_validated']} validee(s), "
        f"{c['actions_done']} terminee(s)",
    ]
    if stats["open_actions_by_type"]:
        lignes.append("A traiter :")
        lignes += [f"  {a['count']} x {a['action_type']}" for a in stats["open_actions_by_type"]]
    lignes.append(f"{c['contents_without_action']} contenu(s) sans action")
    return "\n".join(lignes)


def create_review(conn, summary=None):
    """Enregistre un bilan couvrant la periode depuis le precedent (instantane complet conserve)."""
    stats = review_since_last(conn)
    t = stats["until"]
    cur = conn.execute(
        "INSERT INTO reviews (started_at, ended_at, summary, stats, created_at) VALUES (?,?,?,?,?)",
        (stats["since"], t, summary or stats["summary_text"], json.dumps(stats, ensure_ascii=False), t),
    )
    conn.commit()
    return get_review(conn, cur.lastrowid)


def _review(r):
    donnees = json.loads(r["stats"] or "{}")
    # Compatibilite : les bilans v1 ne stockaient que les compteurs.
    r["stats"] = donnees.get("counts", donnees)
    r["details"] = donnees if "counts" in donnees else None
    return r


def get_review(conn, rid):
    r = _one(conn, "SELECT * FROM reviews WHERE id = ?", (rid,))
    if not r:
        raise VaultError(f"bilan {rid} introuvable", "not_found")
    return _review(r)


def list_reviews(conn, details=False):
    rows = [_review(r) for r in _all(conn, "SELECT * FROM reviews ORDER BY ended_at DESC")]
    if not details:
        for r in rows:
            r.pop("details")
    return rows


# ------------------------------------------------------------------ export generique

def dump(conn):
    """Toutes les donnees du Vault, a plat : base commune des exports (CSV, Excel, Sheets, JSON...)."""
    contenus = [_enrich(conn, r, full=True) for r in conn.execute("SELECT * FROM contents ORDER BY id")]
    return {
        "exported_at": now(),
        "contents": contenus,
        "resources": list_resources(conn, limit=10**9)["items"],
        "actions": list_actions(conn, limit=10**9)["items"],
        "notes": _all(conn, "SELECT n.*, c.title AS content_title FROM notes n JOIN contents c ON c.id = n.content_id"),
        "taxonomy": get_taxonomy(conn),
        "reviews": list_reviews(conn),
    }
