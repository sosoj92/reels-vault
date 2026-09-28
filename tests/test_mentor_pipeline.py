import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import io
import sqlite3
import unittest
from contextlib import redirect_stdout
from unittest import mock

import mentor_decision
import mentor_notion_sync
import mentor_review
import mentor_worker
import vault_intelligence as vi


class DecisionTests(unittest.TestCase):
    def test_source_64_profile_is_publishable_without_cloud(self):
        # Valeurs observees sur la source 64 (RTK) avec qwen3:1.7b + Agency.
        result = mentor_decision.finalize(
            heuristic_score=0.75,
            current_outcome="opportunity",
            qwen={"classification": "opportunity", "relevance": 0.23,
                  "confidence": 0.23, "needs_agency": True},
            agency={"classification": "opportunity", "relevance": 0.65,
                    "confidence": 0.75, "needs_cloud": True,
                    "routes": [{"route": "tool_automation"}]},
            needs_agency=True,
        )
        self.assertEqual(result["outcome"], "opportunity")
        self.assertAlmostEqual(result["relevance_score"], 0.612, places=3)
        self.assertEqual(result["final_confidence"], 0.75)
        self.assertFalse(result["needs_cloud"])
        self.assertTrue(result["publishable"])
        self.assertEqual(result["anti_noise_reasons"], [])

    def test_sensitive_route_asking_cloud_goes_to_review(self):
        result = mentor_decision.finalize(
            heuristic_score=0.6,
            current_outcome="novelty",
            qwen={"relevance": 0.6, "confidence": 0.6},
            agency={"classification": "novelty", "relevance": 0.7,
                    "confidence": 0.95, "needs_cloud": True,
                    "routes": [{"route": "research_evidence"}]},
            needs_agency=True,
        )
        self.assertTrue(result["needs_cloud"])
        self.assertFalse(result["publishable"])
        self.assertIn("awaiting_cloud", result["anti_noise_reasons"])

    def test_agency_needed_but_missing_is_not_published(self):
        result = mentor_decision.finalize(
            heuristic_score=0.9,
            current_outcome="opportunity",
            qwen={"relevance": 0.9, "confidence": 0.9},
            agency=None,
            needs_agency=True,
        )
        self.assertTrue(result["needs_cloud"])
        self.assertFalse(result["publishable"])

    def test_low_signal_is_capped(self):
        result = mentor_decision.finalize(
            heuristic_score=0.95,
            current_outcome="low_signal",
        )
        self.assertLessEqual(result["relevance_score"], 0.44)
        self.assertFalse(result["publishable"])
        self.assertIn("low_signal", result["anti_noise_reasons"])


def _mentor_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE contents (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    vi.ensure_schema(conn)
    mentor_worker.ensure_schema(conn)
    mentor_review.ensure_schema(conn)
    return conn


class ReviewQueueTests(unittest.TestCase):
    def test_enqueue_is_an_idempotent_upsert(self):
        conn = _mentor_db(":memory:")
        row = {"source_id": 8, "content_id": 8}
        analysis = {"analysis_model": "qwen3:1.7b+agency", "score": 0.68,
                    "final_confidence": 0.95, "agency_agents": ["Statistician"]}
        mentor_review.enqueue(conn, row, analysis)
        mentor_review.enqueue(conn, row, {**analysis, "score": 0.7})
        rows = conn.execute(
            "SELECT source_id, status, reason, relevance_score "
            "FROM intelligence_mentor_review_queue"
        ).fetchall()
        self.assertEqual(
            [tuple(r) for r in rows],
            [(8, "review_required", "cloud_escalation_required", 0.7)],
        )


class FakeNotion:
    created = []
    fail_on = None

    def __init__(self, data_source_id=None):
        pass

    def create_page(self, props):
        title = props["Insight"]["title"][0]["text"]["content"]
        if title == FakeNotion.fail_on:
            raise RuntimeError("Notion indisponible")
        FakeNotion.created.append(title)
        return {"id": f"page-{len(FakeNotion.created)}", "url": "https://notion.test"}


class NotionSyncTests(unittest.TestCase):
    def setUp(self):
        self.path = _isolation.TMP / f"mentor_sync_{self._testMethodName}.db"
        conn = _mentor_db(self.path)
        for index, title in enumerate(("Premier", "Deuxieme", "Troisieme"), 1):
            source_id = vi.upsert_source(
                conn, source_url=f"https://x.test/{index}", title=title,
            )
            conn.execute(
                """
                INSERT INTO intelligence_mentor_insights_local(
                    source_id, insight_type, observation, relevance_score,
                    created_at, updated_at, analysis_model
                ) VALUES (?, 'opportunity', 'obs', 0.7, 'now', 'now', ?)
                """,
                (source_id, "qwen3:1.7b+agency"),
            )
        conn.commit()
        conn.close()
        FakeNotion.created = []
        FakeNotion.fail_on = None
        self.patches = [
            mock.patch.object(mentor_notion_sync, "DB", str(self.path)),
            mock.patch.object(mentor_notion_sync, "NotionCockpitClient", FakeNotion),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def run_sync(self, confirm=True):
        with redirect_stdout(io.StringIO()):
            mentor_notion_sync.run(limit=20, confirm=confirm)

    def synced(self):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM intelligence_mentor_insights_local "
                "WHERE notion_page_id IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()

    def test_sync_is_idempotent(self):
        self.run_sync()
        self.assertEqual(FakeNotion.created, ["Premier", "Deuxieme", "Troisieme"])
        self.run_sync()
        self.assertEqual(len(FakeNotion.created), 3)
        self.assertEqual(self.synced(), 3)

    def test_failure_midway_does_not_duplicate_on_retry(self):
        FakeNotion.fail_on = "Deuxieme"
        with self.assertRaises(RuntimeError):
            self.run_sync()
        # La premiere page creee reste marquee malgre l'erreur suivante.
        self.assertEqual(self.synced(), 1)
        FakeNotion.fail_on = None
        self.run_sync()
        self.assertEqual(FakeNotion.created, ["Premier", "Deuxieme", "Troisieme"])
        self.assertEqual(self.synced(), 3)

    def test_model_column_uses_real_analysis_model(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            mentor_notion_sync.ensure_schema(conn)
            row = mentor_notion_sync.pending(conn, 1)[0]
        finally:
            conn.close()
        props = mentor_notion_sync.props(row)
        self.assertEqual(
            props["Modèle"]["rich_text"][0]["text"]["content"],
            "qwen3:1.7b+agency",
        )

    def test_dry_run_writes_nothing(self):
        self.run_sync(confirm=False)
        self.assertEqual(FakeNotion.created, [])
        self.assertEqual(self.synced(), 0)


class AgencyDegradationTests(unittest.TestCase):
    def test_missing_agency_playbooks_returns_none_instead_of_crashing(self):
        import mentor_agency

        with mock.patch.object(
            mentor_agency.agency_router,
            "build_orchestration_context",
            side_effect=RuntimeError("Agency Agents absent"),
        ), redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertIsNone(mentor_agency.analyze("un texte"))

        # Sans Agency alors qu'elle etait requise : review, jamais publie.
        result = mentor_decision.finalize(
            heuristic_score=0.9,
            current_outcome="opportunity",
            qwen={"relevance": 0.9, "confidence": 0.9},
            agency=None,
            needs_agency=True,
        )
        self.assertTrue(result["needs_cloud"])
        self.assertFalse(result["publishable"])


class WorkerPathTests(unittest.TestCase):
    def test_db_path_is_absolute(self):
        from pathlib import Path
        self.assertTrue(Path(mentor_worker.DB).is_absolute())
        self.assertTrue(Path(mentor_notion_sync.DB).is_absolute())


if __name__ == "__main__":
    unittest.main()
