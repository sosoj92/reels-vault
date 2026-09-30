import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import unittest
import uuid
from unittest import mock

import _life_fixtures as fx

import telegram_gateway as tg
import vault_db
import vault_ingestion as ing
from test_telegram_gateway import CHAT, USER, GatewayCase, msg

ARTICLE = """<html><head><title>Apify : scraper LinkedIn</title>
<meta property="og:description" content="Comment Apify automatise le sourcing.">
<meta name="author" content="Jane Doe"></head><body>
<p>Apify permet de construire des listes de candidats pour un cabinet de recrutement.</p>
<p>On connecte ensuite Manatal pour qualifier les profils recus automatiquement.</p>
<script>var x = 1;</script></body></html>"""


def fetcher(url):
    return ing.parse_article(ARTICLE, url)


def offline(url):
    raise OSError("reseau coupe")


class IngestionCase(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()
        self.path, self.conn, self.ids, _ = fx.life_db()
        cid = self.conn.execute("SELECT MIN(id) FROM contents").fetchone()[0]
        self.conn.execute("UPDATE contents SET category = 'Business & Marketing' WHERE id = ?",
                          (cid,))
        vault_db.ensure_subtheme(self.conn, "Prospection & génération de leads",
                                 "Business & Marketing")
        self.conn.commit()
        self.router = fx.FakeBackend()
        self.queue = _isolation.TMP / f"queue-{uuid.uuid4().hex[:6]}.txt"

    def tearDown(self):
        self.conn.close()


class UrlTests(IngestionCase):
    def test_video_url_uses_existing_pipeline_then_reconciles(self):
        url = "https://www.tiktok.com/@x/video/42"
        result = ing.ingest_url(self.conn, url, note="Garde ca pour Nexdoor Talent",
                                video_queue=self.queue, router=self.router)
        self.assertEqual((result["status"], result["project"]), ("queued_video", "Nexdoor Talent"))
        self.assertEqual(self.queue.read_text(encoding="utf-8").strip(), url)
        # Le pipeline video existant cree ensuite le contenu (simule).
        vault_db.upsert_content_from_import(self.conn, {"source_url": url, "title": "Video"})
        import vault_intelligence_bridge as bridge

        bridge.promote_all_contents(self.conn)
        self.assertEqual(ing.reconcile(self.conn), 1)
        item = ing.recent(self.conn, 1)[0]
        self.assertEqual(item["status"], "ingested")
        role = self.conn.execute("SELECT role FROM intelligence_entity_sources WHERE source_id = ? "
                                 "AND entity_id = ?", (item["source_id"],
                                                       self.ids["activity:nexdoor_talent"])).fetchone()
        self.assertEqual(role[0], "relevant_to")
        self.assertEqual(ing.ingest_url(self.conn, url, video_queue=self.queue)["status"],
                         "duplicate")

    def test_article_is_fetched_classified_and_linked(self):
        result = ing.ingest_url(self.conn, "https://blog.example.com/apify", fetcher=fetcher,
                                router=self.router)
        self.assertEqual((result["status"], result["kind"]), ("stored", "article"))
        content = vault_db.get_content(self.conn, result["content_id"])
        self.assertEqual(content["title"], "Apify : scraper LinkedIn")
        self.assertEqual(content["author"], "Jane Doe")
        self.assertEqual(content["platform"], "blog.example.com")
        self.assertIn("Manatal", content["transcript"])
        self.assertNotIn("var x", content["transcript"])
        classification = result["classification"]
        self.assertEqual(classification["category"], "Business & Marketing")
        self.assertEqual(classification["subthemes"], ["Prospection & génération de leads"])
        self.assertEqual(classification["tools"], ["Manatal"])  # outil absent du texte ecarte
        self.assertIsNotNone(result["source_id"])
        self.assertEqual(self.router.routing["task"], "light")

    def test_offline_article_keeps_the_link_and_does_not_block(self):
        result = ing.ingest_url(self.conn, "https://offline.example.com/a", fetcher=offline,
                                router=fx.FakeBackend(available=False))
        self.assertEqual(result["status"], "stored")
        self.assertIsNone(result["classification"])
        item = ing.recent(self.conn, 1)[0]
        self.assertIn("recuperation impossible", item["error"])
        self.assertEqual(item["status"], "ingested")
        self.assertEqual(ing.retry_pending_classification(self.conn, router=self.router), 1)


class NoteAndFileTests(IngestionCase):
    def test_note_with_explicit_project(self):
        result = ing.ingest_note(self.conn, "Garde ca pour Nexdoor Talent : sourcing data via "
                                            "Manatal et LinkedIn", router=self.router)
        self.assertEqual((result["status"], result["project"]), ("stored", "Nexdoor Talent"))
        content = vault_db.get_content(self.conn, result["content_id"])
        self.assertTrue(content["source_url"].startswith("note://telegram/"))
        self.assertEqual(content["content_type"], "note")

    def test_secret_is_never_stored_nor_sent(self):
        before = self.conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0]
        result = ing.ingest_note(self.conn, "note : mot de passe Manatal = Sup3rSecret!",
                                 router=self.router)
        self.assertEqual(result["status"], "rejected_secret")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0], before)
        self.assertEqual(self.router.calls, [])

    def test_sensitive_note_is_classified_locally_only(self):
        result = ing.ingest_note(self.conn, "note : mon traitement TDAH change lundi",
                                 router=self.router)
        self.assertEqual(result["sensitivity"], "sensitive")
        self.assertEqual(self.router.routing["sensitivity"], "sensitive")

    def test_local_inbox_files_and_sensitive_folder(self):
        root = _isolation.TMP / f"inbox-{uuid.uuid4().hex[:6]}"
        (root / "files").mkdir(parents=True)
        (root / "sensitive").mkdir(parents=True)
        (root / "files" / "veille.md").write_text("Apify et Manatal pour le sourcing.",
                                                  encoding="utf-8")
        (root / "sensitive" / "bilan.txt").write_text("Compte rendu medical.", encoding="utf-8")
        results = ing.process_local_inbox(self.conn, root, router=self.router)
        self.assertEqual([r["status"] for r in results], ["stored", "stored"])
        self.assertEqual(results[1]["sensitivity"], "sensitive")
        self.assertEqual(list((root / "files").iterdir()), [])  # deplaces, jamais supprimes
        self.assertEqual(len(list((root / "processed").rglob("*.*"))), 2)
        again = ing.process_local_inbox(self.conn, root, router=self.router)
        self.assertEqual(again, [])

    def test_metadata_is_kept(self):
        ing.ingest_note(self.conn, "idee : offre sourcing IA", telegram_message_id="77",
                        router=self.router)
        item = ing.recent(self.conn, 1)[0]
        for field in ("channel", "captured_at", "sensitivity", "source_type",
                      "telegram_message_id", "original_url"):
            self.assertIsNotNone(item[field], field)
        self.assertEqual((item["channel"], item["telegram_message_id"]), ("telegram", "77"))


class TelegramIngestionTests(GatewayCase):
    def setUp(self):
        super().setUp()
        self.files = _isolation.TMP / f"tg-files-{uuid.uuid4().hex[:6]}"
        patcher = mock.patch.object(tg, "TELEGRAM_FILES", self.files)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_note_command_and_short_confirmation(self):
        self.assertEqual(self.gw.handle_update(msg("Garde ca pour Nexdoor Talent : sourcing "
                                                   "data avec Manatal")), "saved:stored")
        text = self.last()["text"]
        self.assertTrue(text.startswith("✅ Ajoute au Vault\n📝 Note"))
        self.assertIn("💼 Nexdoor Talent", text)
        self.assertLessEqual(text.count("\n"), 5)

    def test_document_is_downloaded_and_ingested(self):
        self.telegram.files["doc-1"] = b"Process de qualification des candidats data."
        update = {"update_id": 3, "message": {"message_id": 3, "from": {"id": USER},
                                              "chat": {"id": CHAT}, "caption": "SOP qualif",
                                              "document": {"file_id": "doc-1",
                                                           "file_name": "qualif.txt"}}}
        self.assertEqual(self.gw.handle_update(update), "file:stored")
        self.assertEqual(len(list(self.files.rglob("*qualif.txt"))), 1)
        self.assertIn("📄 Document", self.last()["text"])

    def test_inbox_save_and_project_commands(self):
        self.gw.handle_update(msg("idee : offre de sourcing IA pour PME", message_id=5))
        confirmation = self.last()["message_id"]
        self.assertEqual(self.gw.handle_update(msg("/inbox", message_id=6)), "inbox:1")
        self.assertEqual(self.gw.handle_update(msg("/project Nexdoor Talent",
                                                   reply_to=confirmation, message_id=7)),
                         "project_linked")
        update = msg("/save", message_id=8)
        update["message"]["reply_to_message"] = {"message_id": 4, "text": "Penser a Manatal"}
        self.assertEqual(self.gw.handle_update(update), "saved:stored")

    def test_secret_via_telegram_is_refused(self):
        self.gw.handle_update(msg("note : api_key = sk-abcdefghijklmnopqrstuvwxyz123"))
        self.assertIn("secret", self.last()["text"])

    def test_questions_are_not_saved(self):
        self.assertTrue(self.gw.handle_update(msg("Que sais-je sur le sourcing ?"))
                        .startswith("answer"))


if __name__ == "__main__":
    unittest.main()
