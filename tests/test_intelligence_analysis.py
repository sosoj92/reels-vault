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
