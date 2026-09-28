import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import io
import json
import sqlite3
import unittest
from contextlib import redirect_stdout
from unittest import mock

import mentor_consensus as consensus
import mentor_review
import mentor_worker
from _v2_fixtures import add_source, make_db

QWEN = {"classification": "novelty", "relevance": 0.6, "confidence": 0.7,
        "needs_agency": True}


def agency(classification="opportunity", relevance=0.72, confidence=0.78,
           needs_cloud=False, variant=1):
    return {"classification": classification, "relevance": relevance,
            "confidence": confidence, "needs_cloud": needs_cloud,
            "agents": ["Tool Evaluator"], "routes": [{"route": "tool_automation"}],
            "variant": variant}


TEXT = ("Un outil open source pour automatiser la prospection LinkedIn avec un agent IA : "
        "workflow, CRM, leads, clients, automation, api, business. " * 3)


class RuleTests(unittest.TestCase):
    def test_borderline_definition(self):
        decision = {"relevance_score": 0.612, "needs_cloud": False}
        self.assertIn("score_near_threshold",
                      consensus.borderline_reasons(None, agency(confidence=0.9), decision))
        self.assertIn("confidence_near_threshold",
                      consensus.borderline_reasons(None, agency(confidence=0.62),
                                                   {"relevance_score": 0.9}))
        self.assertIn("qwen_agency_disagree",
                      consensus.borderline_reasons(QWEN, agency(), {"relevance_score": 0.9}))
        self.assertIn("agency_requested_verification",
                      consensus.borderline_reasons(
                          None, agency(needs_cloud=True, confidence=0.9),
                          {"relevance_score": 0.9, "needs_cloud": False}))
        # Verdict net : pas de seconde analyse.
        self.assertEqual(
            consensus.borderline_reasons(
                {"classification": "opportunity"}, agency(confidence=0.9),
                {"relevance_score": 0.85, "needs_cloud": False}),
            [])

    def test_agreement_thresholds(self):
        ok, _, reasons = consensus.agreement(agency(relevance=0.70), agency(relevance=0.60))
        self.assertTrue(ok, reasons)
        ok, _, reasons = consensus.agreement(agency(relevance=0.70), agency(relevance=0.50))
        self.assertIn("relevance_gap_too_large", reasons)
        ok, _, reasons = consensus.agreement(agency(), agency(classification="low_signal"))
        self.assertIn("classification_disagreement", reasons)
        ok, _, reasons = consensus.agreement(agency(confidence=0.80), agency(confidence=0.50))
        self.assertIn("low_confidence_run", reasons)
        ok, _, reasons = consensus.agreement(agency(), None)
        self.assertEqual(reasons, ["second_run_failed"])

    def test_consensus_uses_conservative_values(self):
        result = consensus.build_consensus(
            heuristic_score=0.75, current_outcome="opportunity", qwen=QWEN,
            run1=agency(relevance=0.80, confidence=0.80),
            run2=agency(relevance=0.70, confidence=0.72, variant=2),
            needs_agency=True,
        )
        self.assertTrue(result["publishable"])
        self.assertEqual(result["consensus"]["conservative"]["relevance"], 0.70)
        self.assertEqual(result["consensus"]["conservative"]["confidence"], 0.72)
        self.assertEqual(result["consensus"]["decision"], "publish")


class WorkerConsensusTests(unittest.TestCase):
    def setUp(self):
        self.path = _isolation.TMP / f"consensus_{self._testMethodName}.db"
        conn = make_db(str(self.path))
        self.source_id = add_source(conn, "https://example.test/borderline",
                                    "Outil de prospection LinkedIn", transcript=TEXT)
        conn.close()
        self.calls = []
        self.patches = [
            mock.patch.object(mentor_worker, "DB", str(self.path)),
            mock.patch.object(mentor_worker, "ollama_available", return_value=True),
            mock.patch.object(mentor_worker, "qwen_analyze", return_value=dict(QWEN)),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def run_worker(self, run2, run1=None):
        run1 = run1 or agency()

        def fake_analyze(text, **kwargs):
            self.calls.append(kwargs.get("variant", 1))
            return dict(run1) if kwargs.get("variant", 1) == 1 else (
                dict(run2) if run2 else None)

        with mock.patch.object(mentor_worker.mentor_agency, "analyze", side_effect=fake_analyze):
            out = io.StringIO()
            with redirect_stdout(out):
                mentor_worker.run(limit=5, confirm=True)
        return json.loads(out.getvalue())["items"][0]

    def db(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # V2-7a : desaccord entre les deux analyses -> review, jamais publie.
    def test_disagreement_goes_to_review(self):
        item = self.run_worker(agency(classification="low_signal", relevance=0.3, variant=2))
        self.assertEqual(self.calls, [1, 2])
        self.assertFalse(item["publishable"])
        self.assertFalse(item["would_create_insight"])
        self.assertTrue(item["review_required"])
        self.assertTrue(item["review_reason"].startswith("agency_disagreement"))
        conn = self.db()
        try:
            queue = conn.execute(
                "SELECT status, reason FROM intelligence_mentor_review_queue").fetchone()
            self.assertEqual(queue["status"], "review_required")
            self.assertIn("classification_disagreement", queue["reason"])
            trace = conn.execute("SELECT * FROM intelligence_mentor_consensus").fetchone()
            self.assertEqual(trace["decision"], "review")
            self.assertEqual(json.loads(trace["agency_run_1"])["variant"], 1)
            self.assertEqual(json.loads(trace["agency_run_2"])["variant"], 2)
            self.assertIn("qwen_agency_disagree", json.loads(trace["borderline_reasons"]))
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM intelligence_mentor_insights_local")
                .fetchone()[0], 0)
        finally:
            conn.close()

    # V2-7b : accord suffisant -> decision autorisee selon les seuils.
    def test_agreement_allows_publication(self):
        item = self.run_worker(agency(relevance=0.70, confidence=0.74, variant=2))
        self.assertEqual(self.calls, [1, 2])
        self.assertTrue(item["publishable"], item)
        self.assertTrue(item["would_create_insight"])
        self.assertFalse(item["review_required"])
        self.assertEqual(item["consensus"]["decision"], "publish")
        self.assertEqual(item["analysis_model"], "qwen3:1.7b+agency x2")
        conn = self.db()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM intelligence_mentor_insights_local")
                .fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM intelligence_mentor_review_queue")
                .fetchone()[0], 0)
        finally:
            conn.close()

    def test_second_run_failure_goes_to_review(self):
        item = self.run_worker(None)
        self.assertTrue(item["review_required"])
        self.assertIn("second_run_failed", item["review_reason"])

    def test_dry_run_on_existing_base_does_not_alter_schema(self):
        # Base "existante" : tables Mentor V1 presentes, tables V2 absentes.
        conn = sqlite3.connect(self.path)
        mentor_worker.ensure_schema(conn)
        conn.close()

        def schema():
            c = sqlite3.connect(self.path)
            try:
                tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
                columns = {r[1] for r in c.execute(
                    "PRAGMA table_info(intelligence_mentor_review_queue)")}
                return tables, columns
            finally:
                c.close()

        before = schema()
        with mock.patch.object(mentor_worker.mentor_agency, "analyze",
                               return_value=agency(relevance=0.95, confidence=0.95)):
            out = io.StringIO()
            with redirect_stdout(out):
                mentor_worker.run(source_id=self.source_id)
        self.assertFalse(json.loads(out.getvalue())["writes_performed"])
        self.assertEqual(schema(), before)
        self.assertNotIn("intelligence_mentor_consensus", before[0])

    def test_clear_case_runs_agency_once(self):
        run1 = agency(relevance=0.95, confidence=0.95)
        with mock.patch.object(mentor_worker, "qwen_analyze",
                               return_value={**QWEN, "classification": "opportunity"}):
            item = self.run_worker(None, run1=run1)
        self.assertEqual(self.calls, [1])
        self.assertIsNone(item["consensus"])
        self.assertTrue(item["publishable"])


class ReassessExistingSourcesTests(unittest.TestCase):
    """Deploiement V2 : re-evaluation des sources deja traitees en V1."""

    def setUp(self):
        self.path = _isolation.TMP / f"reassess_{self._testMethodName}.db"
        conn = make_db(str(self.path))
        self.source_id = add_source(conn, "https://example.test/old",
                                    "Outil de prospection LinkedIn", transcript=TEXT)
        mentor_worker.ensure_schema(conn)
        import mentor_notion_sync
        mentor_notion_sync.ensure_schema(conn)
        conn.execute(
            "INSERT INTO intelligence_mentor_processing(source_id, content_id, outcome, "
            "relevance_score, processed_at) VALUES (?, ?, 'opportunity', 0.75, 'v1')",
            (self.source_id, self.source_id))
        conn.execute(
            "INSERT INTO intelligence_mentor_insights_local(source_id, insight_type, observation, "
            "relevance_score, created_at, updated_at, notion_page_id) "
            "VALUES (?, 'opportunity', 'obs V1', 0.75, 'v1', 'v1', 'notion-page-v1')",
            (self.source_id,))
        conn.commit()
        conn.close()
        self.calls = []
        self.patches = [
            mock.patch.object(mentor_worker, "DB", str(self.path)),
            mock.patch.object(mentor_worker, "ollama_available", return_value=True),
            mock.patch.object(mentor_worker, "qwen_analyze", return_value=dict(QWEN)),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def reassess(self, run2, **kwargs):
        def fake_analyze(text, **kw):
            self.calls.append(kw.get("variant", 1))
            return agency() if kw.get("variant", 1) == 1 else run2

        with mock.patch.object(mentor_worker.mentor_agency, "analyze", side_effect=fake_analyze):
            with redirect_stdout(io.StringIO()):
                return mentor_worker.reassess(**kwargs)

    def snapshot_v1(self):
        conn = sqlite3.connect(self.path)
        try:
            return (conn.execute("SELECT * FROM intelligence_mentor_processing").fetchall(),
                    conn.execute("SELECT * FROM intelligence_mentor_insights_local").fetchall())
        finally:
            conn.close()

    def test_reassess_traces_consensus_and_queues_review_without_touching_v1(self):
        before = self.snapshot_v1()
        result = self.reassess(agency(classification="low_signal", relevance=0.3, variant=2),
                               confirm=True)
        item = result["items"][0]
        self.assertTrue(item["borderline"])
        self.assertEqual(item["decision"], "review")
        self.assertEqual(self.calls, [1, 2])
        conn = sqlite3.connect(self.path)
        try:
            reason = conn.execute(
                "SELECT reason FROM intelligence_mentor_review_queue").fetchone()[0]
            self.assertTrue(reason.startswith("v2_backfill:agency_disagreement"))
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM intelligence_mentor_consensus").fetchone()[0], 1)
            trace = conn.execute(
                "SELECT decision, had_insight, v1_outcome FROM intelligence_mentor_v2_reassessment"
            ).fetchone()
            self.assertEqual(tuple(trace), ("review", 1, "opportunity"))
        finally:
            conn.close()
        # Traitement V1 et insight (deja dans Notion) intacts.
        self.assertEqual(self.snapshot_v1(), before)

    def test_reassess_is_idempotent_and_dry_run_writes_nothing(self):
        dry = self.reassess(agency(variant=2), confirm=False)
        self.assertEqual(dry["reassessed"], 1)
        conn = sqlite3.connect(self.path)
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='intelligence_mentor_v2_reassessment'"
            ).fetchone())
        finally:
            conn.close()
        self.reassess(agency(variant=2), confirm=True)
        again = self.reassess(agency(variant=2), confirm=True)
        self.assertEqual(again["reassessed"], 0)

    def test_reassess_refused_on_locked_real_vault(self):
        import v2_guard
        with mock.patch.object(v2_guard, "REAL_DB", self.path), \
                mock.patch.object(v2_guard, "FLAG", _isolation.TMP / "flag_reassess_absent"), \
                mock.patch.dict("os.environ", {"VAULT_V2_WRITES": ""}):
            with self.assertRaises(v2_guard.V2WritesBlocked):
                self.reassess(agency(variant=2), confirm=True)


class LockedRealVaultMentorTests(unittest.TestCase):
    """Verrou V2 : sur le vrai Vault non valide, le Mentor reste en V1."""

    def setUp(self):
        import v2_guard

        self.v2_guard = v2_guard
        self.path = _isolation.TMP / f"real_mentor_{self._testMethodName}.db"
        conn = make_db(str(self.path))
        self.source_id = add_source(conn, "https://example.test/borderline",
                                    "Outil de prospection LinkedIn", transcript=TEXT)
        mentor_worker.ensure_schema(conn)
        mentor_review.ensure_schema(conn, v2=False)  # schema V1 d'une vraie base
        conn.close()
        self.calls = []
        self.patches = [
            mock.patch.object(v2_guard, "REAL_DB", self.path),
            mock.patch.object(v2_guard, "FLAG", _isolation.TMP / "flag_mentor_absent"),
            mock.patch.dict("os.environ", {"VAULT_V2_WRITES": ""}),
            mock.patch.object(mentor_worker, "DB", str(self.path)),
            mock.patch.object(mentor_worker, "ollama_available", return_value=True),
            mock.patch.object(mentor_worker, "qwen_analyze", return_value=dict(QWEN)),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def schema(self):
        conn = sqlite3.connect(self.path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            columns = {r[1] for r in conn.execute(
                "PRAGMA table_info(intelligence_mentor_review_queue)")}
            return tables, columns
        finally:
            conn.close()

    def test_confirm_run_keeps_v1_behaviour_and_schema(self):
        before = self.schema()

        def fake_analyze(text, **kwargs):
            self.calls.append(kwargs.get("variant", 1))
            return agency()

        with mock.patch.object(mentor_worker.mentor_agency, "analyze", side_effect=fake_analyze):
            out = io.StringIO()
            with redirect_stdout(out):
                mentor_worker.run(limit=5, confirm=True)
        item = json.loads(out.getvalue())["items"][0]
        # Cas borderline (Qwen et Agency divergent) mais verrou ferme : V1.
        self.assertEqual(self.calls, [1])
        self.assertIsNone(item["consensus"])
        self.assertEqual(self.schema(), before)
        self.assertNotIn("intelligence_mentor_consensus", before[0])
        conn = sqlite3.connect(self.path)
        try:
            # Ecritures V1 normales du pipeline.
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM intelligence_mentor_processing").fetchone()[0], 1)
        finally:
            conn.close()

    def test_cloud_review_is_blocked_but_dry_run_reads(self):
        conn = sqlite3.connect(self.path)
        try:
            before = self.schema()
            result = mentor_review.process_queue(conn, provider=None, dry_run=True)
            self.assertEqual(result["status"], "no_cloud_provider")
            self.assertEqual(self.schema(), before)
            with self.assertRaises(self.v2_guard.V2WritesBlocked):
                mentor_review.process_queue(conn, provider=object(), dry_run=False)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
