import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import json
import unittest

import vault_claims as claims
from _v2_fixtures import add_source, claim, make_db


class ClaimExtractionTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.rtk = add_source(
            self.conn,
            "https://example.test/rtk",
            "RTK pour les agents de code",
            author="auteur.rtk",
            transcript=(
                "Voici mon test. RTK réduit la consommation de tokens de 90 % "
                "sur les tâches de code. Il s'installe en une commande."
            ),
        )
        self.opinion = add_source(
            self.conn,
            "https://example.test/notion",
            "Mon avis sur Notion",
            author="auteur.notion",
            transcript="Honnêtement je pense que Notion est le meilleur outil de productivité.",
        )
        self.health = add_source(
            self.conn,
            "https://example.test/tsg",
            "TSG et TDAH",
            author="auteur.sante",
            category="Santé & bien-être",
            transcript="Une étude sur des rats montre que le TSG réduit l'hyperactivité.",
        )

    def tearDown(self):
        self.conn.close()

    def extract(self, source_id, items, **kwargs):
        extractor = claims.StaticClaimExtractor({source_id: items})
        return claims.extract_source(self.conn, source_id, extractor, **kwargs)

    # V2-1 : claim factuel simple, extraction correcte + provenance.
    def test_simple_fact_is_extracted_with_provenance(self):
        result = self.extract(self.rtk, [claim(
            "RTK réduit de 90 % la consommation de tokens sur les tâches de code",
            "RTK", "réduit", "la consommation de tokens de 90 %",
            "RTK réduit la consommation de tokens de 90 %",
        )])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["claim_ids"]), 1)
        row = claims.claims_for_source(self.conn, self.rtk)[0]
        self.assertEqual(row["claim_kind"], "fact")
        self.assertEqual(row["subject"], "RTK")
        self.assertEqual(row["predicate_key"], "reduce")
        self.assertEqual(row["object_number"], 90.0)
        self.assertEqual(row["object_unit"], "%")
        self.assertEqual(row["validation_status"], "accepted")
        self.assertEqual(row["evidence_match"], "exact")
        self.assertEqual(row["evidence_field"], "transcript")
        # L'extrait stocke est le texte REEL de la source.
        self.assertIn("RTK réduit la consommation de tokens de 90 %", row["evidence_excerpt"])
        provenance = json.loads(row["provenance"])
        for key in ("extractor", "model", "extraction_version", "extraction_id",
                    "source_fingerprint", "extracted_at", "source_url"):
            self.assertIn(key, provenance)
        self.assertEqual(row["source_id"], self.rtk)
        self.assertIsNotNone(row["content_id"])
        self.assertIsNotNone(row["observed_at"])
        self.assertEqual(row["verification_status"], "asserted")

    # V2-2 : une opinion ne devient jamais un fait.
    def test_opinion_never_becomes_fact(self):
        result = self.extract(self.opinion, [claim(
            "Notion est le meilleur outil de productivité",
            "Notion", "est", "le meilleur outil de productivité",
            "je pense que Notion est le meilleur outil de productivité",
            kind="fact", confidence=0.95,
        )])
        row = claims.claims_for_source(self.conn, self.opinion)[0]
        self.assertEqual(row["claim_kind"], "opinion")
        self.assertIn("kind_downgraded_from_fact_to_opinion", json.loads(row["review_reasons"]))
        self.assertLessEqual(row["confidence"], 0.6)
        self.assertEqual(row["verification_status"], "unverified")
        self.assertEqual(len(result["claim_ids"]), 1)

    def test_evidence_absent_from_source_is_rejected(self):
        result = self.extract(self.rtk, [claim(
            "RTK est utilisé par 10 000 entreprises", "RTK", "est utilisé par",
            "10 000 entreprises", "RTK est utilisé par 10 000 entreprises",
        )])
        self.assertEqual(result["claim_ids"], [])
        self.assertEqual(result["rejected"][0]["reason"], "evidence_not_found_in_source")

    def test_invented_number_is_rejected(self):
        result = self.extract(self.rtk, [claim(
            "RTK réduit de 99 % la consommation", "RTK", "réduit", "99 %",
            "RTK réduit la consommation de tokens de 90 %",
        )])
        self.assertEqual(result["claim_ids"], [])
        self.assertEqual(result["rejected"][0]["reason"], "object_number_not_in_evidence")

    def test_health_claim_is_cautious_and_scoped(self):
        self.extract(self.health, [claim(
            "Le TSG réduit l'hyperactivité", "TSG", "réduit", "l'hyperactivité",
            "le TSG réduit l'hyperactivité", confidence=0.9, evidence_type="study",
        )])
        row = claims.claims_for_source(self.conn, self.health)[0]
        self.assertEqual(row["sensitivity"], "health")
        self.assertEqual(row["validation_status"], "needs_review")
        self.assertLessEqual(row["confidence"], 0.5)
        self.assertEqual(row["verification_status"], "unverified")

    def test_modality_is_never_strengthened(self):
        conn = self.conn
        source = add_source(conn, "https://example.test/may", "Peut-être", author="x",
                            transcript="Cet outil pourrait diviser les coûts par deux.")
        extractor = claims.StaticClaimExtractor({source: [claim(
            "Cet outil divise les coûts par deux", "Cet outil", "divise", "les coûts par deux",
            "Cet outil pourrait diviser les coûts par deux", modality="asserted",
        )]})
        claims.extract_source(conn, source, extractor)
        row = claims.claims_for_source(conn, source)[0]
        self.assertEqual(row["modality"], "possible")
        self.assertNotEqual(row["claim_kind"], "fact")
        self.assertLessEqual(row["confidence"], 0.5)

    def test_missing_evidence_or_structure(self):
        result = self.extract(self.rtk, [
            {"claim_text": "RTK est rapide", "subject": "RTK", "predicate": "est",
             "object": "rapide", "kind": "fact", "confidence": 0.9, "evidence": ""},
            claim("Il s'installe en une commande", "", "", "",
                  "Il s'installe en une commande"),
        ])
        self.assertEqual(result["rejected"][0]["reason"], "missing_evidence")
        row = claims.claims_for_source(self.conn, self.rtk)[0]
        self.assertEqual(row["validation_status"], "needs_review")
        self.assertIn("missing_structure", json.loads(row["review_reasons"]))

    def test_idempotent_and_non_destructive_reextraction(self):
        items = [claim("RTK réduit la consommation de tokens", "RTK", "réduit",
                       "la consommation de tokens",
                       "RTK réduit la consommation de tokens de 90 %")]
        first = self.extract(self.rtk, items)
        cached = self.extract(self.rtk, items)
        self.assertEqual(cached["status"], "cached")
        forced = self.extract(self.rtk, items, force=True)
        self.assertNotEqual(first["extraction_id"], forced["extraction_id"])
        # L'ancien claim est retire du graphe actif, jamais supprime.
        total = self.conn.execute("SELECT COUNT(*) FROM intelligence_claim_structure").fetchone()[0]
        active = self.conn.execute(
            "SELECT COUNT(*) FROM intelligence_claim_structure WHERE superseded_at IS NULL"
        ).fetchone()[0]
        self.assertEqual((total, active), (2, 1))

    def test_dry_run_writes_nothing(self):
        result = self.extract(self.rtk, [claim(
            "RTK réduit la consommation de tokens", "RTK", "réduit", "la consommation",
            "RTK réduit la consommation de tokens de 90 %")], dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(len(result["claims"]), 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM intelligence_claims").fetchone()[0], 0)

    def test_extractor_failure_is_recorded_not_raised(self):
        extractor = claims.StaticClaimExtractor(
            {self.rtk: claims.ClaimExtractionError("Ollama indisponible")})
        result = claims.extract_source(self.conn, self.rtk, extractor)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM intelligence_claims").fetchone()[0], 0)

    def test_capability_becomes_available_with_structured_claims(self):
        import vault_intelligence_context as context

        before = context.build_context_pack(self.conn, "RTK")["capabilities"]
        self.assertEqual(before["contradiction_detection"], "unavailable_no_structured_claims")
        self.extract(self.rtk, [claim("RTK réduit la consommation de tokens", "RTK",
                                      "réduit", "la consommation de tokens",
                                      "RTK réduit la consommation de tokens de 90 %")])
        after = context.build_context_pack(self.conn, "RTK")["capabilities"]
        self.assertEqual(after["contradiction_detection"], "available")


class RealDataRegressionTests(unittest.TestCase):
    """Defauts observes lors de l'extraction sur une copie du Vault reel."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def extract(self, source_id, items):
        return claims.extract_source(
            self.conn, source_id, claims.StaticClaimExtractor({source_id: items}))

    def test_likes_are_not_turned_into_users(self):
        source = add_source(self.conn, "https://example.test/likes", "RTK",
                            transcript="Sur le GitHub, il y a plus de 80 000 likes.")
        self.extract(source, [claim(
            "RTK est utilisé par plus de 80 000 utilisateurs", "RTK", "est utilisé par",
            "plus de 80 000 utilisateurs", "il y a plus de 80 000 likes")])
        row = claims.claims_for_source(self.conn, source)[0]
        self.assertEqual(row["validation_status"], "needs_review")
        self.assertIn("object_not_grounded", json.loads(row["review_reasons"]))
        self.assertEqual(row["object_number"], 80000.0)

    def test_evidence_from_ai_summary_is_traced_as_second_hand(self):
        source = add_source(self.conn, "https://example.test/ai", "Pack LinkedIn",
                            transcript="Voici le pack.")
        self.conn.execute(
            "UPDATE contents SET description_ai = ? WHERE id = ?",
            ("Le pack contient 11 skills open source pour LinkedIn.", source))
        self.conn.commit()
        self.extract(source, [claim(
            "Le pack contient 11 skills open source", "Le pack", "contient",
            "11 skills open source", "Le pack contient 11 skills open source")])
        row = claims.claims_for_source(self.conn, source)[0]
        self.assertEqual(row["evidence_field"], "description_ai")
        self.assertIn("evidence_from_ai_summary", json.loads(row["review_reasons"]))
        self.assertLess(row["confidence"], 0.85)

    def test_marketing_superlative_is_opinion(self):
        source = add_source(self.conn, "https://example.test/perfect", "Guanfacine",
                            transcript="Perfect for inattention and working memory.")
        self.extract(source, [claim(
            "Guanfacine is good for inattention", "Guanfacine", "is", "inattention",
            "Perfect for inattention and working memory")])
        row = claims.claims_for_source(self.conn, source)[0]
        self.assertEqual(row["claim_kind"], "opinion")

    def test_empty_scope_is_silently_ignored(self):
        source = add_source(self.conn, "https://example.test/scope", "RTK",
                            transcript="RTK réduit la consommation de tokens.")
        self.extract(source, [claim(
            "RTK réduit la consommation de tokens", "RTK", "réduit", "la consommation de tokens",
            "RTK réduit la consommation de tokens", scope="general")])
        row = claims.claims_for_source(self.conn, source)[0]
        self.assertEqual(json.loads(row["review_reasons"]), [])
        self.assertIsNone(row["scope"])

    def test_truncated_model_json_keeps_only_complete_claims(self):
        truncated = ('{"claims": [{"claim_text": "A", "evidence": "x"}, '
                     '{"claim_text": "B", "evidence": "y"}, {"claim_text": "C", "evid')
        self.assertEqual([c["claim_text"] for c in claims.parse_claims_json(truncated)],
                         ["A", "B"])
        with self.assertRaises(claims.ClaimExtractionError):
            claims.parse_claims_json('{"claims": [{"claim_text": "cou')


class LockedRealVaultTests(unittest.TestCase):
    """
    Verrou V2 : sur le vrai Vault (simule par une base temporaire designee
    comme "vraie"), aucune ecriture V2 tant que la V2 n'est pas validee.
    """

    def setUp(self):
        import hashlib
        from unittest import mock

        import v2_guard

        self.hashlib = hashlib
        self.v2_guard = v2_guard
        self.path = _isolation.TMP / f"real_{self._testMethodName}.db"
        conn = make_db(str(self.path))
        self.source = add_source(conn, "https://example.test/rtk", "RTK",
                                 transcript="RTK réduit la consommation de tokens de 90 %.")
        conn.close()
        self.patches = [
            mock.patch.object(v2_guard, "REAL_DB", self.path),
            mock.patch.object(v2_guard, "FLAG", _isolation.TMP / f"flag_{self._testMethodName}"),
            mock.patch.dict("os.environ", {"VAULT_V2_WRITES": ""}),
        ]
        for patch in self.patches:
            patch.start()
        self.items = [claim("RTK réduit la consommation de tokens de 90 %", "RTK", "réduit",
                            "la consommation de tokens de 90 %",
                            "RTK réduit la consommation de tokens de 90 %")]

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def digest(self):
        return self.hashlib.sha256(self.path.read_bytes()).hexdigest()

    def connect(self):
        import sqlite3
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_write_is_blocked_and_file_is_untouched(self):
        before = self.digest()
        conn = self.connect()
        try:
            with self.assertRaises(self.v2_guard.V2WritesBlocked):
                claims.extract_source(conn, self.source,
                                     claims.StaticClaimExtractor({self.source: self.items}))
        finally:
            conn.close()
        self.assertEqual(self.digest(), before)

    def test_dry_run_and_reads_work_without_any_write(self):
        before = self.digest()
        conn = self.connect()
        try:
            result = claims.extract_source(
                conn, self.source, claims.StaticClaimExtractor({self.source: self.items}),
                dry_run=True)
            pending = claims.pending_source_ids(
                conn, claims.StaticClaimExtractor({}), 5)
            stored = claims.claims_for_source(conn, self.source)
        finally:
            conn.close()
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(len(result["claims"]), 1)
        self.assertEqual(pending, [self.source])
        self.assertEqual(stored, [])
        self.assertEqual(self.digest(), before)

    def test_unlocked_after_validation_flag(self):
        self.v2_guard.FLAG.write_text("valide", encoding="utf-8")
        conn = self.connect()
        try:
            result = claims.extract_source(
                conn, self.source, claims.StaticClaimExtractor({self.source: self.items}))
        finally:
            conn.close()
        self.assertEqual(len(result["claim_ids"]), 1)

    def test_other_databases_are_never_locked(self):
        import sqlite3
        memory = sqlite3.connect(":memory:")
        self.assertTrue(self.v2_guard.v2_writes_allowed(memory))
        memory.close()
        self.assertTrue(self.v2_guard.v2_writes_allowed(str(_isolation.TMP / "copie.db")))
        self.assertFalse(self.v2_guard.v2_writes_allowed(str(self.path)))


class ValidatorUnitTests(unittest.TestCase):
    def test_kind_detection(self):
        self.assertEqual(claims.detect_kinds("il faut absolument que tu l'installes")[0], "advice")
        self.assertEqual(claims.detect_kinds("GPT-7 va remplacer les développeurs")[0], "prediction")
        self.assertEqual(claims.detect_kinds("j'ai testé pendant un mois")[0], "personal_experience")
        self.assertEqual(claims.detect_kinds("l'outil utilise GPT"), [])

    def test_predicate_families(self):
        self.assertEqual(claims.predicate_key("réduit"), "reduce")
        self.assertEqual(claims.predicate_key("diminue"), "reduce")
        self.assertEqual(claims.predicate_key("reduces"), "reduce")
        self.assertEqual(claims.predicate_key("coûte"), "cost")

    def test_fuzzy_evidence_returns_real_source_text(self):
        located = claims.locate_evidence(
            "RTK reduit la consommation des tokens de 90 pour cent",
            {"transcript": "Bref, RTK réduit la consommation de tokens de 90 % sur le code."},
        )
        self.assertIsNotNone(located)
        field, excerpt, match = located
        self.assertEqual((field, match), ("transcript", "fuzzy"))
        self.assertIn("RTK réduit", excerpt)


if __name__ == "__main__":
    unittest.main()
