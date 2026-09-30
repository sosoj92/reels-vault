#!/usr/bin/env python3
"""
vault_notion_library.py - Projection Vault -> Notion (Vault Library, fiches projet, portail).

vault.db reste maitre ; Notion est la vitrine pour consulter et piloter.
Aucun LLM. Idempotent, incremental (empreinte par source), reprenable, stable par
Source ID (Vault ref = source:<id>, Vault managed = true), sans doublon.
Seuls les blocs CREES par le Vault sont mis a jour : le reste des pages n'est jamais
touche (les blocs geres sont traces dans vault.db).

1. Base « Life Intelligence — Vault Library » sous 📚 KNOWLEDGE & VEILLE : une page par
   source (resume en haut, informations, lien original, transcript complet repliable).
2. Vues : 🆕 Derniers contenus, 🧠 Par theme, 🛠️ Par outil, 💼 Par projet, 🎬 Videos,
   ⭐ A exploiter, 📚 Tout le Vault, + une vue filtree par projet.
3. Fiches projet (pages Notion EXISTANTES des bindings) : section « 🧠 Fiche Life
   Intelligence » (vue d'ensemble, outils, process, priorites, decisions, taches,
   ressources du Vault, SOP, Drive, metadonnees, profil technique repliable).
4. COMMAND CENTER : « 📚 Explorer ma memoire » et « 💼 Mes activites & projets » ;
   KNOWLEDGE & VEILLE : porte d'entree (Library, vues, veille, Mentor Insights).

Usage :
    python vault_notion_library.py                 # dry-run : statistiques, aucune ecriture
    python vault_notion_library.py --confirm       # synchronisation complete
    python vault_notion_library.py --confirm --sources-only
"""

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIBRARY_TITLE = "Life Intelligence — Vault Library"
COMMAND_CENTER = "3e89afcb-cabd-8173-83c7-c3c3f6194f84"
KNOWLEDGE = "3e89afcb-cabd-814f-b0f8-e05aeb946aaa"
ACTIVITIES = "3e89afcb-cabd-81cd-8b76-e634b42e53a0"
MENTOR_INSIGHTS_DB = "95f07b08-97b1-4bab-b555-9db13a389ecf"
TEXT_LIMIT = 1900
PAUSE = 0.34  # ~3 requetes/s (limite Notion)

TYPE_LABELS = {"video": "🎬 Vidéo", "carrousel": "🖼️ Carrousel", "article": "📰 Article",
               "note": "📝 Note", "voice": "🎙️ Note vocale", "pdf": "📄 PDF",
               "document": "📄 Document", "image": "🖼️ Image", "file": "📎 Fichier"}
CHANNEL_LABELS = {"telegram": "Telegram", "local_inbox": "Inbox locale",
                  "mobile_sync": "Mobile", "notion": "Notion", "other": "Autre"}
TOOL_RESOURCE_TYPES = ("outil", "logiciel", "application", "site", "service")
CONCEPT_ENTITY_TYPES = ("method", "training", "other")

PROPERTIES = {
    "Titre": {"title": {}},
    "Type source": {"select": {}},
    "URL originale": {"url": {}},
    "Plateforme": {"select": {}},
    "Auteur / créateur": {"rich_text": {}},
    "Date capture": {"date": {}},
    "Description originale": {"rich_text": {}},
    "Résumé": {"rich_text": {}},
    "Transcript preview": {"rich_text": {}},
    "Thème principal": {"select": {}},
    "Sous-thèmes": {"multi_select": {}},
    "Tags": {"multi_select": {}},
    "Outils": {"multi_select": {}},
    "Concepts": {"multi_select": {}},
    "Projets": {"multi_select": {}},
    "Entités liées": {"multi_select": {}},
    "Signal Mentor": {"select": {}},
    "À exploiter": {"checkbox": {}},
    "Pertinence": {"number": {"format": "number"}},
    "Sensibilité": {"select": {}},
    "Canal d'ingestion": {"select": {}},
    "Source ID": {"number": {"format": "number"}},
    "Vault ref": {"rich_text": {}},
    "Vault managed": {"checkbox": {}},
    "Dernière synchro": {"date": {}},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS notion_library_links (
    source_id      INTEGER PRIMARY KEY,
    page_id        TEXT NOT NULL,
    page_url       TEXT,
    payload_hash   TEXT NOT NULL,
    body_hash      TEXT,
    body_block_ids TEXT NOT NULL DEFAULT '[]',
    synced_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notion_managed_blocks (
    vault_ref    TEXT PRIMARY KEY,
    page_id      TEXT NOT NULL,
    block_ids    TEXT NOT NULL DEFAULT '[]',
    content_hash TEXT NOT NULL,
    synced_at    TEXT NOT NULL
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()


def _setting(conn, key, value=None):
    import vault_db

    if value is None:
        return vault_db.get_setting(conn, f"notion.{key}")
    vault_db.set_setting(conn, f"notion.{key}", value)
    return value


# ------------------------------------------------------------------ blocs Notion

def rt(text, link=None, bold=False):
    parts = []
    text = str(text or "")
    for start in range(0, max(len(text), 1), TEXT_LIMIT):
        chunk = {"type": "text", "text": {"content": text[start:start + TEXT_LIMIT]}}
        if link:
            chunk["text"]["link"] = {"url": link}
        if bold:
            chunk["annotations"] = {"bold": True}
        parts.append(chunk)
    return parts[:100]


def block(kind, text="", **extra):
    body = {"rich_text": rt(text)} if kind not in ("divider",) else {}
    body.update(extra)
    return {"object": "block", "type": kind, kind: body}


def para(text):
    return block("paragraph", text)


def heading(text, level=3):
    return block(f"heading_{level}", text)


def bullet(text, link=None):
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": rt(text, link)}}


def callout(text, emoji="🧠", children=None):
    body = {"rich_text": rt(text), "icon": {"type": "emoji", "emoji": emoji}}
    if children:
        body["children"] = children
    return {"object": "block", "type": "callout", "callout": body}


def toggle(text, children):
    return {"object": "block", "type": "toggle",
            "toggle": {"rich_text": rt(text), "children": children}}


def paragraphs(text, limit=90):
    chunks = [text[i:i + TEXT_LIMIT] for i in range(0, len(text or ""), TEXT_LIMIT)]
    return [para(c) for c in chunks[:limit]] or [para("—")]


# ------------------------------------------------------------------ client

class Notion:
    """Client minimal (reutilise le token/version du cockpit), avec gestion du 429."""

    def __init__(self, inner=None, pause=PAUSE):
        from vault_notion_cockpit import NotionCockpitClient

        self.inner = inner or NotionCockpitClient()
        self.pause = pause

    def configured(self):
        return self.inner.configured()

    def request(self, method, path, body=None):
        from vault_notion_cockpit import CockpitError

        for attempt in range(4):
            try:
                result = self.inner._request(method, path, body)
                if self.pause:
                    time.sleep(self.pause)
                return result
            except CockpitError as exc:
                if getattr(exc, "status", None) in (429, 502, 503) and attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
        return None

    def children(self, block_id):
        results, cursor = [], None
        while True:
            path = f"/blocks/{block_id}/children?page_size=100" + (
                f"&start_cursor={cursor}" if cursor else "")
            page = self.request("GET", path)
            results.extend(page.get("results", []))
            if not page.get("has_more"):
                return results
            cursor = page.get("next_cursor")

    def append(self, block_id, blocks, after=None):
        """
        Ajoute des blocs (apres `after` si fourni). Les ids crees sont determines en comparant
        les enfants avant/apres : la reponse de l'API ne suffit pas a les identifier.
        """
        created = []
        for start in range(0, len(blocks), 90):
            before = {b["id"] for b in self.children(block_id)}
            body = {"children": blocks[start:start + 90]}
            anchor = created[-1] if created else after
            if anchor:
                body["position"] = {"type": "after_block", "after_block": {"id": anchor}}
            self.request("PATCH", f"/blocks/{block_id}/children", body)
            created.extend(b["id"] for b in self.children(block_id) if b["id"] not in before)
        return created

    def delete(self, block_id):
        from vault_notion_cockpit import CockpitError

        try:
            self.request("DELETE", f"/blocks/{block_id}")
        except CockpitError as exc:
            if getattr(exc, "status", None) not in (404, 400):
                raise


# ------------------------------------------------------------------ donnees du Vault

def _normalize_tool(name):
    base = re.sub(r"\.(com|io|ai|app|co|fr|dev)$", "", str(name or "").strip(), flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", base.lower())


def _tool_names(conn, content_id, source_id):
    """Outils de la source, dedoublonnes (Make / Make.com / make.com -> Make)."""
    names = {}
    linked = _table(conn, "intelligence_resource_links")
    sql = ("SELECT r.name, e.name AS entity FROM resources r "
           "LEFT JOIN intelligence_resource_links l ON l.resource_id = r.id "
           "LEFT JOIN intelligence_entities e ON e.id = l.entity_id ") if linked else \
        "SELECT r.name, NULL AS entity FROM resources r "
    for row in conn.execute(
            sql + f"WHERE r.content_id = ? AND r.type IN "
                  f"({','.join('?' for _ in TOOL_RESOURCE_TYPES)})",
            (content_id, *TOOL_RESOURCE_TYPES)):
        label = row["entity"] or row["name"]
        names.setdefault(_normalize_tool(label), label)
    if source_id:
        for row in conn.execute(
                "SELECT e.name FROM intelligence_entity_sources es JOIN intelligence_entities e "
                "ON e.id = es.entity_id WHERE es.source_id = ? AND e.entity_type = 'solution'",
                (source_id,)):
            names.setdefault(_normalize_tool(row["name"]), row["name"])
    return sorted(v for k, v in names.items() if k)


def source_records(conn):
    """Toutes les sources du Vault avec les champs projetes (aucune valeur inventee)."""
    import mentor_proactive

    view = mentor_proactive.VaultView(conn)
    projects = {pid: view.entity(pid)["name"] for pid in view.project_entities()}
    records = []
    for src in conn.execute(
            "SELECT s.id AS source_id, s.source_url, s.title AS s_title, s.author AS s_author, "
            "s.captured_at, s.created_at AS s_created, c.* FROM intelligence_sources s "
            "LEFT JOIN contents c ON c.id = s.content_id ORDER BY s.id").fetchall():
        src = dict(src)
        sid, cid = src["source_id"], src.get("id")
        subthemes = [r[0] for r in conn.execute(
            "SELECT st.name FROM content_subthemes cs JOIN subthemes st ON st.id = cs.subtheme_id "
            "WHERE cs.content_id = ? ORDER BY st.name", (cid,))] if cid else []
        tags = [r[0] for r in conn.execute(
            "SELECT t.name FROM content_tags ct JOIN tags t ON t.id = ct.tag_id "
            "WHERE ct.content_id = ? ORDER BY t.name", (cid,))] if cid else []
        entities = [dict(r) for r in conn.execute(
            "SELECT DISTINCT e.id, e.name, e.entity_type FROM intelligence_entity_sources es "
            "JOIN intelligence_entities e ON e.id = es.entity_id WHERE es.source_id = ? "
            "AND es.role <> 'primary' ORDER BY e.name", (sid,))]
        linked_projects = sorted({e["name"] for e in entities if e["id"] in projects})
        insight = conn.execute(
            "SELECT insight_type, relevance_score, status, observation FROM "
            "intelligence_mentor_insights_local WHERE source_id = ?", (sid,)).fetchone()
        processing = conn.execute(
            "SELECT outcome, relevance_score FROM intelligence_mentor_processing "
            "WHERE source_id = ?", (sid,)).fetchone()
        channel = None
        try:
            item = conn.execute("SELECT channel FROM ingestion_items WHERE source_id = ? OR "
                                "content_id = ? ORDER BY id LIMIT 1", (sid, cid)).fetchone()
            channel = item[0] if item else None
        except Exception:  # noqa: BLE001 - table absente
            channel = None
        sensitive = view.source_is_sensitive(sid)
        insight = dict(insight) if insight else None
        exploitable = bool(insight and insight["insight_type"] in
                           ("opportunity", "opportunity_convergence") and
                           (insight["relevance_score"] or 0) >= 0.6 and
                           insight["status"] not in ("dismissed", "rejected", "archived"))
        records.append({
            "source_id": sid, "content_id": cid,
            "title": (src.get("title") or src.get("s_title") or src["source_url"])[:250],
            "type": src.get("content_type"), "url": src.get("source_url"),
            "platform": src.get("platform"), "author": src.get("author") or src.get("s_author"),
            "captured_at": src.get("captured_at") or (src.get("ingested_at") or "")[:10] or None,
            "description": src.get("description_manual") or src.get("description_original"),
            "summary": src.get("description_ai"), "transcript": src.get("transcript"),
            "theme": src.get("category"), "subthemes": subthemes, "tags": tags,
            "tools": _tool_names(conn, cid, sid) if cid else [],
            "concepts": sorted({e["name"] for e in entities
                                if e["entity_type"] in CONCEPT_ENTITY_TYPES}),
            "projects": linked_projects,
            "entities": sorted({e["name"] for e in entities if e["id"] not in projects}),
            "signal": (insight or {}).get("insight_type") or (dict(processing)["outcome"]
                                                             if processing else None),
            "relevance": (insight or {}).get("relevance_score") or (
                dict(processing)["relevance_score"] if processing else None),
            "why": (insight or {}).get("observation"),
            "exploitable": exploitable, "sensitive": sensitive, "channel": channel,
        })
    return records


def _options(values):
    seen, out = set(), []
    for value in values or []:
        name = re.sub(r"[,]", " ", str(value)).strip()[:100]
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append({"name": name})
    return out[:40]


def properties(record):
    sensitive = record["sensitive"]
    props = {
        "Titre": {"title": rt(record["title"])},
        "Type source": {"select": {"name": TYPE_LABELS.get(record["type"], record["type"])}
                        if record["type"] else None},
        "URL originale": {"url": record["url"] if str(record["url"] or "").startswith("http")
                          else None},
        "Plateforme": {"select": {"name": record["platform"][:90]} if record["platform"] else None},
        "Auteur / créateur": {"rich_text": rt(record["author"]) if record["author"] else []},
        "Date capture": {"date": {"start": record["captured_at"][:10]}
                         if record["captured_at"] else None},
        "Description originale": {"rich_text": [] if sensitive or not record["description"]
                                  else rt(record["description"][:TEXT_LIMIT])},
        "Résumé": {"rich_text": [] if sensitive or not record["summary"]
                   else rt(record["summary"][:TEXT_LIMIT])},
        "Transcript preview": {"rich_text": [] if sensitive or not record["transcript"]
                               else rt(record["transcript"][:400])},
        "Thème principal": {"select": {"name": record["theme"]} if record["theme"] else None},
        "Sous-thèmes": {"multi_select": _options(record["subthemes"])},
        "Tags": {"multi_select": _options(record["tags"])},
        "Outils": {"multi_select": _options(record["tools"])},
        "Concepts": {"multi_select": _options(record["concepts"])},
        "Projets": {"multi_select": _options(record["projects"])},
        "Entités liées": {"multi_select": _options(record["entities"])},
        "Signal Mentor": {"select": {"name": record["signal"]} if record["signal"] else None},
        "À exploiter": {"checkbox": record["exploitable"]},
        "Pertinence": {"number": round(record["relevance"], 2)
                       if record["relevance"] is not None else None},
        "Sensibilité": {"select": {"name": "🔒 Sensible (gardé local)" if sensitive
                                   else "Standard"}},
        "Canal d'ingestion": {"select": {"name": CHANNEL_LABELS.get(record["channel"],
                                                                    record["channel"])}
                              if record["channel"] else None},
        "Source ID": {"number": record["source_id"]},
        "Vault ref": {"rich_text": rt(f"source:{record['source_id']}")},
        "Vault managed": {"checkbox": True},
    }
    return props


def body_blocks(record):
    """Corps de la page : pourquoi / resume en haut, infos, lien, transcript repliable."""
    if record["sensitive"]:
        return [callout("Contenu sensible : le detail reste dans le Vault local "
                        "(aucune description ni transcript projete).", "🔒")]
    top = record["summary"] or record["why"] or record["description"] or "Pas encore de résumé."
    blocks = [callout(top[:TEXT_LIMIT], "🧠")]
    if record["why"] and record["summary"]:
        blocks.append(para(f"Pourquoi c'est utile (Mentor) : {record['why'][:TEXT_LIMIT]}"))
    info = [("Type", TYPE_LABELS.get(record["type"], record["type"])),
            ("Plateforme", record["platform"]), ("Auteur", record["author"]),
            ("Date", record["captured_at"]), ("Thème", record["theme"]),
            ("Sous-thèmes", ", ".join(record["subthemes"])),
            ("Outils", ", ".join(record["tools"])), ("Projets", ", ".join(record["projects"])),
            ("Source ID", record["source_id"])]
    blocks.append(heading("Informations"))
    blocks.extend(bullet(f"{k} : {v}") for k, v in info if v not in (None, ""))
    if str(record["url"] or "").startswith("http"):
        blocks.append({"object": "block", "type": "bookmark",
                       "bookmark": {"url": record["url"]}})
    if record["description"]:
        blocks.append(heading("Description originale"))
        blocks.extend(paragraphs(record["description"], limit=10))
    if record["transcript"]:
        blocks.append(toggle("📜 Transcript complet", paragraphs(record["transcript"], limit=90)))
    return blocks


# ------------------------------------------------------------------ Vault Library

def ensure_library(conn, notion):
    """Trouve ou cree la base sous KNOWLEDGE & VEILLE (jamais de doublon)."""
    database_id = _setting(conn, "library_database_id")
    data_source_id = _setting(conn, "library_data_source_id")
    if database_id and data_source_id:
        return database_id, data_source_id
    for child in notion.children(KNOWLEDGE):
        if child["type"] == "child_database" and \
                child["child_database"].get("title") == LIBRARY_TITLE:
            database = notion.request("GET", f"/databases/{child['id']}")
            database_id, data_source_id = database["id"], database["data_sources"][0]["id"]
            break
    else:
        database = notion.request("POST", "/databases", {
            "parent": {"type": "page_id", "page_id": KNOWLEDGE},
            "title": rt(LIBRARY_TITLE),
            "icon": {"type": "emoji", "emoji": "📚"},
            "initial_data_source": {"properties": PROPERTIES},
        })
        database_id, data_source_id = database["id"], database["data_sources"][0]["id"]
    _setting(conn, "library_database_id", database_id)
    _setting(conn, "library_data_source_id", data_source_id)
    return database_id, data_source_id


def _find_page(notion, data_source_id, source_id):
    result = notion.request("POST", f"/data_sources/{data_source_id}/query", {
        "filter": {"property": "Vault ref", "rich_text": {"equals": f"source:{source_id}"}},
        "page_size": 5})
    pages = [p for p in result.get("results", []) if not p.get("in_trash")]
    return pages[0] if pages else None


def sync_sources(conn, notion, *, only=None, limit=None):
    """Upsert incremental des sources. Retourne les compteurs."""
    ensure_schema(conn)
    _, data_source_id = ensure_library(conn, notion)
    counts = {"created": 0, "updated": 0, "unchanged": 0, "body_updated": 0}
    records = [r for r in source_records(conn) if only is None or r["source_id"] in only]
    for record in records[:limit] if limit else records:
        props = properties(record)
        payload_hash = _hash(props)
        body = body_blocks(record)
        body_hash = _hash(body)
        link = conn.execute("SELECT * FROM notion_library_links WHERE source_id = ?",
                            (record["source_id"],)).fetchone()
        link = dict(link) if link else None
        if link is None:
            existing = _find_page(notion, data_source_id, record["source_id"])
            if existing:  # reprise : la page existe deja (base locale perdue ou coupure)
                link = {"page_id": existing["id"], "page_url": existing.get("url"),
                        "payload_hash": "", "body_hash": None, "body_block_ids": "[]"}
        stamped = dict(props, **{"Dernière synchro": {"date": {"start": now()}}})
        if link is None:
            page = notion.request("POST", "/pages", {
                "parent": {"type": "data_source_id", "data_source_id": data_source_id},
                "properties": stamped, "children": body[:90]})
            block_ids = [b["id"] for b in notion.children(page["id"])]
            if len(body) > 90:
                block_ids += notion.append(page["id"], body[90:])
            link = {"page_id": page["id"], "page_url": page.get("url")}
            counts["created"] += 1
            _save_link(conn, record["source_id"], link, payload_hash, body_hash, block_ids)
            continue
        changed = False
        if link["payload_hash"] != payload_hash:
            notion.request("PATCH", f"/pages/{link['page_id']}", {"properties": stamped})
            changed = True
        block_ids = json.loads(link.get("body_block_ids") or "[]")
        if link.get("body_hash") != body_hash:
            for block_id in block_ids:  # seulement les blocs crees par le Vault
                notion.delete(block_id)
            block_ids = notion.append(link["page_id"], body)
            counts["body_updated"] += 1
            changed = True
        counts["updated" if changed else "unchanged"] += 1
        _save_link(conn, record["source_id"], link, payload_hash, body_hash, block_ids)
    return counts


def _save_link(conn, source_id, link, payload_hash, body_hash, block_ids):
    conn.execute(
        "INSERT INTO notion_library_links (source_id, page_id, page_url, payload_hash, body_hash, "
        "body_block_ids, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source_id) DO "
        "UPDATE SET page_id = excluded.page_id, page_url = excluded.page_url, "
        "payload_hash = excluded.payload_hash, body_hash = excluded.body_hash, "
        "body_block_ids = excluded.body_block_ids, synced_at = excluded.synced_at",
        (source_id, link["page_id"], link.get("page_url"), payload_hash, body_hash,
         json.dumps(block_ids), now()))
    conn.commit()


# ------------------------------------------------------------------ vues

def view_specs(conn):
    import mentor_proactive

    view = mentor_proactive.VaultView(conn)
    specs = [
        ("🆕 Derniers contenus", "table", None, [{"property": "Date capture",
                                                 "direction": "descending"}]),
        ("🧠 Par thème", "table", {"property": "Thème principal", "select": {"is_not_empty": True}},
         [{"property": "Thème principal", "direction": "ascending"}]),
        ("🛠️ Par outil", "table", {"property": "Outils", "multi_select": {"is_not_empty": True}},
         [{"property": "Titre", "direction": "ascending"}]),
        ("💼 Par projet", "table", {"property": "Projets",
                                   "multi_select": {"is_not_empty": True}}, None),
        ("🎬 Vidéos", "table", {"property": "Type source", "select": {"equals": "🎬 Vidéo"}},
         [{"property": "Date capture", "direction": "descending"}]),
        ("⭐ À exploiter", "table", {"property": "À exploiter", "checkbox": {"equals": True}},
         [{"property": "Pertinence", "direction": "descending"}]),
        ("📚 Tout le Vault", "table", None, [{"property": "Source ID", "direction": "descending"}]),
    ]
    for pid in view.project_entities():
        name = view.entity(pid)["name"]
        specs.append((f"💼 {name}", "table",
                      {"property": "Projets", "multi_select": {"contains": name}}, None))
    return specs


def ensure_views(conn, notion):
    """Cree les vues manquantes (API /views). Retourne {nom: url} ; erreurs signalees."""
    database_id, data_source_id = ensure_library(conn, notion)
    existing = {}
    listing = notion.request("GET", f"/views?data_source_id={data_source_id}")
    for item in listing.get("results", []):
        detail = notion.request("GET", f"/views/{item['id']}")
        existing[detail.get("name")] = detail.get("url")
    urls, errors = {}, {}
    from vault_notion_cockpit import CockpitError

    # Une vue filtree sur un projet exige que l'option existe (projets encore sans source).
    import mentor_proactive

    names = [mentor_proactive.VaultView(conn).entity(pid)["name"]
             for pid in mentor_proactive.VaultView(conn).project_entities()]
    schema = notion.request("GET", f"/data_sources/{data_source_id}")
    current = ((schema.get("properties") or {}).get("Projets") or {}).get(
        "multi_select", {}).get("options", [])
    missing = [n for n in names if n not in {o["name"] for o in current}]
    if missing:
        notion.request("PATCH", f"/data_sources/{data_source_id}", {"properties": {"Projets": {
            "multi_select": {"options": [{"id": o["id"], "name": o["name"]} for o in current]
                             + [{"name": n} for n in missing]}}}})

    for name, kind, filt, sorts in view_specs(conn):
        if name in existing:
            urls[name] = existing[name]
            continue
        body = {"data_source_id": data_source_id, "database_id": database_id, "name": name,
                "type": kind}
        if filt:
            body["filter"] = filt
        if sorts:
            body["sorts"] = sorts
        try:
            created = notion.request("POST", "/views", body)
            urls[name] = created.get("url")
        except CockpitError as exc:
            errors[name] = str(exc)[:160]
    _setting(conn, "library_views", json.dumps(urls, ensure_ascii=False))
    return urls, errors


# ------------------------------------------------------------------ fiches projet

def _managed(conn, notion, vault_ref, page_id, blocks, *, after=None):
    """Remplace uniquement les blocs geres (trace dans vault.db) d'une page existante."""
    ensure_schema(conn)
    content_hash = _hash(blocks)
    row = conn.execute("SELECT * FROM notion_managed_blocks WHERE vault_ref = ?",
                       (vault_ref,)).fetchone()
    if row and row["content_hash"] == content_hash and row["page_id"] == page_id:
        return "unchanged"
    for block_id in json.loads(row["block_ids"]) if row else []:
        notion.delete(block_id)
    ids = notion.append(page_id, blocks, after=after)
    conn.execute(
        "INSERT INTO notion_managed_blocks (vault_ref, page_id, block_ids, content_hash, "
        "synced_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(vault_ref) DO UPDATE SET "
        "page_id = excluded.page_id, block_ids = excluded.block_ids, "
        "content_hash = excluded.content_hash, synced_at = excluded.synced_at",
        (vault_ref, page_id, json.dumps(ids), content_hash, now()))
    conn.commit()
    return "updated" if row else "created"


def _page_url(page_id):
    return f"https://www.notion.so/{str(page_id).replace('-', '')}"


def project_blocks(conn, notion, entity_id, library_links, view_urls):
    import mentor_project_profiles as profiles
    import vault_ask

    view = vault_ask.project_view(conn, entity_id)
    profile = profiles.get_profile(conn, entity_id) or {}
    binding = view.get("binding") or {}
    entity = view["entity"]
    todo = "À compléter (aucune donnée enregistrée dans le Vault ni le profil)."
    blocks = [heading("Vue d'ensemble"),
              para(profile.get("description") or todo)]
    for title in ("Objectif", "Offre / proposition de valeur", "Cibles", "Marchés / zones",
                  "Produits / services"):
        blocks += [heading(title), para(todo)]

    tools = [t["topic"] for t in profile.get("specific_tools") or []]
    seen_tools = sorted({t for s in source_records(conn) if s["source_id"] in
                         {x["source_id"] for x in view["sources"]} for t in s["tools"]})
    blocks.append(heading("Outils & stack"))
    blocks += [bullet(f"{t} (outil du projet)") for t in tools] or [para("—")]
    if seen_tools:
        blocks.append(bullet("Vus dans les ressources liées : " + ", ".join(seen_tools)))

    children = [c for c in notion.children(binding_page(conn, entity_id))
                if c["type"] == "child_page"] if binding_page(conn, entity_id) else []
    sops = [c for c in children if re.search(r"\bsop\b|process|proc[ée]dure",
                                             c["child_page"]["title"], re.I)]
    process = [c for c in children if c not in sops]
    blocks.append(heading("Process / systèmes"))
    blocks += [bullet(c["child_page"]["title"], _page_url(c["id"])) for c in process] or \
        [para("—")]

    blocks.append(heading("Clients / prospects / partenaires"))
    kids = conn.execute("SELECT e.id, e.name, e.entity_type, b.notion_page_id FROM "
                        "intelligence_entity_bindings b JOIN intelligence_entities e ON "
                        "e.id = b.entity_id WHERE b.parent_entity_id = ?", (entity_id,)).fetchall()
    blocks += [bullet(f"{k['name']} ({k['entity_type']})",
                      _page_url(k["notion_page_id"]) if k["notion_page_id"] else None)
               for k in kids] or [para("—")]

    blocks.append(heading("Priorités actuelles"))
    open_suggestions = [s for s in view["suggestions"] if s["status"] in
                        ("ready", "sent", "opened", "snoozed")]
    priorities = [f"Attention : {entity['attention_status']}"] + \
        [f"{s['suggestion_id']} — {s['title']} ({s['status']})" for s in open_suggestions]
    blocks += [bullet(p) for p in priorities]

    blocks.append(heading("Décisions importantes"))
    decided = [s for s in view["suggestions"] if s["status"] not in
               ("ready", "sent", "opened", "snoozed")]
    link_decisions = conn.execute(
        "SELECT p.proposal_id, p.status, p.reviewed_at, s.title FROM "
        "mentor_project_link_proposals p LEFT JOIN intelligence_sources s ON s.id = p.source_id "
        "WHERE p.entity_id = ? AND p.status IN ('approved', 'rejected') ORDER BY p.reviewed_at",
        (entity_id,)).fetchall() if _table(conn, "mentor_project_link_proposals") else []
    items = [f"{s['suggestion_id']} — {s['title']} : {s['status']}" for s in decided] + \
        [f"Rattachement {d['proposal_id']} ({d['title']}) : "
         f"{'approuvé' if d['status'] == 'approved' else 'refusé'}" for d in link_decisions]
    blocks += [bullet(i) for i in items] or [para("—")]

    blocks.append(heading("Tâches actives"))
    blocks += [bullet(f"#{t['id']} {t['title']} ({t['status']})") for t in view["tasks"]
               if t["status"] not in ("done",)] or [para("Aucune tâche active liée.")]

    blocks.append(heading("Ressources du Vault"))
    filtered = view_urls.get(f"💼 {entity['name']}")
    if filtered:
        blocks.append(bullet(f"Vue filtrée « {entity['name']} » dans la Vault Library", filtered))
    for source in view["sources"]:
        url = library_links.get(source["source_id"])
        blocks.append(bullet(f"{source['title']} (source {source['source_id']})", url))
    if not view["sources"]:
        blocks.append(para("Aucune source rattachée pour l'instant."))

    blocks.append(heading("SOP"))
    blocks += [bullet(c["child_page"]["title"], _page_url(c["id"])) for c in sops] or \
        [para("À formaliser.")]

    blocks.append(heading("Fichiers Drive"))
    blocks.append(bullet("Dossier Drive du projet", binding["drive_url"])
                  if binding.get("drive_url") else para("—"))

    blocks.append(heading("Métadonnées"))
    blocks += [bullet(f"Entity ID : {entity_id}"),
               bullet(f"Type : {entity['entity_type']}"),
               bullet(f"Statut : attention {entity['attention_status']}, profil "
                      f"{profile.get('profile_status', 'absent')}, "
                      f"{'actif' if profile.get('active', 1) else 'inactif'}"),
               bullet(f"Dernière synchro : {now()}")]
    technical = {k: profile.get(k) for k in ("core_topics", "context_topics", "excluded_topics",
                                             "specific_tools", "named_entities", "advisors",
                                             "active", "compound_rules")}
    # Notion accepte 2 niveaux d'imbrication par requete : le profil technique (repliable)
    # est un bloc frere du conteneur, pas un enfant.
    return [callout("Fiche Life Intelligence — synchronisée depuis le Vault (vault.db reste "
                    "maître ; modifier ce bloc ne modifie pas le Vault).", "🧠", blocks[:95]),
            toggle("⚙️ Profil technique (mentor_project_profiles, lecture seule)",
                   paragraphs(json.dumps(technical, ensure_ascii=False, indent=1), limit=40))]


def _table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def binding_page(conn, entity_id):
    row = conn.execute("SELECT notion_page_id FROM intelligence_entity_bindings WHERE "
                       "entity_id = ?", (entity_id,)).fetchone()
    return row[0] if row and row[0] else None


def sync_projects(conn, notion, view_urls=None):
    links = {r["source_id"]: r["page_url"] for r in conn.execute(
        "SELECT source_id, page_url FROM notion_library_links")} if _table(
        conn, "notion_library_links") else {}
    view_urls = view_urls or json.loads(_setting(conn, "library_views") or "{}")
    result = {}
    for row in conn.execute("SELECT entity_id, notion_page_id FROM intelligence_entity_bindings "
                            "WHERE notion_page_id IS NOT NULL ORDER BY entity_id").fetchall():
        blocks = project_blocks(conn, notion, row[0], links, view_urls)
        result[row[0]] = _managed(conn, notion, f"project_page:{row[0]}", row[1], blocks)
    return result


# ------------------------------------------------------------------ portails

def sync_portals(conn, notion, view_urls=None):
    database_id, _ = ensure_library(conn, notion)
    view_urls = view_urls or json.loads(_setting(conn, "library_views") or "{}")
    library = _page_url(database_id)

    def view_link(name):
        return view_urls.get(name) or library

    explore = [bullet("📚 Vault Library (tout le Vault)", library)] + [
        bullet(name, view_link(name)) for name in
        ("🆕 Derniers contenus", "🎬 Vidéos", "🧠 Par thème", "🛠️ Par outil", "💼 Par projet",
         "⭐ À exploiter")]
    projects = []
    for row in conn.execute("SELECT e.name, b.notion_page_id, b.parent_entity_id, e.entity_type "
                            "FROM intelligence_entity_bindings b JOIN intelligence_entities e "
                            "ON e.id = b.entity_id ORDER BY b.parent_entity_id IS NOT NULL, "
                            "e.id").fetchall():
        prefix = "   ↳ " if row["parent_entity_id"] else ""
        projects.append(bullet(f"{prefix}{row['name']}", _page_url(row["notion_page_id"])))
    projects.append(bullet("👥 Clients / opportunités", _page_url(ACTIVITIES)))
    command = [callout("📚 Explorer ma mémoire", "📚", explore),
               callout("💼 Mes activités & projets", "💼", projects)]
    first = next((b["id"] for b in notion.children(COMMAND_CENTER) if b["type"] == "callout"),
                 None)
    result = {"command_center": _managed(conn, notion, "portal:command_center", COMMAND_CENTER,
                                         command, after=first)}
    knowledge = [callout("Porte d'entrée : tout ce que ton Vault a mémorisé, à consulter ici "
                         "(vault.db reste maître).", "🚪", explore + [
                             bullet("📁 Veille marché", _page_url(_child_id(
                                 notion, KNOWLEDGE, "📁 Veille marché")) if _child_id(
                                 notion, KNOWLEDGE, "📁 Veille marché") else None),
                             bullet("🧭 Mentor Insights", _page_url(MENTOR_INSIGHTS_DB))])]
    first_k = next((b["id"] for b in notion.children(KNOWLEDGE) if b["type"] == "callout"), None)
    result["knowledge"] = _managed(conn, notion, "portal:knowledge", KNOWLEDGE, knowledge,
                                   after=first_k)
    return result


def _child_id(notion, parent, title):
    for child in notion.children(parent):
        if child["type"] == "child_page" and child["child_page"]["title"] == title:
            return child["id"]
    return None


# ------------------------------------------------------------------ statistiques / CLI

def stats(conn):
    records = source_records(conn)
    projected = conn.execute("SELECT COUNT(*) FROM notion_library_links").fetchone()[0] \
        if _table(conn, "notion_library_links") else 0
    return {
        "sources_vault": len(records), "projetees_notion": projected,
        "videos": sum(1 for r in records if r["type"] == "video"),
        "avec_resume": sum(1 for r in records if r["summary"]),
        "avec_description": sum(1 for r in records if r["description"]),
        "avec_transcript": sum(1 for r in records if r["transcript"]),
        "avec_theme": sum(1 for r in records if r["theme"]),
        "avec_outils": sum(1 for r in records if r["tools"]),
        "avec_projet": sum(1 for r in records if r["projects"]),
        "sans_classement": sum(1 for r in records if not r["theme"] and not r["subthemes"]),
        "sensibles": sum(1 for r in records if r["sensitive"]),
    }


def sync_all(conn, notion, *, sources_only=False):
    report = {"sources": sync_sources(conn, notion)}
    if not sources_only:
        urls, errors = ensure_views(conn, notion)
        report["views"] = sorted(urls)
        report["view_errors"] = errors
        report["projects"] = sync_projects(conn, notion, urls)
        report["portals"] = sync_portals(conn, notion, urls)
    report["stats"] = stats(conn)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Projection Vault -> Notion")
    parser.add_argument("--db", default=str(ROOT / "vault.db"))
    parser.add_argument("--confirm", action="store_true", help="ecrit dans Notion")
    parser.add_argument("--sources-only", action="store_true")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    from vault_db import connect

    conn = connect(args.db)
    try:
        if not args.confirm:
            print(json.dumps({"dry_run": True, "stats": stats(conn)}, ensure_ascii=False, indent=2))
            return 0
        notion = Notion()
        if not notion.configured():
            print("Notion non configure (NOTION_TOKEN absent) : rien a faire.")
            return 0
        if not acquire_lock():
            # Une autre synchronisation tourne (tache horaire / quotidienne / manuelle) :
            # deux passes simultanees creeraient des doublons.
            print("Synchronisation deja en cours : passage ignore.")
            return 0
        try:
            print(json.dumps(sync_all(conn, notion, sources_only=args.sources_only),
                             ensure_ascii=False, indent=2, default=str))
        finally:
            LOCK_FILE.unlink(missing_ok=True)
    finally:
        conn.close()
    return 0


LOCK_FILE = ROOT / "logs" / "notion_library.lock"


def _pid_alive(pid):
    import os

    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock(path=None):
    """Verrou d'instance unique (PID). Un verrou orphelin (processus mort) est repris."""
    import os

    path = Path(path or LOCK_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if _pid_alive(int(path.read_text().strip())):
                return False
        except (ValueError, OSError):
            pass
    path.write_text(str(os.getpid()))
    return True


if __name__ == "__main__":
    sys.exit(main())
