#!/usr/bin/env python3
"""
vault_ingestion.py - Porte d'entree unique des contenus (Ingestion Gateway).

Entrees :
  telegram     : URL, article, TikTok/Reel, note, idee, vocal (transcrit), PDF, document,
                 image, fichier ;
  local_inbox  : C:\\Users\\rosyb\\reels-vault\\inbox\\files (lots, fichiers lourds) et
                 inbox\\sensitive (donnees personnelles : jamais de cloud, jamais de
                 contenu projete dans Notion) ;
  mobile_sync  : pipeline MacroDroid / Syncthing existant (mobile-inbox\\inbox.txt), inchange ;
  notion       : entree secondaire (jamais source maitre).

Reutilise : pipeline video existant (ingest.py via mobile-inbox\\inbox.txt), vault.db
(upsert_content_from_import), pont V3 (promote_all_contents), rattachement projet
(mentor_project_links), routeur LLM (classement par la taxonomie EXISTANTE, jamais inventee).

Regles : un secret n'est jamais stocke ni envoye a un LLM ; une donnee sensible est
classee en local uniquement ; rien n'est supprime (fichiers traites -> inbox\\processed).
"""

import argparse
import hashlib
import html
import json
import re
import shutil
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import llm_router

ROOT = Path(__file__).resolve().parent
INBOX = ROOT / "inbox"
VIDEO_QUEUE = ROOT / "mobile-inbox" / "inbox.txt"
VIDEO_DOMAINS = ("tiktok.com", "instagram.com")  # domaines pris en charge par ingest.py
CHANNELS = ("telegram", "local_inbox", "mobile_sync", "notion", "other")
TEXT_SUFFIXES = (".txt", ".md", ".csv", ".json", ".html", ".htm")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic")
MAX_TEXT = 60000
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/143.0 Safari/537.36")
SAVE_RE = re.compile(r"^\s*(garde(?:[- ]moi)? (?:ca|ça|ceci|cette idee|cette idée)|note\s*:|"
                     r"idee\s*:|idée\s*:|a garder|à garder|sauvegarde|memo\s*:|mémo\s*:)",
                     re.IGNORECASE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingestion_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel             TEXT NOT NULL CHECK (channel IN
                          ('telegram', 'local_inbox', 'mobile_sync', 'notion', 'other')),
    kind                TEXT NOT NULL,
    source_type         TEXT,
    original_url        TEXT,
    original_filename   TEXT,
    stored_path         TEXT,
    telegram_message_id TEXT,
    captured_at         TEXT NOT NULL,
    sensitivity         TEXT NOT NULL DEFAULT 'internal',
    status              TEXT NOT NULL DEFAULT 'pending',
    content_id          INTEGER,
    source_id           INTEGER,
    project_hint        INTEGER,
    error               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingestion_items_status ON ingestion_items(status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ingestion_items_telegram
    ON ingestion_items(channel, telegram_message_id) WHERE telegram_message_id IS NOT NULL;
"""

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "subthemes": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "tools": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["category", "subthemes", "tags", "summary", "tools"],
}


class IngestionError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _item(conn, item_id):
    row = conn.execute("SELECT * FROM ingestion_items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def _new_item(conn, channel, kind, **fields):
    if channel not in CHANNELS:
        raise IngestionError(f"canal inconnu : {channel}")
    ensure_schema(conn)
    stamp = now()
    columns = {"channel": channel, "kind": kind, "captured_at": fields.pop("captured_at", stamp),
               "created_at": stamp, "updated_at": stamp, **fields}
    cur = conn.execute(
        f"INSERT INTO ingestion_items ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})", tuple(columns.values()))
    conn.commit()
    return cur.lastrowid


def _update(conn, item_id, **fields):
    fields["updated_at"] = now()
    conn.execute(f"UPDATE ingestion_items SET {', '.join(f'{k} = ?' for k in fields)} "
                 f"WHERE id = ?", (*fields.values(), item_id))
    conn.commit()


def recent(conn, limit=10):
    ensure_schema(conn)
    rows = conn.execute("SELECT i.*, c.title FROM ingestion_items i LEFT JOIN contents c "
                        "ON c.id = i.content_id ORDER BY i.id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def wants_to_save(text):
    return bool(SAVE_RE.search(str(text or "")))


# ------------------------------------------------------------------ extraction

def is_video_url(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in VIDEO_DOMAINS)


def _meta(page, *names):
    for name in names:
        match = re.search(
            r'<meta[^>]+(?:property|name)=["\']' + re.escape(name) +
            r'["\'][^>]*content=["\']([^"\']+)["\']', page, re.I) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']' +
            re.escape(name) + r'["\']', page, re.I)
        if match:
            return html.unescape(match.group(1)).strip()
    return None


def parse_article(page, url):
    """HTML -> titre, description, auteur, date, texte (sans dependance externe)."""
    title = _meta(page, "og:title", "twitter:title")
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        title = html.unescape(match.group(1)).strip() if match else None
    body = re.sub(r"<(script|style|noscript|nav|footer|header)[^>]*>.*?</\1>", " ", page,
                  flags=re.I | re.S)
    paragraphs = [html.unescape(re.sub(r"<[^>]+>", " ", p)) for p in
                  re.findall(r"<p[^>]*>(.*?)</p>", body, re.I | re.S)]
    text = "\n".join(" ".join(p.split()) for p in paragraphs if len(p.split()) >= 6)
    return {
        "title": " ".join((title or "").split())[:300] or url,
        "description": _meta(page, "og:description", "description", "twitter:description"),
        "author": _meta(page, "author", "article:author", "og:site_name"),
        "published": _meta(page, "article:published_time", "og:updated_time", "date"),
        "text": text[:MAX_TEXT] or None,
    }


def fetch_article(url, timeout=15):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept-Language": "fr,en;q=0.8"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        page = response.read(3_000_000).decode(charset, "replace")
    return parse_article(page, url)


def extract_file_text(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        text = path.read_text(encoding="utf-8", errors="replace")
        if suffix in (".html", ".htm"):
            text = re.sub(r"<[^>]+>", " ", text)
        return " ".join(text.split())[:MAX_TEXT] or None
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:60])
            return text.strip()[:MAX_TEXT] or None
        except Exception:  # noqa: BLE001 - PDF illisible : metadonnees seulement
            return None
    return None


def file_kind(name):
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in TEXT_SUFFIXES:
        return "document"
    return "file"


# ------------------------------------------------------------------ classement

def taxonomy(conn):
    categories = [r[0] for r in conn.execute(
        "SELECT DISTINCT category FROM contents WHERE category IS NOT NULL ORDER BY 1")]
    subthemes = [dict(r) for r in conn.execute("SELECT name, category FROM subthemes ORDER BY name")]
    return categories, subthemes


def classify_content(conn, content_id, *, sensitivity="internal", router=None):
    """Categorie / sous-themes EXISTANTS, tags, resume, outils nommes dans le texte."""
    import vault_db

    row = conn.execute("SELECT title, description_original, transcript, description_manual "
                       "FROM contents WHERE id = ?", (content_id,)).fetchone()
    if row is None:
        return None
    text = "\n".join(filter(None, (row["title"], row["description_manual"],
                                   row["description_original"], row["transcript"])))[:6000]
    categories, subthemes = taxonomy(conn)
    router = router or llm_router.shared()
    result = router.chat_json(
        "Tu classes un contenu du Vault de Rosy. Reponds en francais, JSON uniquement. "
        "category : UNE categorie parmi la liste fournie (sinon chaine vide). subthemes : 0 a 3 "
        "parmi la liste fournie. tags : 3 a 6 mots-cles precis. summary : 1 a 2 phrases, sans "
        "rien inventer. tools : outils/logiciels/sites NOMMES dans le texte uniquement.",
        json.dumps({"categories": categories, "subthemes": [s["name"] for s in subthemes],
                    "contenu": text}, ensure_ascii=False),
        CLASSIFY_SCHEMA, num_predict=260, task="light", sensitivity=sensitivity,
        purpose="ingestion_classify")
    if not isinstance(result, dict):
        return None
    folded = vault_db.fold(text)
    category = result.get("category") if result.get("category") in categories else None
    known = {s["name"] for s in subthemes}
    chosen = [s for s in result.get("subthemes") or [] if s in known][:3]
    tags = [" ".join(str(t).split())[:40] for t in result.get("tags") or [] if str(t).strip()][:6]
    tools = []
    for tool in result.get("tools") or []:
        name = " ".join(str(tool).split())[:60]
        if name and vault_db.fold(name) in folded and name not in tools:
            tools.append(name)  # jamais un outil absent du texte
    summary = " ".join(str(result.get("summary") or "").split())[:500] or None
    current = conn.execute("SELECT category, description_ai, locked_fields FROM contents "
                           "WHERE id = ?", (content_id,)).fetchone()
    locked = set(json.loads(current["locked_fields"] or "[]"))
    changes = {}
    if category and not current["category"] and "category" not in locked:
        changes["category"] = category
    if summary and not current["description_ai"] and "description_ai" not in locked:
        changes["description_ai"] = summary
    if changes:
        conn.execute(f"UPDATE contents SET {', '.join(f'{k} = ?' for k in changes)}, "
                     f"updated_at = ? WHERE id = ?", (*changes.values(), now(), content_id))
    # Jamais d'ecrasement : sous-themes et tags existants (index.md, Rosy) sont conserves.
    has_subthemes = conn.execute("SELECT 1 FROM content_subthemes WHERE content_id = ?",
                                 (content_id,)).fetchone()
    has_tags = conn.execute("SELECT 1 FROM content_tags WHERE content_id = ?",
                            (content_id,)).fetchone()
    if chosen and not has_subthemes:
        vault_db.set_subthemes(conn, content_id, chosen, category)
    if tags and not has_tags:
        vault_db.set_tags(conn, content_id, tags)
    for tool in tools:
        conn.execute("INSERT OR IGNORE INTO resources (content_id, name, type, created_at, "
                     "updated_at) VALUES (?, ?, 'outil', ?, ?)", (content_id, tool, now(), now()))
    conn.commit()
    return {"category": category, "subthemes": chosen, "tags": tags, "tools": tools,
            "summary": summary, "engine": (getattr(router, "last", None) or {}).get("provider")}


# ------------------------------------------------------------------ enregistrement

def _store_content(conn, data):
    import vault_db
    import vault_intelligence_bridge as bridge

    content_id, _ = vault_db.upsert_content_from_import(conn, data)
    conn.commit()
    bridge.promote_all_contents(conn)
    row = conn.execute("SELECT id FROM intelligence_sources WHERE content_id = ?",
                       (content_id,)).fetchone()
    return content_id, (row[0] if row else None)


def _existing_content(conn, url):
    row = conn.execute("SELECT id FROM contents WHERE source_url = ?", (url,)).fetchone()
    return row[0] if row else None


def _project_from_text(conn, text):
    import mentor_council

    found = mentor_council.projects_in_text(conn, text or "")
    return found[0] if len(found) == 1 else None


def _finish(conn, item_id, content_id, source_id, *, sensitivity, project_id, classify,
            router=None):
    """Classement, rattachement projet, evenements Mentor. Retourne un resume court."""
    summary = {"item_id": item_id, "content_id": content_id, "source_id": source_id,
               "sensitivity": sensitivity, "project": None, "proposed_project": None}
    classification = classify_content(conn, content_id, sensitivity=sensitivity,
                                      router=router) if classify else None
    summary["classification"] = classification
    status = "classified" if classification else "ingested"
    if source_id and project_id:
        import mentor_project_links as links

        links.record_decision(conn, source_id=source_id, entity_id=project_id, decision="approve",
                              level="high", confidence=1.0,
                              reason="rattachement demande explicitement par Rosy",
                              explanation=["demande explicite a l'ingestion"])
        summary["project"] = _entity_name(conn, project_id)
    elif source_id:
        summary["proposed_project"] = _propose_project(conn, source_id)
    _update(conn, item_id, status=status, content_id=content_id, source_id=source_id)
    _publish_events(conn)
    return summary


def _entity_name(conn, entity_id):
    row = conn.execute("SELECT name FROM intelligence_entities WHERE id = ?",
                       (entity_id,)).fetchone()
    return row[0] if row else None


def _propose_project(conn, source_id):
    try:
        import mentor_project_links as links

        analysis = links.analyze(conn)
        result = next((r for r in analysis["results"] if r["source"]["id"] == source_id), None)
        if not result:
            return None
        links.record_proposals(conn, {"projects": analysis["projects"], "results": [result]})
        best = [c for c in result["candidates"] if c["level"] in ("high", "medium")]
        return best[0]["project"] if best else None
    except Exception:  # noqa: BLE001 - un rattachement propose n'est jamais bloquant
        return None


def _publish_events(conn):
    try:
        import mentor_proactive
        import mentor_proactive_store as store

        if store.writes_allowed(conn):
            mentor_proactive.publish_vault_events(conn)
    except Exception:  # noqa: BLE001 - le Mentor ne bloque jamais l'ingestion
        pass


def ingest_url(conn, url, *, channel="telegram", telegram_message_id=None, note=None,
               fetcher=fetch_article, video_queue=VIDEO_QUEUE, router=None):
    ensure_schema(conn)
    url = url.strip()
    existing = _existing_content(conn, url)
    if existing:
        return {"status": "duplicate", "content_id": existing}
    project_id = _project_from_text(conn, note)
    if is_video_url(url):
        item_id = _new_item(conn, channel, "url_video", source_type="video", original_url=url,
                            telegram_message_id=telegram_message_id, status="queued_video",
                            project_hint=project_id)
        video_queue.parent.mkdir(parents=True, exist_ok=True)
        with Path(video_queue).open("a", encoding="utf-8") as handle:
            handle.write(url + "\n")
        return {"status": "queued_video", "item_id": item_id, "project": _entity_name(
            conn, project_id) if project_id else None}
    item_id = _new_item(conn, channel, "url_article", source_type="article", original_url=url,
                        telegram_message_id=telegram_message_id, project_hint=project_id)
    try:
        article = fetcher(url)
    except Exception as exc:  # noqa: BLE001 - hors ligne : on garde le lien, analyse plus tard
        article = {"title": url, "description": None, "author": None, "text": None}
        _update(conn, item_id, error=f"recuperation impossible : {type(exc).__name__}")
    sensitivity = llm_router.classify(" ".join(filter(None, (article.get("title"),
                                                             article.get("description"),
                                                             article.get("text"), note))))
    if sensitivity == "secret":
        _update(conn, item_id, status="rejected_secret", sensitivity="secret")
        return {"status": "rejected_secret", "item_id": item_id}
    content_id, source_id = _store_content(conn, {
        "source_url": url, "platform": urllib.parse.urlparse(url).netloc.lower(),
        "content_type": "article", "title": article.get("title"), "author": article.get("author"),
        "description_original": article.get("description") or note,
        "transcript": article.get("text"), "ingested_at": now()})
    _update(conn, item_id, sensitivity=sensitivity)
    return {"status": "stored", "kind": "article", **_finish(
        conn, item_id, content_id, source_id, sensitivity=sensitivity, project_id=project_id,
        classify=True, router=router)}


def ingest_note(conn, text, *, channel="telegram", telegram_message_id=None, source_type="note",
                router=None):
    ensure_schema(conn)
    text = str(text or "").strip()
    if not text:
        raise IngestionError("note vide")
    sensitivity = llm_router.classify(text)
    if sensitivity == "secret":
        # Jamais stocke : ni Vault, ni LLM, ni Notion.
        item_id = _new_item(conn, channel, "note", source_type=source_type,
                            telegram_message_id=telegram_message_id, status="rejected_secret",
                            sensitivity="secret")
        return {"status": "rejected_secret", "item_id": item_id}
    body = SAVE_RE.sub("", text, count=1).strip(" :-") or text
    # « pour Nexdoor Talent : … » : le projet est rattache, le titre garde le contenu.
    body = re.sub(r"^pour\s+[^:\n]{2,60}:\s*", "", body, flags=re.IGNORECASE) or body
    project_id = _project_from_text(conn, text)
    key = telegram_message_id or hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    url = f"note://{channel}/{key}"
    if _existing_content(conn, url):
        return {"status": "duplicate", "content_id": _existing_content(conn, url)}
    item_id = _new_item(conn, channel, "note", source_type=source_type, original_url=url,
                        telegram_message_id=telegram_message_id, sensitivity=sensitivity,
                        project_hint=project_id)
    first_line = body.splitlines()[0]
    content_id, source_id = _store_content(conn, {
        "source_url": url, "platform": channel, "content_type": source_type,
        "title": (first_line[:90] + ("…" if len(first_line) > 90 else "")),
        "description_original": body, "ingested_at": now()})
    return {"status": "stored", "kind": source_type, **_finish(
        conn, item_id, content_id, source_id, sensitivity=sensitivity, project_id=project_id,
        classify=True, router=router)}


def ingest_file(conn, path, *, channel="local_inbox", original_filename=None, caption=None,
                telegram_message_id=None, sensitive=False, router=None):
    ensure_schema(conn)
    path = Path(path)
    name = original_filename or path.name
    kind = file_kind(name)
    text = extract_file_text(path)
    sensitivity = "sensitive" if sensitive else llm_router.classify(
        " ".join(filter(None, (name, caption, text))))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    url = f"file://{channel}/{digest}/{name}"
    if _existing_content(conn, url):
        return {"status": "duplicate", "content_id": _existing_content(conn, url)}
    project_id = _project_from_text(conn, caption)
    item_id = _new_item(conn, channel, kind, source_type=kind, original_url=url,
                        original_filename=name, stored_path=str(path),
                        telegram_message_id=telegram_message_id, sensitivity=sensitivity,
                        project_hint=project_id)
    if sensitivity == "secret":
        # Fichier conserve localement, mais aucun texte en base ni envoi a un LLM.
        _update(conn, item_id, status="rejected_secret")
        return {"status": "rejected_secret", "item_id": item_id}
    content_id, source_id = _store_content(conn, {
        "source_url": url, "platform": channel, "content_type": kind,
        "title": caption.splitlines()[0][:90] if caption else Path(name).stem[:90],
        "description_original": caption, "transcript": text, "source_file": str(path),
        "ingested_at": now()})
    return {"status": "stored", "kind": kind, "text_extracted": bool(text), **_finish(
        conn, item_id, content_id, source_id, sensitivity=sensitivity, project_id=project_id,
        classify=bool(text or caption), router=router)}


# ------------------------------------------------------------------ inbox locale / reprise

def process_local_inbox(conn, root=INBOX, router=None):
    """inbox\\files et inbox\\sensitive -> Vault ; fichiers traites deplaces (jamais supprimes)."""
    results = []
    for folder, sensitive in (("files", False), ("sensitive", True)):
        directory = Path(root) / folder
        directory.mkdir(parents=True, exist_ok=True)
        for path in sorted(p for p in directory.iterdir() if p.is_file()):
            done = Path(root) / "processed" / datetime.now().strftime("%Y-%m-%d") / folder
            done.mkdir(parents=True, exist_ok=True)
            target = done / path.name
            if target.exists():
                target = done / f"{path.stem}-{hashlib.sha1(str(path).encode()).hexdigest()[:6]}{path.suffix}"
            shutil.move(str(path), target)
            try:
                results.append(ingest_file(conn, target, channel="local_inbox",
                                           original_filename=path.name, sensitive=sensitive,
                                           router=router))
            except Exception as exc:  # noqa: BLE001 - un fichier ne bloque pas les autres
                results.append({"status": "failed", "file": path.name,
                                "error": type(exc).__name__})
    return results


def reconcile(conn):
    """Videos en file (pipeline existant) -> marquees ingerees quand le contenu existe."""
    ensure_schema(conn)
    updated = 0
    for item in conn.execute("SELECT * FROM ingestion_items WHERE status = 'queued_video'"):
        content_id = _existing_content(conn, item["original_url"])
        if content_id:
            row = conn.execute("SELECT id FROM intelligence_sources WHERE content_id = ?",
                               (content_id,)).fetchone()
            _update(conn, item["id"], status="ingested", content_id=content_id,
                    source_id=row[0] if row else None)
            if row and item["project_hint"]:
                import mentor_project_links as links

                links.record_decision(conn, source_id=row[0], entity_id=item["project_hint"],
                                      decision="approve", level="high", confidence=1.0,
                                      reason="rattachement demande explicitement par Rosy")
            updated += 1
    return updated


def retry_pending_classification(conn, limit=10, router=None):
    """Contenus ingeres sans classement (LLM indisponible) : nouvel essai."""
    ensure_schema(conn)
    done = 0
    for item in conn.execute("SELECT * FROM ingestion_items WHERE status = 'ingested' AND "
                             "content_id IS NOT NULL ORDER BY id LIMIT ?", (limit,)).fetchall():
        if classify_content(conn, item["content_id"], sensitivity=item["sensitivity"],
                            router=router):
            _update(conn, item["id"], status="classified")
            done += 1
    return done


def classify_unclassified(conn, limit=None, router=None):
    """Contenus existants sans categorie ni resume : classement (jamais d'ecrasement)."""
    import mentor_proactive

    view = mentor_proactive.VaultView(conn)
    rows = conn.execute("SELECT c.id, s.id AS source_id FROM contents c LEFT JOIN "
                        "intelligence_sources s ON s.content_id = c.id WHERE c.category IS NULL "
                        "AND c.description_ai IS NULL ORDER BY c.id").fetchall()
    done, skipped = 0, 0
    for row in rows[:limit] if limit else rows:
        sensitivity = "sensitive" if row["source_id"] and \
            view.source_is_sensitive(row["source_id"]) else "internal"
        if classify_content(conn, row["id"], sensitivity=sensitivity, router=router):
            done += 1
        else:
            skipped += 1
    return {"classified": done, "waiting": skipped, "total": len(rows)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ingestion Gateway du Vault")
    parser.add_argument("--db", default=str(ROOT / "vault.db"))
    parser.add_argument("--local-inbox", action="store_true")
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument("--retry", action="store_true", help="classement des contenus en attente")
    parser.add_argument("--recent", action="store_true")
    parser.add_argument("--classify-existing", action="store_true",
                        help="classe les contenus existants sans categorie ni resume")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    from vault_db import connect

    conn = connect(args.db)
    llm_router.bind(conn)
    try:
        report = {}
        if args.local_inbox:
            report["local_inbox"] = process_local_inbox(conn)
        if args.reconcile:
            report["reconciled"] = reconcile(conn)
        if args.retry:
            report["classified"] = retry_pending_classification(conn)
        if args.classify_existing:
            report["existing"] = classify_unclassified(conn, limit=args.limit)
        if args.recent or not report:
            report["recent"] = recent(conn)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
