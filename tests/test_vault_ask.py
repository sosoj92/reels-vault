import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import sqlite3
import os
import time
import unittest

import vault_ask
import vault_intelligence as vi
import vault_intelligence_context as context
from vault_db import SCHEMA as V2_SCHEMA


NOW = "2026-09-28T00:00:00+00:00"


class AskFixture(unittest.TestCase):
    """Mini Vault realiste : schema V2 + V3, quelques sources."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(V2_SCHEMA)
        vi.ensure_schema(self.conn)
        self.conn.execute(
            """
            CREATE TABLE intelligence_mentor_processing (
                source_id INTEGER PRIMARY KEY, content_id INTEGER,
                outcome TEXT NOT NULL, relevance_score REAL NOT NULL,
                max_similarity REAL NOT NULL DEFAULT 0, matched_source_id INTEGER,
                entity_ids TEXT NOT NULL DEFAULT '[]', processed_at TEXT NOT NULL
            )
            """
        )
        self.rtk = self._content(
            "https://vm.tiktok.com/rtk/",
            "Économise tes tokens IA grâce à RTK ! #productivity #ia",
            author="powered.by.ai2",
            description_original="Économise tes tokens IA grâce à RTK ! #ia",
            transcript=(
                "Les gars c'est un truc de ouf. Pour faire simple RTK, ça évite "
                "à l'IA de lire des tonnes de textes inutiles et donc "
                "d'économiser jusqu'à 90% de tes tokens. Abonne-toi."
            ),
        )
        self.astra = self._content(
            "https://vm.tiktok.com/astra/",
            "Astra-Flash Orchestrator : réduire de ~95 % sa consommation",
            author="ethanplusai",
            category="IA & Tech",
            description_ai="Un repo GitHub qui installe des modèles moins chers.",
            tags=["réduction des coûts", "Codex"],
        )
        self.linkedin = self._content(
            "https://vm.tiktok.com/linkedin/",
            "Prospection de marques avec Claude sur LinkedIn",
            author="h_in_hec",
            category="Business & Marketing",
            description_ai="Tutoriel pour automatiser la recherche de clients.",
            resources=["Claude", "Exa"],
        )
        self.adhd = self._content(
            "https://vm.tiktok.com/adhd/",
            "Exploring guanfacine vs. clonidine for ADHD!",
            author="genia.thon",
            description_original="Exploring guanfacine vs. clonidine for ADHD!",
        )
        self.noise = self._content(
            "https://vm.tiktok.com/noise/",
            "Mes vidéos sauvegardées préférées",
            author="someone",
            transcript="Sauvegarde cette vidéo et retrouve-la plus tard.",
        )
        self.conn.execute(
            "INSERT INTO intelligence_mentor_processing"
            "(source_id, content_id, outcome, relevance_score, processed_at)"
            " VALUES (?, ?, 'opportunity', 0.75, ?)",
            (self.rtk, self.rtk, NOW),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _content(self, url, title, *, author=None, category=None,
                 description_original=None, description_ai=None,
                 transcript=None, tags=(), resources=()):
        cur = self.conn.execute(
            """
            INSERT INTO contents(
                source_url, platform, author, title, description_original,
                description_ai, transcript, category, created_at, updated_at
            ) VALUES (?, 'tiktok', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (url, author, title, description_original, description_ai,
             transcript, category, NOW, NOW),
        )
        content_id = cur.lastrowid
        for tag in tags:
            self.conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
            tag_id = self.conn.execute(
                "SELECT id FROM tags WHERE name = ?", (tag,)
            ).fetchone()[0]
            self.conn.execute(
                "INSERT INTO content_tags(content_id, tag_id) VALUES (?, ?)",
                (content_id, tag_id),
            )
        for name in resources:
            self.conn.execute(
                "INSERT INTO resources(content_id, name, type, created_at, updated_at)"
                " VALUES (?, ?, 'outil', ?, ?)",
                (content_id, name, NOW, NOW),
            )
        source_id = vi.upsert_source(
            self.conn,
            source_url=url,
            platform="tiktok",
            source_type="video",
            title=title,
            author=author,
            content_id=content_id,
        )
        self.assertEqual(source_id, content_id)
        return source_id


class SourceFirstSearchTests(AskFixture):
    def test_token_question_ranks_rtk_first_with_required_fields(self):
        results = context.search_saved_sources(
            self.conn,
            "Qu'est-ce que j'avais sauvegardé sur les outils pour économiser des tokens ?",
        )
        self.assertTrue(results)
        self.assertEqual(results[0]["source_id"], self.rtk)
        for key in ("source_id", "content_id", "title", "url", "platform",
                    "author", "category", "snippet", "matched_tokens", "score"):
            self.assertIn(key, results[0])
        self.assertIn("economiser", results[0]["matched_tokens"])
        self.assertIn("tokens", results[0]["matched_tokens"])

    def test_recall_words_do_not_drive_ranking(self):
        results = context.search_saved_sources(
            self.conn, "Qu'est-ce que j'avais sauvegardé dans mes vidéos sur RTK ?",
        )
        ids = [item["source_id"] for item in results]
        self.assertEqual(ids, [self.rtk])
        self.assertNotIn(self.noise, ids)

    def test_accents_and_french_inflections(self):
        results = context.search_saved_sources(self.conn, "economise tokens")
        self.assertEqual(results[0]["source_id"], self.rtk)
        results = context.search_saved_sources(self.conn, "réduire la consommation")
        self.assertEqual(results[0]["source_id"], self.astra)

    def test_bilingual_tdah(self):
        results = context.search_saved_sources(self.conn, "Consulte mon Vault sur le TDAH")
        self.assertEqual([item["source_id"] for item in results], [self.adhd])

    def test_taxonomy_and_resources_are_searched(self):
        results = context.search_saved_sources(self.conn, "réduction des coûts")
        self.assertEqual(results[0]["source_id"], self.astra)
        self.assertIn("taxonomy", results[0]["matched_fields"])
        results = context.search_saved_sources(self.conn, "Exa")
        self.assertEqual(results[0]["source_id"], self.linkedin)
        self.assertIn("resources", results[0]["matched_fields"])

    def test_no_result_for_absent_topic(self):
        self.assertEqual(
            context.search_saved_sources(
                self.conn, "Qu'est-ce que j'avais sauvegardé sur la tarte aux pommes ?",
            ),
            [],
        )

    def test_short_terms_need_exact_word(self):
        # "ia" ne doit pas matcher "via" ou "media".
        self.assertTrue(context._FieldIndex("tokens IA").matches("ia"))
        self.assertFalse(context._FieldIndex("via media").matches("ia"))

    def test_tuple_connection_is_supported_and_restored(self):
        self.conn.row_factory = None
        pack = context.build_context_pack(self.conn, "économiser des tokens")
        self.assertIsNone(self.conn.row_factory)
        self.assertEqual(pack["sources"][0]["source_id"], self.rtk)
        self.assertEqual(pack["retrieval"], "source_first")
        self.assertEqual(
            pack["capabilities"]["contradiction_detection"],
            "unavailable_no_structured_claims",
        )


class AskTests(AskFixture):
    def test_deterministic_answer_cites_titles_and_urls(self):
        result = vault_ask.ask(
            self.conn,
            "Qu'est-ce que j'avais sauvegardé sur les outils pour économiser des tokens ?",
            llm="off",
        )
        answer = result["answer"]
        self.assertTrue(answer.startswith("Tu avais notamment sauvegardé"))
        self.assertIn("RTK", answer)
        self.assertIn("https://vm.tiktok.com/rtk/", answer)
        # Extrait du transcript : la phrase utile, pas le hook.
        self.assertIn("économiser jusqu'à 90% de tes tokens", answer)
        self.assertIn("opportunité", answer)
        self.assertEqual(result["sources"][0]["source_id"], self.rtk)
        self.assertFalse(result["llm"]["used"])
        self.assertEqual(result["llm"]["status"], "disabled")

    def test_long_question_keeps_original_and_compacts_retrieval(self):
        question = (
            "Analyse mon Life Intelligence System et propose des solutions concretes. "
            + "Je veux une analyse detaillee avec priorites, contraintes et alternatives. " * 12
            + "A la fin, concentre la recherche du Vault sur RTK pour economiser des tokens."
        )
        result = vault_ask.ask(
            self.conn, question, llm="off", semantic="off",
        )
        self.assertEqual(result["question"], " ".join(question.split()))
        self.assertLessEqual(
            len(result["retrieval_query"]), vault_ask.MAX_RETRIEVAL_QUERY_CHARS,
        )
        self.assertIn("rtk", result["retrieval_query"])
        self.assertIn("tokens", result["retrieval_query"])
        self.assertEqual(result["sources"][0]["source_id"], self.rtk)

    def test_no_result_answer_does_not_invent(self):
        result = vault_ask.ask(
            self.conn,
            "Qu'est-ce que j'avais sauvegardé sur la tarte aux pommes ?",
        )
        self.assertEqual(result["sources"], [])
        self.assertIn("aucune source suffisamment pertinente", result["answer"])
        self.assertIn("tarte aux pommes", result["answer"])
        self.assertEqual(result["llm"]["status"], "skipped_no_sources")

    def test_health_topic_adds_epistemic_caution(self):
        original = (vault_ask._ollama_available, vault_ask._ollama_chat)
        vault_ask._ollama_available = lambda timeout=1.5: True
        vault_ask._ollama_chat = lambda *a, **k: "affirmation inventee"
        try:
            result = vault_ask.ask(
                self.conn, "Consulte mon Vault sur le TDAH", mode="consult",
            )
        finally:
            vault_ask._ollama_available, vault_ask._ollama_chat = original
        # Pas de synthese LLM sur un sujet de sante.
        self.assertEqual(result["llm"]["status"], "skipped_sensitive")
        self.assertNotIn("affirmation inventee", result["answer"])
        self.assertTrue(result["sensitive"])
        self.assertIn("Prudence", result["answer"])
        self.assertIn("pas des preuves vérifiées", result["answer"])
        self.assertIn("détection de contradictions n'est pas encore disponible",
                      result["answer"])

    def test_llm_unreachable_falls_back_immediately(self):
        # Reseau interdit par _isolation : Ollama est injoignable.
        started = time.perf_counter()
        result = vault_ask.ask(self.conn, "économiser des tokens")
        self.assertLess(time.perf_counter() - started, 5)
        self.assertEqual(result["llm"]["status"], "unavailable")
        self.assertIn("RTK", result["answer"])

    def test_llm_timeout_returns_deterministic_answer(self):
        # Synthetiseur local explicite (le defaut est desormais le routeur LLM).
        os.environ['VAULT_ASK_SYNTHESIZER'] = 'local'
        self.addCleanup(os.environ.pop, 'VAULT_ASK_SYNTHESIZER', None)
        original = (vault_ask._ollama_available, vault_ask._ollama_chat)
        vault_ask._ollama_available = lambda timeout=1.5: True
        vault_ask._ollama_chat = lambda *a, **k: time.sleep(3) or "trop tard"
        try:
            started = time.perf_counter()
            result = vault_ask.ask(self.conn, "économiser des tokens", llm_timeout=0.3)
            elapsed = time.perf_counter() - started
        finally:
            vault_ask._ollama_available, vault_ask._ollama_chat = original
        self.assertLess(elapsed, 2.5)
        self.assertEqual(result["llm"]["status"], "timeout")
        self.assertNotIn("Synthèse", result["answer"])
        self.assertIn("https://vm.tiktok.com/rtk/", result["answer"])

    def test_llm_synthesis_is_appended_when_fast(self):
        # Synthetiseur local explicite (le defaut est desormais le routeur LLM).
        os.environ['VAULT_ASK_SYNTHESIZER'] = 'local'
        self.addCleanup(os.environ.pop, 'VAULT_ASK_SYNTHESIZER', None)
        original = (vault_ask._ollama_available, vault_ask._ollama_chat)
        vault_ask._ollama_available = lambda timeout=1.5: True
        vault_ask._ollama_chat = lambda *a, **k: "RTK réduit la lecture inutile [1]."
        try:
            result = vault_ask.ask(self.conn, "économiser des tokens")
        finally:
            vault_ask._ollama_available, vault_ask._ollama_chat = original
        self.assertTrue(result["llm"]["used"])
        self.assertIn("Synthèse locale", result["answer"])
        self.assertIn("RTK réduit la lecture inutile", result["answer"])

    def test_truncated_synthesis_is_cut_to_last_sentence(self):
        self.assertEqual(
            vault_ask._complete_sentences("Phrase une [1]. Phrase deux coupee au"),
            "Phrase une [1].",
        )
        self.assertEqual(vault_ask._complete_sentences("Complet."), "Complet.")

    def test_readonly_connection(self):
        path = _isolation.TMP / "ask_readonly.db"
        disk = sqlite3.connect(path)
        self.conn.backup(disk)
        disk.close()
        conn = vault_ask.connect_readonly(path)
        try:
            result = vault_ask.ask(conn, "RTK", llm="off")
        finally:
            conn.close()
        self.assertEqual(result["sources"][0]["source_id"], self.rtk)


class AskApiTests(unittest.TestCase):
    def test_api_endpoint_returns_cited_sources(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from fastapi.testclient import TestClient

        import vault_api
        import vault_db

        tmp = Path(tempfile.mkdtemp(dir=_isolation.TMP))
        with mock.patch.object(vault_db, "DB_PATH", tmp / "vault.db"):
            conn = vault_db.connect()
            vi.ensure_schema(conn)
            content_id, _ = vault_db.upsert_content_from_import(conn, {
                "source_url": "https://vm.tiktok.com/rtk/",
                "title": "Économise tes tokens IA grâce à RTK",
                "platform": "TikTok",
            })
            vi.upsert_source(
                conn, source_url="https://vm.tiktok.com/rtk/",
                platform="tiktok", title="RTK", content_id=content_id,
            )
            conn.commit()
            conn.close()
            api = TestClient(vault_api.app)
            response = api.post(
                "/intelligence/ask",
                json={"question": "Qu'est-ce que j'avais sauvegardé sur les tokens ?"},
            )
            empty = api.post("/intelligence/ask", json={"question": "   "})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["sources"][0]["url"], "https://vm.tiktok.com/rtk/")
        self.assertIn("RTK", data["answer"])
        self.assertEqual(data["llm"]["status"], "disabled")
        self.assertEqual(empty.status_code, 422)


def tearDownModule():
    # Journal du routeur LLM partage : ferme a la fin du module (aucune connexion residuelle).
    import llm_router

    llm_router.close_shared()


if __name__ == "__main__":
    unittest.main()
