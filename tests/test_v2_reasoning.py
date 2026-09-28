import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import unittest
from datetime import datetime, timezone

import vault_claim_reasoning as reasoning
import vault_claims as claims
from _v2_fixtures import add_source, claim, make_db

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)


class ReasoningTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def source_with_claim(self, url, author, transcript, item, captured_at=None):
        source_id = add_source(self.conn, url, f"Source {url[-6:]}", author=author,
                               transcript=transcript, captured_at=captured_at)
        extractor = claims.StaticClaimExtractor({source_id: [item]})
        result = claims.extract_source(self.conn, source_id, extractor)
        self.assertEqual(len(result["claim_ids"]), 1, result)
        return source_id, result["claim_ids"][0]

    def assessments(self, kind=None):
        rows = self.conn.execute(
            "SELECT kind, claim_ids, source_ids, checks, status "
            "FROM intelligence_claim_assessments WHERE stale_at IS NULL"
        ).fetchall()
        import json
        out = [
            {"kind": r[0], "claim_ids": json.loads(r[1]), "source_ids": json.loads(r[2]),
             "checks": json.loads(r[3]), "status": r[4]}
            for r in rows
        ]
        return [a for a in out if kind is None or a["kind"] == kind]

    # V2-3 : deux sources concordantes -> convergence.
    def test_independent_sources_converge(self):
        s1, c1 = self.source_with_claim(
            "https://a.test/rtk-01", "auteur.a",
            "RTK réduit la consommation de tokens de 90 % sur le code.",
            claim("RTK réduit la consommation de tokens de 90 %", "RTK", "réduit",
                  "la consommation de tokens de 90 %",
                  "RTK réduit la consommation de tokens de 90 %"))
        s2, c2 = self.source_with_claim(
            "https://b.test/rtk-02", "auteur.b",
            "Avec RTK, la consommation de tokens diminue de 88 % chez moi aussi.",
            claim("RTK diminue la consommation de tokens de 88 %", "RTK", "diminue",
                  "la consommation de tokens de 88 %",
                  "la consommation de tokens diminue de 88 %"))
        reasoning.assess(self.conn, now=NOW)
        found = self.assessments("convergence")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["claim_ids"], sorted([c1, c2]))
        self.assertEqual(found[0]["source_ids"], sorted([s1, s2]))
        self.assertFalse(self.assessments("contradiction"))

    def test_same_author_is_not_independent_convergence(self):
        for suffix in ("rtk-11", "rtk-12"):
            self.source_with_claim(
                f"https://a.test/{suffix}", "meme.auteur",
                "RTK réduit la consommation de tokens de 90 %.",
                claim("RTK réduit la consommation de tokens de 90 %", "RTK", "réduit",
                      "la consommation de tokens de 90 %",
                      "RTK réduit la consommation de tokens de 90 %"))
        reasoning.assess(self.conn, now=NOW)
        self.assertFalse(self.assessments("convergence"))

    # V2-4 : affirmations reellement incompatibles -> contradiction tracee.
    def test_real_contradiction_with_exact_references(self):
        s1, c1 = self.source_with_claim(
            "https://a.test/astra-1", "auteur.a",
            "L'abonnement Astra coûte 20 dollars par mois.",
            claim("Astra coûte 20 $ par mois", "Astra", "coûte", "20 $ par mois",
                  "L'abonnement Astra coûte 20 dollars par mois"))
        s2, c2 = self.source_with_claim(
            "https://b.test/astra-2", "auteur.b",
            "Astra coûte 60 dollars par mois, attention.",
            claim("Astra coûte 60 $ par mois", "Astra", "coûte", "60 $ par mois",
                  "Astra coûte 60 dollars par mois"))
        reasoning.assess(self.conn, now=NOW)
        found = self.assessments("contradiction")
        self.assertEqual(len(found), 1)
        item = found[0]
        self.assertEqual(item["claim_ids"], sorted([c1, c2]))
        self.assertEqual(item["source_ids"], sorted([s1, s2]))
        self.assertEqual(item["status"], "needs_review")
        checks = item["checks"]
        self.assertTrue(checks["same_subject"])
        self.assertTrue(checks["same_predicate"])
        self.assertIn(checks["scope"], ("identical", "both_unspecified"))
        self.assertEqual(checks["time"], "compatible")
        self.assertTrue(checks["modalities_comparable"])
        self.assertEqual(checks["values"], "incompatible")

    def test_opposite_polarity_is_a_contradiction(self):
        self.source_with_claim(
            "https://a.test/free-1", "auteur.a", "L'outil Zeta est gratuit pour tous.",
            claim("Zeta est gratuit", "Zeta", "est", "gratuit", "L'outil Zeta est gratuit"))
        self.source_with_claim(
            "https://b.test/free-2", "auteur.b", "Non, Zeta n'est pas gratuit du tout.",
            claim("Zeta n'est pas gratuit", "Zeta", "est", "gratuit",
                  "Zeta n'est pas gratuit", polarity="negative"))
        reasoning.assess(self.conn, now=NOW)
        self.assertEqual(len(self.assessments("contradiction")), 1)

    # V2-5 : populations ou dates differentes -> PAS de fausse contradiction.
    def test_different_populations_are_not_a_contradiction(self):
        self.source_with_claim(
            "https://a.test/tsg-rats", "auteur.a",
            "Chez les rats, le TSG réduit l'hyperactivité de 40 %.",
            claim("Le TSG réduit l'hyperactivité de 40 % chez les rats", "TSG", "réduit",
                  "l'hyperactivité de 40 %", "le TSG réduit l'hyperactivité de 40 %",
                  scope="rats"))
        self.source_with_claim(
            "https://b.test/tsg-kids", "auteur.b",
            "Chez les enfants, le TSG réduit l'hyperactivité de 5 % seulement.",
            claim("Le TSG réduit l'hyperactivité de 5 % chez les enfants", "TSG", "réduit",
                  "l'hyperactivité de 5 %", "le TSG réduit l'hyperactivité de 5 %",
                  scope="enfants"))
        reasoning.assess(self.conn, now=NOW)
        self.assertFalse(self.assessments("contradiction"))
        self.assertFalse(self.assessments("possible_tension"))

    def test_different_dates_are_not_a_contradiction(self):
        self.source_with_claim(
            "https://a.test/price-20", "auteur.a", "En 2020, Astra coûtait 20 dollars.",
            claim("Astra coûtait 20 $ en 2020", "Astra", "coûte", "20 $",
                  "En 2020, Astra coûtait 20 dollars"))
        self.source_with_claim(
            "https://b.test/price-24", "auteur.b", "En 2024, Astra coûte 60 dollars.",
            claim("Astra coûte 60 $ en 2024", "Astra", "coûte", "60 $",
                  "En 2024, Astra coûte 60 dollars"))
        reasoning.assess(self.conn, now=NOW)
        self.assertFalse(self.assessments("contradiction"))
        superseded = self.assessments("possible_supersession")
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["checks"]["time"], "different_years")

    def test_different_wording_alone_is_never_a_contradiction(self):
        self.source_with_claim(
            "https://a.test/w1", "auteur.a", "RTK réduit la consommation de tokens.",
            claim("RTK réduit la consommation de tokens", "RTK", "réduit",
                  "la consommation de tokens", "RTK réduit la consommation de tokens"))
        self.source_with_claim(
            "https://b.test/w2", "auteur.b", "RTK s'installe avec une seule commande.",
            claim("RTK s'installe avec une commande", "RTK", "s'installe",
                  "avec une seule commande", "RTK s'installe avec une seule commande"))
        reasoning.assess(self.conn, now=NOW)
        self.assertEqual(self.assessments(), [])

    def test_opinions_disagreeing_are_only_a_possible_tension(self):
        self.source_with_claim(
            "https://a.test/op1", "auteur.a", "Je pense que Notion est génial.",
            claim("Notion est génial", "Notion", "est", "génial",
                  "Je pense que Notion est génial", kind="opinion"))
        self.source_with_claim(
            "https://b.test/op2", "auteur.b", "Pour moi, Notion n'est pas génial.",
            claim("Notion n'est pas génial", "Notion", "est", "génial",
                  "Notion n'est pas génial", kind="opinion", polarity="negative"))
        reasoning.assess(self.conn, now=NOW)
        self.assertFalse(self.assessments("contradiction"))
        self.assertEqual(len(self.assessments("possible_tension")), 1)

    # V2-6 : claim ancien + source plus recente -> obsolescence signalee.
    def test_older_claim_superseded_by_newer_source(self):
        _, old = self.source_with_claim(
            "https://a.test/codex-old", "auteur.a", "Codex est limité à 10 requêtes par jour.",
            claim("Codex est limité à 10 requêtes par jour", "Codex", "est limité à",
                  "10 requêtes par jour", "Codex est limité à 10 requêtes par jour"),
            captured_at="2025-01-10T00:00:00+00:00")
        _, new = self.source_with_claim(
            "https://b.test/codex-new", "auteur.b", "Codex est limité à 50 requêtes par jour.",
            claim("Codex est limité à 50 requêtes par jour", "Codex", "est limité à",
                  "50 requêtes par jour", "Codex est limité à 50 requêtes par jour"),
            captured_at="2026-09-01T00:00:00+00:00")
        reasoning.assess(self.conn, now=NOW)
        superseded = self.assessments("possible_supersession")
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["claim_ids"], sorted([old, new]))
        self.assertEqual(superseded[0]["status"], "needs_review")
        self.assertFalse(self.assessments("contradiction"))
        summary = self.conn.execute(
            "SELECT summary FROM intelligence_claim_assessments WHERE kind='possible_supersession'"
        ).fetchone()[0]
        self.assertIn("50 requêtes", summary.split("pourrait remplacer")[0])

    def test_reactivation_when_old_claim_is_confirmed_recently(self):
        self.source_with_claim(
            "https://a.test/react-old", "auteur.a", "RTK réduit la consommation de tokens de 90 %.",
            claim("RTK réduit la consommation de tokens de 90 %", "RTK", "réduit",
                  "la consommation de tokens de 90 %",
                  "RTK réduit la consommation de tokens de 90 %"),
            captured_at="2025-06-01T00:00:00+00:00")
        self.source_with_claim(
            "https://b.test/react-new", "auteur.b", "RTK réduit la consommation de tokens de 85 %.",
            claim("RTK réduit la consommation de tokens de 85 %", "RTK", "réduit",
                  "la consommation de tokens de 85 %",
                  "RTK réduit la consommation de tokens de 85 %"),
            captured_at="2026-09-20T00:00:00+00:00")
        reasoning.assess(self.conn, now=NOW)
        self.assertEqual(len(self.assessments("reactivation")), 1)

    def test_assess_is_idempotent_and_marks_stale(self):
        s1, _ = self.source_with_claim(
            "https://a.test/idem1", "auteur.a", "Astra coûte 20 dollars par mois.",
            claim("Astra coûte 20 $ par mois", "Astra", "coûte", "20 $",
                  "Astra coûte 20 dollars par mois"))
        self.source_with_claim(
            "https://b.test/idem2", "auteur.b", "Astra coûte 60 dollars par mois.",
            claim("Astra coûte 60 $ par mois", "Astra", "coûte", "60 $",
                  "Astra coûte 60 dollars par mois"))
        reasoning.assess(self.conn, now=NOW)
        reasoning.assess(self.conn, now=NOW)
        total = self.conn.execute("SELECT COUNT(*) FROM intelligence_claim_assessments").fetchone()[0]
        self.assertEqual(total, 1)
        # Re-extraction de s1 sans le claim : l'evaluation devient perimee.
        claims.extract_source(self.conn, s1, claims.StaticClaimExtractor({s1: []}), force=True)
        reasoning.assess(self.conn, now=NOW)
        self.assertEqual(self.assessments(), [])
        stale = self.conn.execute(
            "SELECT COUNT(*) FROM intelligence_claim_assessments WHERE stale_at IS NOT NULL"
        ).fetchone()[0]
        self.assertEqual(stale, 1)

    def test_assessments_for_sources_feeds_ask(self):
        s1, _ = self.source_with_claim(
            "https://a.test/ask1", "auteur.a", "Astra coûte 20 dollars par mois.",
            claim("Astra coûte 20 $ par mois", "Astra", "coûte", "20 $",
                  "Astra coûte 20 dollars par mois"))
        self.source_with_claim(
            "https://b.test/ask2", "auteur.b", "Astra coûte 60 dollars par mois.",
            claim("Astra coûte 60 $ par mois", "Astra", "coûte", "60 $",
                  "Astra coûte 60 dollars par mois"))
        reasoning.assess(self.conn, now=NOW)
        items = reasoning.assessments_for_sources(self.conn, [s1])
        self.assertEqual(items[0]["kind"], "contradiction")

        import vault_ask
        result = vault_ask.ask(self.conn, "Astra coûte combien ?", llm="off", semantic="off")
        self.assertIn("Contradiction à vérifier", result["answer"])
        self.assertIn(f"source {s1}", result["answer"])


if __name__ == "__main__":
    unittest.main()
