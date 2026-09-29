import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import json
import unittest

import vault_claims as claims
import vault_intelligence_actions as actions
import vault_intelligence_signals as signals
import vault_notion_cockpit as cockpit
from _v2_fixtures import add_source, claim, make_db


class SensitiveClaimReviewOnlyTests(unittest.TestCase):
    """
    Un claim sensible reste en needs_review dans vault.db et dans la file de
    review, mais ne cree plus d'action ni de carte individuelle du cockpit.
    """

    def setUp(self):
        self.conn = make_db()
        self.health = add_source(
            self.conn, "https://example.test/tdah", "TSG et TDAH",
            category="Santé & bien-être",
            transcript="Une étude sur des rats montre que le TSG réduit l'hyperactivité.")
        self.plain = add_source(
            self.conn, "https://example.test/rtk", "RTK",
            transcript="RTK réduit la consommation de tokens de 90 %.")
        for source_id, item in (
            (self.health, claim("Le TSG réduit l'hyperactivité", "TSG", "réduit",
                                "l'hyperactivité", "le TSG réduit l'hyperactivité")),
            (self.plain, claim("RTK réduit la consommation de tokens", "RTK", "réduit",
                               "la consommation de tokens",
                               "RTK réduit la consommation de tokens de 90 %")),
        ):
            claims.extract_source(self.conn, source_id,
                                  claims.StaticClaimExtractor({source_id: [item]}))
        # Insight V1 deja projete dans Notion : ne doit jamais bouger.
        self.conn.execute("""
            CREATE TABLE intelligence_mentor_insights_local (
                id INTEGER PRIMARY KEY, source_id INTEGER, observation TEXT,
                notion_page_id TEXT)""")
        self.conn.execute(
            "INSERT INTO intelligence_mentor_insights_local VALUES (1, ?, 'obs', 'page-v1')",
            (self.plain,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def sensitive_signals(self):
        return [s for s in signals.collect_signals(self.conn, limit=500)["items"]
                if s["kind"] == "sensitive_claim_review"]

    def test_1_sensitive_claim_kept_in_needs_review(self):
        row = claims.claims_for_source(self.conn, self.health)[0]
        self.assertEqual(row["validation_status"], "needs_review")
        self.assertEqual(row["sensitivity"], "health")
        self.assertEqual(row["verification_status"], "unverified")

    def test_2_visible_in_internal_review_queue(self):
        found = self.sensitive_signals()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["source_id"], self.health)
        self.assertEqual(found[0]["priority"], 1)

    def test_3_refresh_creates_no_individual_action_or_card(self):
        result = actions.propose_priority_actions(self.conn, max_priority=2)
        self.assertEqual(result["review_only"], 1)
        self.assertEqual(result["created"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 0)
        rows = cockpit.build_projection(self.conn)
        individual = [r for r in rows if r["vault_ref"].startswith("signal:sensitive_claim_review")]
        self.assertEqual(individual, [])
        digest = [r for r in rows if r["vault_ref"] == "review:sensitive_claim_review"]
        self.assertEqual(len(digest), 1)
        self.assertIn("(1)", digest[0]["item"])
        # Le signal n'est pas "traite" : il reste dans la file de review.
        self.assertEqual(len(self.sensitive_signals()), 1)

    def test_4_existing_insight_and_existing_cockpit_action_untouched(self):
        # Action creee AVANT le changement (comme les 14 existantes) : elle
        # reste projetee (mise a jour sur place), rien n'est supprime.
        legacy = actions.propose_signal_action(self.conn, self.sensitive_signals()[0])
        self.assertEqual(legacy["status"], "created")
        before = self.conn.execute("SELECT * FROM intelligence_mentor_insights_local").fetchall()
        actions.propose_priority_actions(self.conn, max_priority=2)
        rows = cockpit.build_projection(self.conn)
        self.assertEqual(
            [r["kind"] for r in rows if r["vault_ref"] == f"signal:{legacy['signal_key']}"],
            ["Action"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute("SELECT * FROM intelligence_mentor_insights_local").fetchall(),
            before)

    def test_5_other_signals_still_create_actions(self):
        plain_claim = claims.claims_for_source(self.conn, self.plain)[0]
        self.conn.execute("UPDATE intelligence_claims SET verification_status='contested' "
                          "WHERE id=?", (plain_claim["id"],))
        self.conn.commit()
        result = actions.propose_priority_actions(self.conn, max_priority=2)
        kinds = {item["signal_key"].split(":")[0] for item in result["items"]
                 if item["status"] == "created"}
        self.assertIn("contested_claim", kinds)
        rows = cockpit.build_projection(self.conn)
        self.assertTrue(any(r["vault_ref"].startswith("signal:contested_claim") for r in rows))


if __name__ == "__main__":
    unittest.main()
