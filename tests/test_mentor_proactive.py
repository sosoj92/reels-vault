import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import unittest
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import mentor_proactive as mp
import mentor_proactive_store as store
import vault_intelligence as intelligence

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def _path():
    return _isolation.TMP / f"proactive-{uuid.uuid4().hex[:8]}.db"


def _day(days_ago):
    return (NOW - timedelta(days=days_ago)).date().isoformat()


def _demo():
    path = _path()
    return path, mp.build_demo_db(str(path), now=NOW)


def _add_source(conn, slug, *, author, days_ago, entity_id, title=None):
    source_id = intelligence.upsert_source(
        conn, source_url=f"https://example.invalid/t/{slug}", title=title or slug,
        author=author, captured_at=_day(days_ago),
    )
    intelligence.link_entity_source(conn, entity_id=entity_id, source_id=source_id)
    return source_id


def _entity(conn, key, *, entity_type="project", attention=None):
    return intelligence.upsert_entity(
        conn, entity_type=entity_type, canonical_key=key, name=key,
        attention_status=attention,
    )


def _candidates(conn, candidate_type=None):
    return [
        c for c in store.list_candidates(conn)
        if candidate_type is None or c["candidate_type"] == candidate_type
    ]


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class SchemaTests(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()

    def test_migration_is_idempotent_and_keeps_existing_data(self):
        path, conn = _demo()
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("intelligence_sources", "intelligence_entities",
                          "intelligence_mentor_insights_local", "contents", "actions")
        }
        schema_before = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE '%mentor_%' ORDER BY name"
        ).fetchall()
        store.ensure_schema(conn)
        store.ensure_schema(conn)
        for table in store.TABLES:
            self.assertTrue(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone(), table)
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
        self.assertEqual(before, after)
        # Aucune table ni colonne existante n'est modifiee (hors meta de version).
        schema_after = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE '%mentor_%' ORDER BY name"
        ).fetchall()
        self.assertEqual([tuple(r) for r in schema_before], [tuple(r) for r in schema_after])
        self.assertEqual(conn.execute(
            "SELECT value FROM meta WHERE key='mentor_proactive_schema_version'"
        ).fetchone()[0], "1")
        conn.close()

    def test_real_vault_is_locked_until_validation(self):
        conn = sqlite3.connect(":memory:")
        with mock.patch.object(store.v2_guard, "db_path", return_value=str(store.v2_guard.REAL_DB)), \
                mock.patch.object(store, "FLAG", _isolation.TMP / "absent.enabled"), \
                mock.patch.dict(os.environ, {"VAULT_PROACTIVE_WRITES": ""}):
            self.assertFalse(store.writes_allowed(conn))
            with self.assertRaises(store.ProactiveWritesBlocked):
                store.ensure_schema(conn)
            import vault_refresh

            self.assertEqual(
                vault_refresh._publish_proactive_events(conn),
                {"status": "skipped", "reason": "proactive_not_validated"},
            )
        self.assertFalse(store.schema_present(conn))


class EventQueueTests(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()
        self.path, self.conn = _demo()
        store.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_event_creation_has_stable_id_and_no_duplicate(self):
        first = store.publish_event(
            self.conn, "task_created", entity_type="task", entity_id=7,
            project_id=1, payload={"title": "x"},
        )
        second = store.publish_event(
            self.conn, "task_created", entity_type="task", entity_id=7,
            payload={"title": "autre"},
        )
        self.assertTrue(first[0].startswith("EVT-"))
        self.assertEqual(first[0], second[0])
        self.assertEqual((first[1], second[1]), (True, False))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM mentor_events WHERE entity_id = 7"
        ).fetchone()[0], 1)
        event = store.get_event(self.conn, first[0])
        for field in ("event_id", "event_type", "entity_type", "entity_id", "project_id",
                      "payload", "created_at", "processed_at", "status"):
            self.assertIn(field, event)
        self.assertEqual(event["status"], "pending")
        self.assertEqual(json.loads(event["payload"]), {"title": "x"})

    def test_question_is_deduplicated_per_day(self):
        a = mp.publish_question(self.conn, "Que sais-je sur l'IA locale ?", [1], now=NOW)
        b = mp.publish_question(self.conn, "  que sais-je sur l'IA LOCALE ?", [1], now=NOW)
        c = mp.publish_question(self.conn, "Que sais-je sur l'IA locale ?", [1],
                                now=NOW + timedelta(days=1))
        self.assertEqual(a[0], b[0])
        self.assertFalse(b[1])
        self.assertNotEqual(a[0], c[0])

    def test_invalid_events_are_refused(self):
        with self.assertRaises(store.ProactiveStoreError):
            store.publish_event(self.conn, "telegram_sent", entity_id=1)
        with self.assertRaises(store.ProactiveStoreError):
            store.publish_event(self.conn, "source_ingested")

    def test_vault_scan_is_idempotent(self):
        first = mp.publish_vault_events(self.conn, NOW)
        count = self.conn.execute("SELECT COUNT(*) FROM mentor_events").fetchone()[0]
        second = mp.publish_vault_events(self.conn, NOW)
        self.assertEqual(first["source_ingested:new"], 7)
        self.assertEqual(first["insight_created:new"], 2)
        self.assertEqual(second["source_ingested:known"], 7)
        self.assertNotIn("source_ingested:new", second)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM mentor_events").fetchone()[0], count
        )

    def test_project_changed_only_on_real_change(self):
        first = mp.publish_vault_events(self.conn, NOW)
        self.assertEqual(first["project_baseline"], 1)
        self.assertNotIn("project_changed:new", first)
        self.conn.execute(
            "UPDATE intelligence_entities SET attention_status = 'now' WHERE entity_type='project'"
        )
        self.conn.commit()
        second = mp.publish_vault_events(self.conn, NOW)
        self.assertEqual(second["project_changed:new"], 1)
        third = mp.publish_vault_events(self.conn, NOW)
        self.assertNotIn("project_changed:new", third)
        row = self.conn.execute(
            "SELECT payload FROM mentor_events WHERE event_type='project_changed'"
        ).fetchone()
        payload = json.loads(row[0])
        self.assertEqual(payload["before"]["attention_status"], "later")
        self.assertEqual(payload["after"]["attention_status"], "now")

    def test_task_events(self):
        stamp = store.now_iso(NOW)
        for title, status, due in (("finie", "done", None), ("en retard", "todo", _day(3)),
                                   ("a venir", "todo", _day(-3))):
            self.conn.execute(
                "INSERT INTO actions (title, status, due_date, priority, created_at, updated_at) "
                "VALUES (?, ?, ?, 2, ?, ?)",
                (title, status, due, stamp, stamp),
            )
        self.conn.commit()
        counts = mp.publish_vault_events(self.conn, NOW)
        self.assertEqual(counts["task_created:new"], 3)
        self.assertEqual(counts["task_completed:new"], 1)
        self.assertEqual(counts["task_overdue:new"], 1)


class RestartTests(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()

    def test_events_survive_restart(self):
        path, conn = _demo()
        mp.publish_vault_events(conn, NOW)
        pending = len(store.pending_events(conn))
        conn.close()  # arret du PC

        conn = mp.open_database(path)
        self.assertEqual(len(store.pending_events(conn)), pending)
        report = mp.run_pass(conn, now=NOW, scan=False)
        self.assertEqual(report["events"]["processed"], pending)
        conn.close()

        conn = mp.open_database(path)
        self.assertEqual(store.pending_events(conn), [])
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM mentor_events WHERE status='processed' AND processed_at IS NOT NULL"
        ).fetchone()[0], pending)
        conn.close()

    def test_interrupted_pass_resumes_where_it_stopped(self):
        path, conn = _demo()
        mp.publish_vault_events(conn, NOW)
        total = len(store.pending_events(conn))
        real_detect = mp.detect
        calls = {"n": 0}

        def crash_on_third(view, event):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt("coupure")
            return real_detect(view, event)

        with mock.patch.object(mp, "detect", side_effect=crash_on_third):
            with self.assertRaises(KeyboardInterrupt):
                mp.run_pass(conn, now=NOW, scan=False)
        conn.close()

        conn = mp.open_database(path)
        self.assertEqual(len(store.pending_events(conn)), total - 2)
        report = mp.run_pass(conn, now=NOW, scan=False)
        self.assertEqual(report["events"]["processed"], total - 2)
        self.assertEqual(store.pending_events(conn), [])
        self.assertEqual(len(_candidates(conn, "convergence")), 2)
        conn.close()

    def test_failing_event_is_retried_then_abandoned(self):
        path, conn = _demo()
        store.publish_event(conn, "daily_pass", payload={"day": "2026-09-29"})
        with mock.patch.object(mp, "detect", side_effect=ValueError("bug")):
            for attempt in range(1, store.MAX_EVENT_ATTEMPTS + 1):
                report = mp.run_pass(conn, now=NOW, scan=False)
                self.assertEqual(len(report["events"]["failed"]), 1)
        row = conn.execute("SELECT status, attempts, last_error FROM mentor_events").fetchone()
        self.assertEqual((row[0], row[1]), ("failed", store.MAX_EVENT_ATTEMPTS))
        self.assertIn("bug", row[2])
        self.assertEqual(store.pending_events(conn), [])
        conn.close()


class DetectionTests(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()

    def test_demo_four_sources_become_one_convergence(self):
        _, conn = _demo()
        report = mp.run_pass(conn, now=NOW)
        convergences = [c for c in _candidates(conn, "convergence") if c["project_id"]]
        self.assertEqual(len(convergences), 1)
        candidate = convergences[0]
        self.assertEqual(candidate["source_ids"], [1, 2, 3, 4])
        self.assertEqual(candidate["project_id"], 1)
        self.assertEqual(candidate["status"], "shortlisted")
        self.assertEqual(candidate["score"], 0.775)
        self.assertEqual(candidate["insight_ids"], [1, 2])
        self.assertEqual(candidate["seen_count"], 5)  # 4 sources + passe quotidienne
        self.assertEqual(len(candidate["event_ids"]), 5)
        self.assertEqual(candidate["evidence"]["tier"], "t1")
        self.assertIn(candidate["candidate_id"], report["shortlisted_now"])
        conn.close()

    def test_score_is_deterministic_and_explained(self):
        factors = {"volume": 0.5, "independence": 1, "recency": 1, "anchor": 1,
                   "insight_support": 0.5}
        first = mp.score_candidate("convergence", factors)
        self.assertEqual(first, mp.score_candidate("convergence", dict(factors)))
        self.assertEqual(first[0], 0.775)
        self.assertEqual(first[1]["volume"], {"value": 0.5, "weight": 0.3, "contribution": 0.15})
        for weights in mp.WEIGHTS.values():
            self.assertAlmostEqual(sum(weights.values()), 1.0)
        with self.assertRaises(ValueError):
            mp.score_candidate("convergence", {"volume": 1})
        # Deux passes independantes sur les memes donnees -> memes candidats.
        results = []
        for _ in range(2):
            _, conn = _demo()
            mp.run_pass(conn, now=NOW)
            results.append([(c["candidate_id"], c["score"], c["status"]) for c in _candidates(conn)])
            conn.close()
        self.assertEqual(results[0], results[1])

    def test_fingerprint(self):
        a = store.fingerprint_for("convergence", "entity:1", "tier", "t1")
        self.assertEqual(a, store.fingerprint_for("convergence", "entity:1", "tier", "t1"))
        self.assertNotEqual(a, store.fingerprint_for("convergence", "entity:2", "tier", "t1"))
        self.assertNotEqual(a, store.fingerprint_for("convergence", "entity:1", "tier", "t2"))
        self.assertNotEqual(a, store.fingerprint_for("opportunity", "entity:1", "tier", "t1"))
        self.assertEqual(store.candidate_id_for(a), "CAND-" + a[:12])
        _, conn = _demo()
        mp.run_pass(conn, now=NOW)
        count = conn.execute("SELECT COUNT(*) FROM mentor_candidates").fetchone()[0]
        self.assertEqual(count, conn.execute(
            "SELECT COUNT(DISTINCT fingerprint) FROM mentor_candidates").fetchone()[0])
        conn.close()

    def test_rerun_creates_no_duplicate(self):
        _, conn = _demo()
        mp.run_pass(conn, now=NOW)
        before = [(c["candidate_id"], c["seen_count"]) for c in _candidates(conn)]
        report = mp.run_pass(conn, now=NOW)
        self.assertEqual(report["events"]["read"], 0)
        self.assertEqual([(c["candidate_id"], c["seen_count"]) for c in _candidates(conn)], before)
        # Le lendemain : nouvelle passe quotidienne, meme preuve -> dedoublonne.
        report = mp.run_pass(conn, now=NOW + timedelta(days=1))
        self.assertEqual(report["events"]["by_type"], {"daily_pass": 1})
        self.assertNotIn("created", report["candidates"])
        self.assertEqual(len(_candidates(conn)), len(before))
        conn.close()

    def test_growing_convergence_supersedes_weaker_one(self):
        _, conn = _demo()
        mp.run_pass(conn, now=NOW)
        _add_source(conn, "cinquieme", author="emma.ops", days_ago=1, entity_id=1)
        report = mp.run_pass(conn, now=NOW)
        convergences = {c["evidence"]["tier"]: c for c in _candidates(conn, "convergence")
                        if c["project_id"] == 1}
        self.assertEqual(set(convergences), {"t1", "t2"})
        self.assertEqual(convergences["t1"]["status"], "expired")
        self.assertEqual(convergences["t1"]["rejection_reason"],
                         f"superseded_by:{convergences['t2']['candidate_id']}")
        self.assertEqual(len(convergences["t2"]["source_ids"]), 5)
        self.assertIn("created", report["candidates"])
        conn.close()

    def test_rejected_candidate_is_journaled_and_never_projectable(self):
        _, conn = _demo()
        report = mp.run_pass(conn, now=NOW)
        rejected = [c for c in _candidates(conn) if c["status"] == "rejected"]
        self.assertEqual(len(rejected), 1)
        candidate = rejected[0]
        self.assertEqual(candidate["rejection_reason"], "same_author_only")
        self.assertIsNotNone(candidate["closed_at"])
        self.assertEqual(candidate["source_ids"], [5, 6, 7])
        self.assertIn("score_breakdown", candidate)
        self.assertEqual([c["candidate_id"] for c in report["rejected"]], [candidate["candidate_id"]])
        with self.assertRaises(store.ProactiveStoreError):
            store.promote_candidate(conn, candidate["candidate_id"], title="t", summary="s")
        self.assertEqual(store.projectable_suggestions(conn), [])
        # Revu plus tard : jamais rouvert.
        mp.run_pass(conn, now=NOW + timedelta(days=1))
        again = store.get_candidate(conn, candidate["candidate_id"])
        self.assertEqual(again["status"], "rejected")
        self.assertGreater(again["seen_count"], candidate["seen_count"])
        conn.close()

    def test_sensitive_convergence_is_rejected(self):
        _, conn = _demo()
        project = _entity(conn, "projet-sante")
        for index, author in enumerate(("a", "b", "c")):
            _add_source(conn, f"sante-{index}", author=author, days_ago=2, entity_id=project,
                        title=f"Nouveau traitement medical pour les patients {index}")
        mp.run_pass(conn, now=NOW)
        candidate = [c for c in _candidates(conn, "convergence") if c["project_id"] == project][0]
        self.assertEqual(candidate["status"], "rejected")
        self.assertEqual(candidate["rejection_reason"], "sensitive_topic")
        conn.close()

    def test_unanchored_candidate_is_only_watched(self):
        _, conn = _demo()
        topic = _entity(conn, "outil-x", entity_type="solution")
        for index, author in enumerate(("a", "b", "c", "d", "e")):
            _add_source(conn, f"outil-{index}", author=author, days_ago=1, entity_id=topic)
        mp.run_pass(conn, now=NOW)
        candidate = [c for c in _candidates(conn, "convergence")
                     if c["focus_entity_id"] == topic][0]
        self.assertIsNone(candidate["project_id"])
        self.assertEqual(candidate["status"], "watching")
        self.assertEqual(candidate["evidence"]["watch_reason"], "no_project_anchor")
        conn.close()

    def test_low_score_is_rejected_below_floor(self):
        _, conn = _demo()
        topic = _entity(conn, "vieux-sujet", entity_type="solution")
        for index, author in enumerate(("a", "b", "c")):
            _add_source(conn, f"vieux-{index}", author=author, days_ago=100, entity_id=topic)
        conn.execute("UPDATE intelligence_sources SET author = 'a' WHERE title = 'vieux-2'")
        conn.commit()
        mp.run_pass(conn, now=NOW)
        candidate = [c for c in _candidates(conn, "convergence")
                     if c["focus_entity_id"] == topic][0]
        self.assertEqual(candidate["status"], "rejected")
        self.assertTrue(candidate["rejection_reason"].startswith("below_floor:"))
        conn.close()

    def test_reactivation_after_dormancy(self):
        _, conn = _demo()
        project = _entity(conn, "projet-dormant")
        _add_source(conn, "ancienne", author="old", days_ago=400, entity_id=project)
        mp.run_pass(conn, now=NOW)
        _add_source(conn, "nouvelle", author="new", days_ago=1, entity_id=project)
        mp.run_pass(conn, now=NOW)
        candidate = _candidates(conn, "reactivation")[0]
        self.assertEqual(candidate["project_id"], project)
        self.assertEqual(candidate["evidence"]["gap_days"], 399)
        self.assertEqual(candidate["status"], "shortlisted")
        conn.close()

    def test_question_on_dormant_source_suggests_reactivation(self):
        _, conn = _demo()
        project = _entity(conn, "projet-oublie")
        source = _add_source(conn, "archive", author="x", days_ago=300, entity_id=project)
        mp.run_pass(conn, now=NOW)
        mp.publish_question(conn, "Ou en est le projet oublie ?", [source], now=NOW)
        mp.run_pass(conn, now=NOW, scan=False)
        candidate = _candidates(conn, "reactivation")[0]
        self.assertEqual(candidate["focus_entity_id"], project)
        self.assertEqual(candidate["score_breakdown"]["new_signal"]["value"], 1.0)
        conn.close()

    def test_overdue_task_becomes_forgotten_candidate(self):
        _, conn = _demo()
        stamp = store.now_iso(NOW)
        conn.execute(
            "INSERT INTO actions (title, status, due_date, priority, created_at, updated_at) "
            "VALUES ('Relancer le prospect', 'todo', ?, 1, ?, ?)",
            (_day(10), stamp, stamp),
        )
        conn.commit()
        mp.run_pass(conn, now=NOW)
        forgotten = _candidates(conn, "forgotten")
        self.assertEqual(len(forgotten), 1)
        self.assertEqual(forgotten[0]["task_ids"], [1])
        self.assertEqual(forgotten[0]["evidence"]["overdue_days"], 10)
        # Tache sans lien projet : conservee en observation, pas presentee.
        self.assertEqual(forgotten[0]["status"], "watching")
        conn.close()

    def test_completed_task_without_follow_up_suggests_next_action(self):
        _, conn = _demo()
        conn.execute(
            "INSERT INTO contents (source_url, created_at, updated_at) VALUES (?, ?, ?)",
            ("https://example.invalid/demo/agent-vocal-pme", store.now_iso(NOW), store.now_iso(NOW)),
        )
        content_id = conn.execute("SELECT MAX(id) FROM contents").fetchone()[0]
        conn.execute("UPDATE intelligence_sources SET content_id = ? WHERE id = 1", (content_id,))
        conn.execute(
            "INSERT INTO actions (content_id, title, status, created_at, updated_at) "
            "VALUES (?, 'Maquette agent vocal', 'done', ?, ?)",
            (content_id, store.now_iso(NOW - timedelta(days=1)), store.now_iso(NOW - timedelta(days=1))),
        )
        conn.commit()
        mp.run_pass(conn, now=NOW)
        candidate = _candidates(conn, "next_action")[0]
        self.assertEqual(candidate["project_id"], 1)
        self.assertEqual(candidate["task_ids"], [1])
        conn.close()

    def test_claim_tension_becomes_tension_candidate(self):
        import vault_claim_reasoning

        _, conn = _demo()
        vault_claim_reasoning.ensure_schema(conn)
        stamp = store.now_iso(NOW)
        conn.execute(
            "INSERT INTO intelligence_claim_assessments (kind, fingerprint, claim_ids, "
            "source_ids, summary, confidence, status, created_at, updated_at) "
            "VALUES ('contradiction', 'fp-test', '[11, 12]', '[1, 2]', "
            "'Prix incompatibles', 0.8, 'needs_review', ?, ?)",
            (stamp, stamp),
        )
        conn.commit()
        mp.run_pass(conn, now=NOW)
        candidate = _candidates(conn, "tension")[0]
        self.assertEqual(candidate["claim_ids"], [11, 12])
        self.assertEqual(candidate["project_id"], 1)
        self.assertEqual(candidate["score"], 0.92)
        conn.close()


class SuggestionAndConsultationTests(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()
        _, self.conn = _demo()
        mp.run_pass(self.conn, now=NOW)
        self.shortlist = store.list_candidates(self.conn, "shortlisted")

    def tearDown(self):
        self.conn.close()

    def test_suggestion_lifecycle(self):
        first = store.promote_candidate(
            self.conn, self.shortlist[0]["candidate_id"], title="Titre", summary="Resume",
            explanation="Pourquoi", at=store.now_iso(NOW),
        )
        second = store.promote_candidate(
            self.conn, self.shortlist[1]["candidate_id"], title="T2", summary="R2",
        )
        self.assertEqual((first["suggestion_id"], second["suggestion_id"]), ("SUG-0001", "SUG-0002"))
        self.assertEqual(first["status"], "ready")
        self.assertEqual(first["fingerprint"], self.shortlist[0]["fingerprint"])
        self.assertEqual(store.get_candidate(self.conn, first["candidate_id"])["status"], "promoted")
        with self.assertRaises(store.ProactiveStoreError):  # deja promu
            store.promote_candidate(self.conn, first["candidate_id"], title="x", summary="y")

        sid = first["suggestion_id"]
        store.transition_suggestion(self.conn, sid, "sent")
        store.transition_suggestion(self.conn, sid, "opened")
        with self.assertRaises(store.ProactiveStoreError):
            store.transition_suggestion(self.conn, sid, "snoozed")  # date obligatoire
        until = store.now_iso(NOW + timedelta(days=2))
        snoozed = store.transition_suggestion(self.conn, sid, "snoozed", decided_by="rosy",
                                              snoozed_until=until)
        self.assertEqual((snoozed["decision"], snoozed["snoozed_until"]), ("snoozed", until))
        self.assertEqual(store.wake_snoozed(self.conn, at=store.now_iso(NOW)), [])
        self.assertEqual(store.wake_snoozed(self.conn, at=store.now_iso(NOW + timedelta(days=3))), [sid])
        accepted = store.transition_suggestion(self.conn, sid, "accepted", decided_by="rosy",
                                               note="ok")
        self.assertEqual((accepted["decision"], accepted["decided_by"]), ("accepted", "rosy"))
        self.assertEqual([h["status"] for h in accepted["status_history"]],
                         ["ready", "sent", "opened", "snoozed", "ready", "accepted"])
        with self.assertRaises(store.ProactiveStoreError):
            store.transition_suggestion(self.conn, sid, "executed")  # doit passer par approved
        dismissed = store.transition_suggestion(self.conn, second["suggestion_id"], "dismissed")
        self.assertEqual(dismissed["decision"], "dismissed")
        projectable = [s["suggestion_id"] for s in store.projectable_suggestions(self.conn)]
        self.assertEqual(projectable, [sid])

    def test_consultation_journal(self):
        suggestion = store.promote_candidate(
            self.conn, self.shortlist[0]["candidate_id"], title="T", summary="S",
        )
        consultation_id = store.record_consultation(
            self.conn, agent_id="engineering-ai-engineer", agent_name="AI Engineer", mode="A",
            session_id="S1", suggestion_id=suggestion["suggestion_id"],
            inputs={"candidate": suggestion["candidate_id"]}, verdict="support",
            value_score=4, proposed_action="Prototype", main_objection="Cout CPU",
            confidence=0.7, evidence_refs=["source:1", "source:2"], model="qwen3:1.7b",
        )
        self.assertTrue(consultation_id.startswith("CONS-"))
        rows = store.list_consultations(self.conn, suggestion_id=suggestion["suggestion_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence_refs"], ["source:1", "source:2"])
        self.assertEqual(rows[0]["value_score"], 4)
        for bad in ({"mode": "D"}, {"value_score": 6}, {"confidence": 1.5}):
            kwargs = dict(agent_id="a", agent_name="A", mode="A", inputs={})
            kwargs.update(bad)
            with self.assertRaises(store.ProactiveStoreError):
                store.record_consultation(self.conn, **kwargs)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()

    def test_pass_makes_no_cloud_llm_or_notion_call(self):
        import vault_cloud
        from vault_notion_cockpit import NotionCockpitClient

        _, conn = _demo()
        guards = [
            mock.patch.object(NotionCockpitClient, name, side_effect=AssertionError(name))
            for name in ("_request", "create_page", "update_page", "get_page")
        ] + [
            mock.patch.object(vault_cloud, "get_provider", side_effect=AssertionError("cloud")),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("reseau")),
        ]
        mocks = [guard.start() for guard in guards]
        try:
            report = mp.run_pass(conn, now=NOW)
        finally:
            for guard in guards:
                guard.stop()
        self.assertEqual(report["events"]["failed"], [])
        for mocked in mocks:
            mocked.assert_not_called()
        self.assertEqual(report["review_stages"], [])
        conn.close()

    def test_module_imports_no_cloud_llm_or_notion_code(self):
        path, conn = _demo()
        conn.close()
        code = (
            "import sys, mentor_proactive as mp\n"
            f"conn = mp.open_database(r'{path}')\n"
            "mp.run_pass(conn)\n"
            "bad = {'vault_cloud', 'anthropic', 'mentor_agency', 'vault_ask', "
            "'vault_notion_cockpit', 'vault_notion_tasks', 'mentor_notion_sync'}\n"
            "print(sorted(bad & set(sys.modules)))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=str(_isolation.RACINE),
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "REELS_VAULT_DB": str(path)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")

    def test_cockpit_projection_never_contains_candidates(self):
        import vault_notion_cockpit

        _, conn = _demo()
        mp.run_pass(conn, now=NOW)
        projection = json.dumps(vault_notion_cockpit.build_projection(conn), default=str)
        for candidate in _candidates(conn):
            self.assertNotIn(candidate["candidate_id"], projection)
        conn.close()

    def test_dry_run_cli_leaves_database_untouched(self):
        path, conn = _demo()
        conn.close()
        before = _sha(path)
        out = io.StringIO()
        with redirect_stdout(out):
            code = mp.main(["--db", str(path), "--dry-run", "--now", NOW.isoformat()])
        self.assertEqual(code, 0)
        self.assertEqual(_sha(path), before)
        text = out.getvalue()
        self.assertIn("Dry-run : copie en memoire", text)
        self.assertIn("[0.775] Convergence", text)
        check = sqlite3.connect(path)
        self.assertFalse(store.schema_present(check))
        check.close()

    def test_confirm_cli_persists_on_unlocked_database(self):
        path, conn = _demo()
        conn.close()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mp.main(["--db", str(path), "--confirm", "--now", NOW.isoformat()]), 0)
        check = mp.open_database(path)
        self.assertEqual(len(store.list_candidates(check, "shortlisted")), 3)
        check.close()

    def test_demo_cli(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(mp.main(["--demo", "--now", NOW.isoformat()]), 0)
        text = out.getvalue()
        self.assertIn("Demo : donnees FICTIVES", text)
        self.assertIn("Convergence : 4 sources sur 'Projet Demo - Agence IA locale'", text)
        self.assertIn("rejet : same_author_only", text)

    def test_refresh_step_publishes_events(self):
        import vault_refresh

        _, conn = _demo()
        result = vault_refresh._publish_proactive_events(conn)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["source_ingested:new"], 7)
        # Publication seulement : aucune detection dans le refresh.
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM mentor_candidates").fetchone()[0], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
