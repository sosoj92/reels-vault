import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import unittest
from unittest import mock

import _life_fixtures as fx

import mentor_council as council
import mentor_proactive as proactive
import mentor_proactive_store as store
import mentor_project_profiles as profiles


def _shortlist(conn):
    proactive.run_pass(conn, now=fx.NOW)
    return {c["candidate_type"]: c for c in store.list_candidates(conn, "shortlisted")}


class SelectionTests(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()
        self.path, self.conn, self.ids, self.sources = fx.life_db()

    def tearDown(self):
        self.conn.close()

    def test_aliases_resolve_to_existing_agency_agents(self):
        self.assertEqual(council.resolve_agent("CFO").name, "Chief Financial Officer")
        self.assertEqual(council.resolve_agent("Recruitment Agent").name, "Recruitment Specialist")
        self.assertEqual(council.resolve_agent("growth hacker").name, "Growth Hacker")
        self.assertIsNone(council.resolve_agent("Agent imaginaire"))
        names = [a.name for a in council.agents_in_text("demande au CFO et au Growth Hacker")]
        self.assertEqual(names, ["Chief Financial Officer", "Growth Hacker"])

    def test_mode_c_selection_is_deterministic_and_capped(self):
        candidate = _shortlist(self.conn)["opportunity"]
        first = council.select_for_candidate(self.conn, candidate)
        second = council.select_for_candidate(self.conn, candidate)
        self.assertEqual([a["name"] for a in first], [a["name"] for a in second])
        self.assertLessEqual(len(first), council.MAX_AGENTS)
        self.assertEqual(first[0]["name"], "Recruitment Specialist")
        self.assertIn("conseiller du projet Nexdoor Talent", first[0]["reason"])

    def test_mode_a_and_b_selection_with_reasons(self):
        mode, agents = council.select_for_question(self.conn, "Demande au CFO ce qu'il pense")
        self.assertEqual((mode, [a["name"] for a in agents]), ("A", ["Chief Financial Officer"]))
        mode, agents = council.select_for_question(
            self.conn, "Comment exploiter ca pour Nexdoor Talent ?")
        self.assertEqual(mode, "B")
        self.assertTrue(1 <= len(agents) <= 3)
        self.assertTrue(all("Nexdoor Talent" in a["reason"] for a in agents))
        mode, agents = council.select_for_question(
            self.conn, "Comment exploiter ca pour Nexdoor Talent ?", exclude=["Growth Hacker"])
        self.assertNotIn("Growth Hacker", [a["name"] for a in agents])


class ReviewTests(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()
        self.path, self.conn, self.ids, self.sources = fx.life_db()
        self.candidate = _shortlist(self.conn)["opportunity"]

    def tearDown(self):
        self.conn.close()

    def test_supported_candidate_becomes_suggestion_with_journaled_consultations(self):
        backend = fx.FakeBackend([fx.verdict(4), fx.verdict(3, "neutral"), fx.verdict(2)])
        result = council.review_candidate(self.conn, self.candidate, backend, now=fx.NOW)
        self.assertEqual(result["result"], "suggestion")
        suggestion = store.get_suggestion(self.conn, result["suggestion_id"])
        self.assertTrue(suggestion["suggestion_id"].startswith("SUG-"))
        self.assertEqual(suggestion["status"], "ready")
        self.assertEqual(len(suggestion["evidence"]["agents"]), 3)
        consultations = store.list_consultations(self.conn,
                                                 suggestion_id=suggestion["suggestion_id"])
        self.assertEqual(len(consultations), 3)
        self.assertEqual({c["mode"] for c in consultations}, {"C"})
        self.assertEqual(consultations[0]["value_score"], 4)
        self.assertEqual(store.get_candidate(self.conn, self.candidate["candidate_id"])["status"],
                         "promoted")
        self.assertIsNone(suggestion["evidence"]["disagreement"])  # 4 vs 2 : pas serieux

    def test_blocking_objection_rejects_even_with_a_high_score(self):
        backend = fx.FakeBackend([fx.verdict(5), fx.verdict(1, "oppose", True, "Deja connu de Rosy")])
        result = council.review_candidate(self.conn, self.candidate, backend, now=fx.NOW)
        self.assertEqual(result["result"], "rejected")
        self.assertTrue(result["reason"].startswith("blocking_objection:"))
        candidate = store.get_candidate(self.conn, self.candidate["candidate_id"])
        self.assertEqual(candidate["status"], "rejected")
        self.assertEqual(store.list_suggestions(self.conn), [])

    def test_incoherent_blocking_flag_is_not_blocking(self):
        backend = fx.FakeBackend([fx.verdict(4, "support", True, "")])
        result = council.review_candidate(self.conn, self.candidate, backend, now=fx.NOW)
        self.assertEqual(result["result"], "suggestion")
        agent = result["agents"][0]
        self.assertFalse(agent["blocking_objection"])
        self.assertTrue(agent["incoherent_blocking_flag"])

    def test_agents_not_convinced(self):
        backend = fx.FakeBackend([fx.verdict(3), fx.verdict(3), fx.verdict(2)])
        result = council.review_candidate(self.conn, self.candidate, backend, now=fx.NOW)
        self.assertEqual((result["result"], result["reason"]),
                         ("rejected", "agents_not_convinced:max=3"))

    def test_serious_disagreement_is_shown(self):
        backend = fx.FakeBackend([fx.verdict(5, "support"), fx.verdict(1, "oppose", False, "Trop tot")])
        result = council.review_candidate(self.conn, self.candidate, backend, now=fx.NOW)
        self.assertEqual(result["result"], "suggestion")
        self.assertIn("Desaccord", store.get_suggestion(self.conn, result["suggestion_id"])
                      ["explanation"])

    def test_invalid_evidence_refs_are_dropped(self):
        backend = fx.FakeBackend([fx.verdict(4, refs=("source:1", "source:999", "http://x"))])
        result = council.review_candidate(self.conn, self.candidate, backend, now=fx.NOW)
        agent = result["agents"][0]
        self.assertEqual(agent["evidence_refs"], ["source:1"])
        self.assertEqual(agent["invalid_refs"], 2)

    def test_local_llm_unavailable_keeps_candidate_waiting(self):
        result = council.review_candidate(self.conn, self.candidate,
                                          fx.FakeBackend(available=False), now=fx.NOW)
        self.assertEqual((result["result"], result["reason"]), ("waiting", "local_llm_unavailable"))
        self.assertEqual(store.get_candidate(self.conn, self.candidate["candidate_id"])["status"],
                         "shortlisted")

    def test_invalid_llm_output_retries_then_expires(self):
        for attempt in range(council.MAX_REVIEW_ATTEMPTS):
            candidate = store.get_candidate(self.conn, self.candidate["candidate_id"])
            result = council.review_candidate(self.conn, candidate,
                                              fx.FakeBackend(hypothesis=False), now=fx.NOW)
        self.assertEqual(result["result"], "expired")

    def test_inactive_project_and_fatigue_block_suggestions(self):
        profile = profiles.get_profile(self.conn, self.ids["activity:nexdoor_talent"])
        profile["active"] = False
        profiles.upsert_profile(self.conn, self.ids["activity:nexdoor_talent"], profile)
        result = council.review_candidate(self.conn, self.candidate, fx.FakeBackend(), now=fx.NOW)
        self.assertTrue(result["reason"].startswith("project_inactive"))

    def test_fatigue_from_negative_feedback(self):
        backend = fx.FakeBackend()
        first = council.review_candidate(self.conn, self.candidate, backend, now=fx.NOW)
        store.record_feedback(self.conn, first["suggestion_id"], "off_topic")
        store.record_feedback(self.conn, first["suggestion_id"], "bad_idea")
        self.assertEqual(council.fatigue(self.conn, self.ids["activity:nexdoor_talent"],
                                         "opportunity", fx.NOW), 2.0)

    def test_review_never_calls_cloud(self):
        import vault_cloud

        with mock.patch.object(vault_cloud, "get_provider", side_effect=AssertionError("cloud")):
            council.review_shortlist(self.conn, fx.FakeBackend(), now=fx.NOW, limit=2)


if __name__ == "__main__":
    unittest.main()
