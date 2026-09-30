import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import itertools
import json
import unittest

import _life_fixtures as fx

import vault_notion_library as lib
from vault_notion_cockpit import CockpitError


def _depth(blocks, level=1):
    deepest = 0
    for b in blocks or []:
        kids = (b.get(b["type"]) or {}).get("children")
        deepest = max(deepest, level, _depth(kids, level + 1) if kids else level)
    return deepest


class FakeNotionAPI:
    """API Notion simulee : pages, blocs, bases, requetes, vues. Limite d'imbrication reelle."""

    def __init__(self):
        self.ids = itertools.count(1)
        self.blocks = {}      # id -> bloc
        self.children_of = {}  # parent -> [ids]
        self.pages = {}
        self.databases = {}
        self.views = {}
        self.writes = 0

    def configured(self):
        return True

    def _new(self, prefix):
        return f"{prefix}-{next(self.ids)}"

    def page(self, title, parent=None, blocks=()):
        page_id = self._new("page")
        self.pages[page_id] = {"id": page_id, "properties": {}, "url": f"https://n/{page_id}",
                               "title": title}
        self.children_of[page_id] = []
        for b in blocks:
            self._add(page_id, b)
        if parent:
            self._add(parent, {"type": "child_page", "child_page": {"title": title}}, page_id)
        return page_id

    def _add(self, parent, block, block_id=None, after=None):
        block_id = block_id or self._new("block")
        stored = {"id": block_id, "type": block["type"], block["type"]: dict(block[block["type"]])}
        kids = stored[block["type"]].pop("children", None)
        stored["has_children"] = bool(kids)
        self.blocks[block_id] = stored
        siblings = self.children_of.setdefault(parent, [])
        if after in siblings:
            siblings.insert(siblings.index(after) + 1, block_id)
        else:
            siblings.append(block_id)
        self.children_of.setdefault(block_id, [])
        for kid in kids or []:
            self._add(block_id, kid)
        return block_id

    def _request(self, method, path, body=None):
        body = body or {}
        if method != "GET":
            self.writes += 1
        parts = path.split("?")[0].strip("/").split("/")
        if parts[0] == "blocks" and parts[-1] == "children" and method == "GET":
            return {"results": [self.blocks[i] for i in self.children_of.get(parts[1], [])],
                    "has_more": False}
        if parts[0] == "blocks" and parts[-1] == "children" and method == "PATCH":
            if _depth(body["children"]) > 3:
                raise CockpitError("nesting too deep")
            after, created = body.get("after"), []
            for b in body["children"]:
                after = self._add(parts[1], b, after=after)
                created.append(self.blocks[after])
            return {"results": created}
        if parts[0] == "blocks" and method == "DELETE":
            for siblings in self.children_of.values():
                if parts[1] in siblings:
                    siblings.remove(parts[1])
            return {}
        if parts[0] == "databases" and method == "POST":
            db_id, ds_id = self._new("db"), self._new("ds")
            self.databases[db_id] = {"id": db_id, "data_sources": [{"id": ds_id}],
                                     "properties": body["initial_data_source"]["properties"]}
            self._add(body["parent"]["page_id"],
                      {"type": "child_database", "child_database": {
                          "title": body["title"][0]["text"]["content"]}}, db_id)
            return self.databases[db_id]
        if parts[0] == "databases":
            return self.databases[parts[1]]
        if parts[0] == "pages" and method == "POST":
            if _depth(body.get("children")) > 2:
                raise CockpitError("nesting too deep")
            page_id = self.page("x", blocks=body.get("children") or [])
            self.pages[page_id].update(properties=body["properties"],
                                       parent=body["parent"]["data_source_id"])
            return self.pages[page_id]
        if parts[0] == "pages" and method == "PATCH":
            self.pages[parts[1]]["properties"].update(body["properties"])
            return self.pages[parts[1]]
        if parts[0] == "data_sources" and parts[-1] == "query":
            ref = body["filter"]["rich_text"]["equals"]
            found = [p for p in self.pages.values() if p.get("parent") == parts[1] and
                     p["properties"].get("Vault ref", {}).get("rich_text", [{}])[0]
                     .get("text", {}).get("content") == ref]
            return {"results": found}
        if parts[0] == "views" and method == "GET" and len(parts) == 1:
            return {"results": [{"id": v} for v in self.views]}
        if parts[0] == "views" and method == "GET":
            return self.views[parts[1]]
        if parts[0] == "views" and method == "POST":
            view_id = self._new("view")
            self.views[view_id] = dict(body, id=view_id, url=f"https://n/view/{view_id}")
            return self.views[view_id]
        raise AssertionError(f"appel inattendu {method} {path}")

    def texts(self, parent):
        out = []
        for block_id in self.children_of.get(parent, []):
            b = self.blocks[block_id]
            rich = b[b["type"]].get("rich_text") or []
            out.append("".join(r["text"]["content"] for r in rich))
            out.extend(self.texts(block_id))
        return out


class LibraryCase(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()
        self.path, self.conn, self.ids, self.sources = fx.life_db()
        self.api = FakeNotionAPI()
        knowledge = self.api.page("📚 KNOWLEDGE & VEILLE",
                                  blocks=[{"type": "callout", "callout": {"rich_text": []}}])
        command = self.api.page("COMMAND CENTER",
                                blocks=[{"type": "callout", "callout": {"rich_text": []}},
                                        {"type": "paragraph", "paragraph": {"rich_text": [
                                            {"type": "text", "text": {"content": "Note de Rosy"}}]}}])
        self.api.page("📁 Veille marché", parent=knowledge)
        self.talent_page = self.api.page("Nexdoor Talent", blocks=[
            {"type": "paragraph", "paragraph": {"rich_text": [
                {"type": "text", "text": {"content": "Fonction : recrutement"}}]}}])
        self.api.page("📄 Process Recrutement", parent=self.talent_page)
        self.api.page("Manatal - BDD", parent=self.talent_page)
        self.conn.execute("UPDATE intelligence_entity_bindings SET notion_page_id = ?, "
                          "drive_url = 'https://drive.invalid/talent' WHERE entity_id = ?",
                          (self.talent_page, self.ids["activity:nexdoor_talent"]))
        self.conn.execute("UPDATE intelligence_entity_bindings SET notion_page_id = NULL "
                          "WHERE entity_id <> ?", (self.ids["activity:nexdoor_talent"],))
        # Outils : variantes d'un meme outil.
        cid = self.conn.execute("SELECT content_id FROM intelligence_sources WHERE id = ?",
                                (self.sources[0],)).fetchone()[0]
        for name in ("Make", "make.com", "Apify"):
            self.conn.execute("INSERT INTO resources (content_id, name, type, created_at, "
                              "updated_at) VALUES (?, ?, 'outil', ?, ?)",
                              (cid, name, fx.STAMP, fx.STAMP))
        self.conn.execute("UPDATE contents SET description_ai = 'Resume IA', category = "
                          "'Business & Marketing', transcript = ? WHERE id = ?",
                          ("mot " * 2500, cid))
        self.conn.commit()
        self.patches = [mock_attr(lib, "KNOWLEDGE", knowledge),
                        mock_attr(lib, "COMMAND_CENTER", command)]
        self.command, self.knowledge = command, knowledge
        self.notion = lib.Notion(inner=self.api, pause=0)

    def tearDown(self):
        for undo in self.patches:
            undo()
        self.conn.close()

    def page_of(self, source_id):
        return self.conn.execute("SELECT page_id FROM notion_library_links WHERE source_id = ?",
                                 (source_id,)).fetchone()[0]


def mock_attr(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    return lambda: setattr(obj, name, old)


class SourceSyncTests(LibraryCase):
    def test_first_sync_projects_every_source_once(self):
        counts = lib.sync_sources(self.conn, self.notion)
        self.assertEqual(counts["created"], len(self.sources))
        props = self.api.pages[self.page_of(self.sources[0])]["properties"]
        self.assertEqual(props["Vault ref"]["rich_text"][0]["text"]["content"],
                         f"source:{self.sources[0]}")
        self.assertTrue(props["Vault managed"]["checkbox"])
        self.assertEqual(props["Source ID"]["number"], self.sources[0])
        self.assertEqual([o["name"] for o in props["Projets"]["multi_select"]], ["Nexdoor Talent"])
        self.assertEqual(sorted(o["name"] for o in props["Outils"]["multi_select"]),
                         ["Apify", "Make"])  # Make / make.com -> un seul outil
        self.assertEqual(props["Thème principal"]["select"]["name"], "Business & Marketing")
        texts = self.api.texts(self.page_of(self.sources[0]))
        self.assertEqual(texts[0], "Resume IA")  # resume en haut
        self.assertIn("📜 Transcript complet", texts)

    def test_second_sync_is_idempotent_and_incremental(self):
        lib.sync_sources(self.conn, self.notion)
        writes = self.api.writes
        counts = lib.sync_sources(self.conn, self.notion)
        self.assertEqual(counts["unchanged"], len(self.sources))
        self.assertEqual(self.api.writes, writes)
        cid = self.conn.execute("SELECT content_id FROM intelligence_sources WHERE id = ?",
                                (self.sources[1],)).fetchone()[0]
        self.conn.execute("UPDATE contents SET description_ai = 'Nouveau resume' WHERE id = ?",
                          (cid,))
        self.conn.commit()
        page = self.page_of(self.sources[1])
        self.api._add(page, {"type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "Ma note perso"}}]}})
        counts = lib.sync_sources(self.conn, self.notion)
        self.assertEqual((counts["updated"], counts["body_updated"]), (1, 1))
        texts = self.api.texts(page)
        self.assertIn("Ma note perso", texts)  # contenu non gere par le Vault conserve
        self.assertIn("Nouveau resume", texts)

    def test_lost_local_links_never_create_duplicates(self):
        lib.sync_sources(self.conn, self.notion)
        pages = len(self.api.pages)
        self.conn.execute("DELETE FROM notion_library_links")
        self.conn.commit()
        lib.sync_sources(self.conn, self.notion)
        self.assertEqual(len(self.api.pages), pages)

    def test_sensitive_source_keeps_its_content_local(self):
        cid = self.conn.execute("SELECT content_id FROM intelligence_sources WHERE id = ?",
                                (self.sources[2],)).fetchone()[0]
        self.conn.execute("UPDATE contents SET category = 'Santé & bien-être', transcript = "
                          "'mon traitement' WHERE id = ?", (cid,))
        self.conn.commit()
        lib.sync_sources(self.conn, self.notion)
        props = self.api.pages[self.page_of(self.sources[2])]["properties"]
        self.assertEqual(props["Transcript preview"]["rich_text"], [])
        self.assertTrue(props["Sensibilité"]["select"]["name"].startswith("🔒"))
        self.assertNotIn("mon traitement", json.dumps(self.api.texts(self.page_of(self.sources[2]))))

    def test_tool_normalization(self):
        self.assertEqual(lib._normalize_tool("Make.com"), lib._normalize_tool("make"))
        self.assertEqual(lib._normalize_tool("Claude Code"), "claudecode")

    def test_stats(self):
        lib.sync_sources(self.conn, self.notion)
        stats = lib.stats(self.conn)
        self.assertEqual(stats["projetees_notion"], stats["sources_vault"])
        self.assertEqual(stats["avec_projet"], len(self.sources))


class ViewsProjectsPortalsTests(LibraryCase):
    def test_views_including_one_per_project(self):
        urls, errors = lib.ensure_views(self.conn, self.notion)
        self.assertEqual(errors, {})
        for name in ("🆕 Derniers contenus", "🧠 Par thème", "🛠️ Par outil", "💼 Par projet",
                     "🎬 Vidéos", "⭐ À exploiter", "📚 Tout le Vault", "💼 Nexdoor Talent"):
            self.assertIn(name, urls)
        again, _ = lib.ensure_views(self.conn, self.notion)
        self.assertEqual(len(self.api.views), len(urls))  # aucune vue en double
        talent = next(v for v in self.api.views.values() if v["name"] == "💼 Nexdoor Talent")
        self.assertEqual(talent["filter"]["multi_select"]["contains"], "Nexdoor Talent")

    def test_project_page_gets_a_managed_operating_section(self):
        lib.sync_sources(self.conn, self.notion)
        urls, _ = lib.ensure_views(self.conn, self.notion)
        result = lib.sync_projects(self.conn, self.notion, urls)
        self.assertEqual(result[self.ids["activity:nexdoor_talent"]], "created")
        texts = self.api.texts(self.talent_page)
        self.assertEqual(texts[0], "Fonction : recrutement")  # contenu existant intact
        for section in ("Vue d'ensemble", "Outils & stack", "Process / systèmes",
                        "Priorités actuelles", "Tâches actives", "Ressources du Vault", "SOP",
                        "Fichiers Drive", "Métadonnées"):
            self.assertIn(section, texts)
        self.assertIn("Cabinet de recrutement specialise principalement Tech, IA, Data, "
                      "Cyber, Cloud, avec activite internationale.", texts)
        self.assertIn("📄 Process Recrutement", texts)  # SOP existante
        self.assertIn("Manatal (outil du projet)", texts)
        self.assertIn("Dossier Drive du projet", texts)
        self.assertEqual(lib.sync_projects(self.conn, self.notion, urls)
                         [self.ids["activity:nexdoor_talent"]], "unchanged")

    def test_portals_are_inserted_without_touching_existing_content(self):
        lib.ensure_library(self.conn, self.notion)
        result = lib.sync_portals(self.conn, self.notion, {})
        self.assertEqual(result, {"command_center": "created", "knowledge": "created"})
        texts = self.api.texts(self.command)
        self.assertIn("📚 Explorer ma mémoire", texts)
        self.assertIn("💼 Mes activités & projets", texts)
        self.assertIn("Note de Rosy", texts)
        order = [self.api.blocks[b]["type"] for b in self.api.children_of[self.command]]
        self.assertEqual(order[:3], ["callout", "callout", "callout"])  # juste apres le 1er
        self.assertEqual(lib.sync_portals(self.conn, self.notion, {})["command_center"],
                         "unchanged")
        self.assertIn("🧭 Mentor Insights", self.api.texts(self.knowledge))


if __name__ == "__main__":
    unittest.main()
