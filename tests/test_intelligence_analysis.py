import json
import sqlite3
import unittest

import vault_analysis as analysis
import vault_intelligence as vi


class FakeAnalysisProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def analyze(self, payload):
        self.calls += 1
        return self.result


class FailingAnalysisProvider:
    name = "failing"
    model = "fail-model"

    def analyze(self, payload):
        raise analysis.AnalysisError("unavailable")


class SuccessfulAnalysisProvider:
    name = "success"
    model = "success-model"

    def __init__(self):
        self.calls = 0

    def analyze(self, payload):
        self.calls += 1
        return analysis.AnalysisResult(
            summary="Local/fallback analysis worked.",
            claims=[],
            relations=[],
        )


class SourceAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE contents (
                id INTEGER PRIMARY KEY,
                source_url TEXT NOT NULL UNIQUE,
                platform TEXT,
                content_type TEXT,
                author TEXT,
                title TEXT,
                description_original TEXT,
                description_ai TEXT,
                description_manual TEXT,
                transcript TEXT,
                category TEXT,
                ingested_at TEXT,
                created_at TEXT
            );
            """
        )
        vi.ensure_schema(self.conn)
        analysis.ensure_analysis_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _source(self):
        self.conn.execute(
            """
            INSERT INTO contents(
                id, source_url, platform, content_type, author, title,
                description_original, description_ai, description_manual,
                transcript, category, ingested_at, created_at
            ) VALUES (
                1, 'https://example.com/video', 'TikTok', 'video',
                'creator', 'Automation demo',
                'original description',
                'AI description',
                'manual description',
                'Notion integrates with GitHub. The creator recommends Notion.',
                'Business', '2026-09-26', '2026-09-26'
            )
            """
        )
        self.conn.commit()

        source_id = vi.upsert_source(
            self.conn,
            source_url="https://example.com/video",
            platform="TikTok",
            source_type="video",
            title="Automation demo",
            content_id=1,
        )
        notion = vi.upsert_entity(
            self.conn,
            entity_type="solution",
            canonical_key="named:solution:notion",
            name="Notion",
            metadata={"needs_review": True},
        )
        github = vi.upsert_entity(
            self.conn,
            entity_type="platform",
            canonical_key="url:https://github.com/",
            name="GitHub",
            url="https://github.com/",
            metadata={"needs_review": False},
        )
        vi.link_entity_source(
            self.conn,
            entity_id=notion,
            source_id=source_id,
        )
        vi.link_entity_source(
            self.conn,
            entity_id=github,
            source_id=source_id,
        )
        return source_id, notion, github

    def test_payload_uses_manual_description_and_linked_entities(self):
        source_id, notion, github = self._source()

        payload = analysis.source_payload(self.conn, source_id)

        self.assertEqual(payload["description"], "manual description")
        self.assertIn("Notion integrates with GitHub", payload["transcript"])
        self.assertEqual(
            {entity["id"] for entity in payload["entities"]},
            {notion, github},
        )

    def test_analysis_writes_asserted_claim_and_relation_then_is_cached(self):
        source_id, notion, github = self._source()

        provider = FakeAnalysisProvider(
            analysis.AnalysisResult(
                summary="The source presents an integration and recommendation.",
                claims=[
                    {
                        "entity_id": notion,
                        "claim_type": "recommendation",
                        "claim_text": "The creator recommends Notion.",
                        "evidence": "The creator recommends Notion.",
                    }
                ],
                relations=[
                    {
                        "subject_entity_id": notion,
                        "predicate": "integrates_with",
                        "object_entity_id": github,
                        "evidence": "Notion integrates with GitHub.",
                    }
                ],
            )
        )

        first = analysis.analyze_source(self.conn, source_id, provider)
        second = analysis.analyze_source(self.conn, source_id, provider)

        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["claims_written"], 1)
        self.assertEqual(first["relations_written"], 1)
        self.assertEqual(second["status"], "cached")
        self.assertEqual(provider.calls, 1)

        claim = self.conn.execute(
            "SELECT * FROM intelligence_claims"
        ).fetchone()
        self.assertEqual(claim["verification_status"], "asserted")
        self.assertEqual(claim["entity_id"], notion)
        notes = json.loads(claim["notes"])
        self.assertEqual(notes["claim_type"], "recommendation")

        relation = self.conn.execute(
            "SELECT * FROM intelligence_relations"
        ).fetchone()
        self.assertEqual(relation["subject_entity_id"], notion)
        self.assertEqual(relation["object_entity_id"], github)
        self.assertEqual(relation["predicate"], "integrates_with")

    def test_analysis_details_returns_names_evidence_and_status(self):
        source_id, notion, github = self._source()

        provider = FakeAnalysisProvider(
            analysis.AnalysisResult(
                summary="Reviewable analysis.",
                claims=[
                    {
                        "entity_id": notion,
                        "claim_type": "recommendation",
                        "claim_text": "The creator recommends Notion.",
                        "evidence": "The creator recommends Notion.",
                    }
                ],
                relations=[
                    {
                        "subject_entity_id": notion,
                        "predicate": "integrates_with",
                        "object_entity_id": github,
                        "evidence": "Notion integrates with GitHub.",
                    }
                ],
            )
        )

        created = analysis.analyze_source(self.conn, source_id, provider)
        details = analysis.analysis_details(
            self.conn,
            created["analysis_id"],
        )

        self.assertEqual(details["summary"], "Reviewable analysis.")
        self.assertEqual(details["claims"][0]["entity_name"], "Notion")
        self.assertEqual(
            details["claims"][0]["verification_status"],
            "asserted",
        )
        self.assertEqual(
            details["relations"][0]["subject_name"],
            "Notion",
        )
        self.assertEqual(
            details["relations"][0]["object_name"],
            "GitHub",
        )
        self.assertIn(
            "Notion integrates with GitHub",
            details["relations"][0]["evidence"],
        )

    def test_uncertainty_symbol_does_not_match_every_claim(self):
        self.assertFalse(
            analysis._contains_any(
                "This is a certain factual statement.",
                analysis.UNCERTAINTY_CUES,
            )
        )
        self.assertTrue(
            analysis._contains_any(
                "The reduction is ~95%.",
                analysis.UNCERTAINTY_CUES,
            )
        )

    def test_uncertainty_cues_use_token_boundaries(self):
        self.assertFalse(
            analysis._contains_any(
                "This claim is factual.",
                analysis.UNCERTAINTY_CUES,
            )
        )
        self.assertTrue(
            analysis._contains_any(
                "This aims to improve quality.",
                analysis.UNCERTAINTY_CUES,
            )
        )

    def test_active_summary_is_built_from_grounded_claims(self):
        source_id, notion, _ = self._source()

        provider = FakeAnalysisProvider(
            analysis.AnalysisResult(
                summary="A free-form provider summary that is not trusted directly.",
                claims=[
                    {
                        "entity_id": notion,
                        "claim_type": "fact",
                        "claim_text": "Notion is described in the source",
                        "evidence": "The creator recommends Notion.",
                    },
                    {
                        "entity_id": notion,
                        "claim_type": "recommendation",
                        "claim_text": "The creator recommends Notion",
                        "evidence": "The creator recommends Notion.",
                    },
                ],
                relations=[],
            )
        )

        result = analysis.analyze_source(self.conn, source_id, provider)

        self.assertEqual(
            result["summary"],
            "Notion is described in the source. The creator recommends Notion.",
        )
        stored = self.conn.execute(
            "SELECT raw_result FROM intelligence_source_analysis"
        ).fetchone()
        raw = json.loads(stored[0])
        self.assertEqual(
            raw["summary"],
            "A free-form provider summary that is not trusted directly.",
        )

    def test_active_summary_omits_rejected_claims(self):
        source_id, notion, _ = self._source()

        provider = FakeAnalysisProvider(
            analysis.AnalysisResult(
                summary="The feature passes five detectors.",
                claims=[
                    {
                        "entity_id": notion,
                        "claim_type": "capability",
                        "claim_text": "Notion passes five AI detectors.",
                        "evidence": "Notion vise à passer 5 détecteurs d'IA.",
                    },
                    {
                        "entity_id": notion,
                        "claim_type": "fact",
                        "claim_text": "Notion is mentioned in the source.",
                        "evidence": "The creator recommends Notion.",
                    },
                ],
                relations=[],
            )
        )

        result = analysis.analyze_source(self.conn, source_id, provider)

        self.assertEqual(
            result["summary"],
            "Notion is mentioned in the source.",
        )
        self.assertEqual(result["claims_written"], 1)
        self.assertEqual(result["rejected_items"], 1)

    def test_without_does_not_justify_invented_requirement(self):
        source_id, notion, _ = self._source()

        provider = FakeAnalysisProvider(
            analysis.AnalysisResult(
                summary="Demo",
                claims=[
                    {
                        "entity_id": notion,
                        "claim_type": "requirement",
                        "claim_text": "Notion requires a 23-hour build.",
                        "evidence": "The first 4.5 hours were run without the tool.",
                    }
                ],
                relations=[],
            )
        )

        result = analysis.analyze_source(self.conn, source_id, provider)

        self.assertEqual(result["claims_written"], 0)
        stored = self.conn.execute(
            "SELECT rejected_items FROM intelligence_source_analysis"
        ).fetchone()
        rejected = json.loads(stored[0])
        self.assertEqual(
            rejected[0]["reason"],
            "unsupported_requirement",
        )

    def test_claim_rejects_invented_requirement(self):
        source_id, notion, _ = self._source()

        provider = FakeAnalysisProvider(
            analysis.AnalysisResult(
                summary="Demo",
                claims=[
                    {
                        "entity_id": notion,
                        "claim_type": "requirement",
                        "claim_text": "Notion requires a repository with 230 stars.",
                        "evidence": "The repository has more than 230 stars.",
                    }
                ],
                relations=[],
            )
        )

        result = analysis.analyze_source(self.conn, source_id, provider)

        self.assertEqual(result["claims_written"], 0)
        self.assertEqual(result["rejected_items"], 1)
        stored = self.conn.execute(
            "SELECT rejected_items FROM intelligence_source_analysis"
        ).fetchone()
        rejected = json.loads(stored[0])
        self.assertEqual(
            rejected[0]["reason"],
            "unsupported_requirement",
        )

    def test_claim_preserves_uncertainty(self):
        source_id, notion, _ = self._source()

        provider = FakeAnalysisProvider(
            analysis.AnalysisResult(
                summary="Demo",
                claims=[
                    {
                        "entity_id": notion,
                        "claim_type": "capability",
                        "claim_text": "Notion passes five AI detectors.",
                        "evidence": "Notion vise à passer 5 détecteurs d'IA.",
                    }
                ],
                relations=[],
            )
        )

        result = analysis.analyze_source(self.conn, source_id, provider)

        self.assertEqual(result["claims_written"], 0)
        self.assertEqual(result["rejected_items"], 1)
        stored = self.conn.execute(
            "SELECT rejected_items FROM intelligence_source_analysis"
        ).fetchone()
        rejected = json.loads(stored[0])
        self.assertEqual(
            rejected[0]["reason"],
            "uncertainty_strengthened",
        )

    def test_claim_keeps_matching_uncertainty(self):
        source_id, notion, _ = self._source()

        provider = FakeAnalysisProvider(
            analysis.AnalysisResult(
                summary="Demo",
                claims=[
                    {
                        "entity_id": notion,
                        "claim_type": "fact",
                        "claim_text": "Notion likely refers to the intended product.",
                        "evidence": "Notion est probablement le produit visé.",
                    }
                ],
                relations=[],
            )
        )

        result = analysis.analyze_source(self.conn, source_id, provider)

        self.assertEqual(result["claims_written"], 1)
        self.assertEqual(result["rejected_items"], 0)

    def test_relation_requires_evidence_naming_both_entities(self):
        source_id, notion, github = self._source()

        provider = FakeAnalysisProvider(
            analysis.AnalysisResult(
                summary="Demo",
                claims=[],
                relations=[
                    {
                        "subject_entity_id": notion,
                        "predicate": "alternative_to",
                        "object_entity_id": github,
                        "evidence": "the page shows alternatives",
                    }
                ],
            )
        )

        result = analysis.analyze_source(self.conn, source_id, provider)

        self.assertEqual(result["relations_written"], 0)
        self.assertEqual(result["rejected_items"], 1)
        stored = self.conn.execute(
            "SELECT rejected_items FROM intelligence_source_analysis"
        ).fetchone()
        rejected = json.loads(stored[0])
        self.assertEqual(
            rejected[0]["reason"],
            "evidence_does_not_name_both_entities",
        )

    def test_new_analysis_supersedes_old_automatic_artifacts(self):
        source_id, notion, github = self._source()

        first_provider = FakeAnalysisProvider(
            analysis.AnalysisResult(
                summary="First",
                claims=[
                    {
                        "entity_id": notion,
                        "claim_type": "fact",
                        "claim_text": "Old claim.",
                        "evidence": "Notion old claim.",
                    }
                ],
                relations=[
                    {
                        "subject_entity_id": notion,
                        "predicate": "integrates_with",
                        "object_entity_id": github,
                        "evidence": "Notion integrates with GitHub.",
                    }
                ],
            )
        )
        first = analysis.analyze_source(
            self.conn,
            source_id,
            first_provider,
        )

        self.conn.execute(
            "UPDATE contents SET transcript = ? WHERE id = 1",
            ("Notion and GitHub are both mentioned, but no relation is stated.",),
        )
        self.conn.commit()

        second_provider = FakeAnalysisProvider(
            analysis.AnalysisResult(
                summary="Second",
                claims=[],
                relations=[],
            )
        )
        second = analysis.analyze_source(
            self.conn,
            source_id,
            second_provider,
        )

        self.assertNotEqual(first["analysis_id"], second["analysis_id"])
        old = self.conn.execute(
            """
            SELECT superseded_at
              FROM intelligence_source_analysis
             WHERE id = ?
            """,
            (first["analysis_id"],),
        ).fetchone()
        self.assertIsNotNone(old[0])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM intelligence_claims"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM intelligence_relations"
            ).fetchone()[0],
            0,
        )

    def test_unlinked_entity_and_invalid_predicate_are_rejected(self):
        source_id, notion, github = self._source()

        provider = FakeAnalysisProvider(
            analysis.AnalysisResult(
                summary="Demo",
                claims=[
                    {
                        "entity_id": 999,
                        "claim_type": "fact",
                        "claim_text": "Invented entity claim",
                        "evidence": "none",
                    },
                    {
                        "entity_id": None,
                        "claim_type": "fact",
                        "claim_text": "A general source claim.",
                        "evidence": "manual description",
                    },
                ],
                relations=[
                    {
                        "subject_entity_id": notion,
                        "predicate": "is_better_than",
                        "object_entity_id": github,
                        "evidence": "none",
                    },
                    {
                        "subject_entity_id": notion,
                        "predicate": "integrates_with",
                        "object_entity_id": 999,
                        "evidence": "none",
                    },
                ],
            )
        )

        result = analysis.analyze_source(self.conn, source_id, provider)

        self.assertEqual(result["claims_written"], 1)
        self.assertEqual(result["relations_written"], 0)
        self.assertEqual(result["rejected_items"], 3)

        claim = self.conn.execute(
            "SELECT entity_id, verification_status FROM intelligence_claims"
        ).fetchone()
        self.assertIsNone(claim[0])
        self.assertEqual(claim[1], "asserted")

    def test_primary_identity_source_is_not_exposed_as_source_entity(self):
        source_id, notion, github = self._source()
        primary = vi.upsert_entity(
            self.conn,
            entity_type="company",
            canonical_key="named:company:example",
            name="Example",
        )
        vi.link_entity_source(
            self.conn,
            entity_id=primary,
            source_id=source_id,
            role="primary",
        )

        payload = analysis.source_payload(self.conn, source_id)

        ids = {entity["id"] for entity in payload["entities"]}
        self.assertIn(notion, ids)
        self.assertIn(github, ids)
        self.assertNotIn(primary, ids)

    def test_auto_router_falls_back_without_implicit_mistral(self):
        success = SuccessfulAnalysisProvider()
        router = analysis.AutoAnalysisProvider(
            providers=[FailingAnalysisProvider(), success]
        )
        result = router.analyze({"title": "x"})

        self.assertEqual(result.summary, "Local/fallback analysis worked.")
        self.assertEqual(router.last_provider, "success")
        self.assertEqual(router.last_model, "success-model")
        self.assertEqual(success.calls, 1)

    def test_auto_router_records_actual_execution_provider(self):
        source_id, _, _ = self._source()
        success = SuccessfulAnalysisProvider()
        router = analysis.AutoAnalysisProvider(providers=[success])

        result = analysis.analyze_source(self.conn, source_id, router)

        self.assertEqual(result["status"], "completed")
        stored = self.conn.execute(
            "SELECT provider, model, raw_result FROM intelligence_source_analysis"
        ).fetchone()
        self.assertEqual(stored["provider"], "auto")
        self.assertEqual(stored["model"], "cost-router")
        raw = json.loads(stored["raw_result"])
        self.assertEqual(raw["execution_provider"], "success")
        self.assertEqual(raw["execution_model"], "success-model")

    def test_provider_aliases_route_to_local_qwen(self):
        for name in ("ollama", "qwen", "local"):
            provider = analysis.provider_from_name(name)
            self.assertIsInstance(provider, analysis.OllamaAnalysisProvider)
            self.assertEqual(provider.model, "qwen3:4b")
            self.assertEqual(provider.max_chars, 8000)
            self.assertEqual(provider.timeout, 600.0)

    def test_auto_router_uses_local_text_budget(self):
        local = analysis.OllamaAnalysisProvider(timeout=10)
        router = analysis.AutoAnalysisProvider(providers=[local])
        self.assertEqual(router.max_chars, 8000)

    def test_fingerprint_changes_when_source_text_changes(self):
        source_id, _, _ = self._source()

        before = analysis.source_fingerprint(
            analysis.source_payload(self.conn, source_id)
        )
        self.conn.execute(
            "UPDATE contents SET transcript = ? WHERE id = 1",
            ("Updated transcript",),
        )
        self.conn.commit()
        after = analysis.source_fingerprint(
            analysis.source_payload(self.conn, source_id)
        )

        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
