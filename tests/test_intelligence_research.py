import sqlite3
import unittest

import vault_intelligence as vi
import vault_research as research


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, findings):
        self.findings = list(findings)

    def research_identity(self, item):
        return self.findings.pop(0)


class RateLimitedProvider:
    name = "rate-limited"
    model = "fake-model"

    def research_identity(self, item):
        raise research.ResearchRateLimitError("rate limit")


class ResearchWorkerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE contents (id INTEGER PRIMARY KEY);
            CREATE TABLE resources (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT,
                type TEXT
            );
            CREATE TABLE intelligence_resource_links (
                resource_id INTEGER PRIMARY KEY,
                entity_id INTEGER NOT NULL,
                linked_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        vi.ensure_schema(self.conn)
        research.ensure_research_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _provisional(self, name="Notion"):
        key = name.lower().replace(" ", "-")
        return vi.upsert_entity(
            self.conn,
            entity_type="solution",
            canonical_key=f"named:solution:{key}",
            name=name,
            metadata={"needs_review": True},
        )

    def _link_resource(self, entity_id, resource_id, name):
        self.conn.execute(
            "INSERT INTO resources(id, name, url, type) VALUES (?, ?, NULL, 'outil')",
            (resource_id, name),
        )
        self.conn.execute(
            """
            INSERT INTO intelligence_resource_links(
                resource_id, entity_id, linked_at, updated_at
            ) VALUES (?, ?, 'x', 'x')
            """,
            (resource_id, entity_id),
        )
        self.conn.commit()

    def test_safe_external_finding_requires_exact_name_and_same_host(self):
        entity = {
            "id": 1,
            "name": "Notion",
            "entity_type": "solution",
        }
        finding = research.ResearchFinding(
            status="resolved",
            canonical_name="Notion",
            canonical_url="https://www.notion.so/",
            entity_type="software",
            primary_source_url="https://www.notion.so/product",
            aliases=[],
        )
        self.assertTrue(research.finding_is_safe(entity, finding))

        wrong_host = research.ResearchFinding(
            status="resolved",
            canonical_name="Notion",
            canonical_url="https://www.notion.so/",
            entity_type="software",
            primary_source_url="https://example.com/notion",
            aliases=[],
        )
        self.assertFalse(research.finding_is_safe(entity, wrong_host))

    def test_alias_can_support_canonical_name_change(self):
        entity = {
            "id": 1,
            "name": "ChatGPT",
            "entity_type": "solution",
        }
        finding = research.ResearchFinding(
            status="resolved",
            canonical_name="OpenAI ChatGPT",
            canonical_url="https://chatgpt.com/",
            entity_type="software",
            primary_source_url="https://chatgpt.com/",
            aliases=["ChatGPT"],
        )
        self.assertTrue(research.finding_is_safe(entity, finding))

    def test_person_is_never_safe_for_automatic_external_resolution(self):
        entity = {
            "id": 1,
            "name": "Alex Smith",
            "entity_type": "person",
        }
        finding = research.ResearchFinding(
            status="resolved",
            canonical_name="Alex Smith",
            canonical_url="https://example.com/alex-smith",
            entity_type="person",
            primary_source_url="https://example.com/alex-smith",
            aliases=[],
        )
        self.assertFalse(research.finding_is_safe(entity, finding))

    def test_worker_saves_finding_then_applies_safe_resolution(self):
        entity_id = self._provisional("Notion")
        self._link_resource(entity_id, 1, "Notion")

        source = vi.upsert_source(
            self.conn,
            source_url="https://example.com/video",
            platform="tiktok",
            source_type="video",
        )
        vi.link_entity_source(
            self.conn,
            entity_id=entity_id,
            source_id=source,
        )

        provider = FakeProvider(
            [
                research.ResearchFinding(
                    status="resolved",
                    canonical_name="Notion",
                    canonical_url="https://www.notion.so/",
                    entity_type="software",
                    primary_source_url="https://www.notion.so/product",
                    aliases=["Notion"],
                    evidence_note="Official Notion product site.",
                )
            ]
        )

        results = research.research_queue_items(
            self.conn,
            provider,
            limit=1,
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["safe_to_apply"])

        # La recherche seule ne mute pas encore l'identite.
        before = self.conn.execute(
            "SELECT canonical_key FROM intelligence_entities WHERE id = ?",
            (entity_id,),
        ).fetchone()[0]
        self.assertEqual(before, "named:solution:notion")

        applied = research.apply_safe_findings(
            self.conn,
            provider_name="fake",
        )
        self.assertEqual(applied["applied"], 1)

        row = self.conn.execute(
            "SELECT * FROM intelligence_entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        self.assertEqual(row["canonical_key"], "url:https://www.notion.so/")
        self.assertEqual(row["entity_type"], "software")

        primary = self.conn.execute(
            """
            SELECT s.source_url, es.role
              FROM intelligence_sources s
              JOIN intelligence_entity_sources es ON es.source_id = s.id
             WHERE es.entity_id = ?
               AND es.role = 'primary'
            """,
            (entity_id,),
        ).fetchone()
        self.assertEqual(primary[0], "https://www.notion.so/product")
        self.assertEqual(primary[1], "primary")

    def test_ambiguous_finding_is_saved_but_never_applied(self):
        entity_id = self._provisional("Acme")
        self._link_resource(entity_id, 1, "Acme")

        provider = FakeProvider(
            [
                research.ResearchFinding(
                    status="ambiguous",
                    evidence_note="Multiple plausible Acme products.",
                )
            ]
        )

        results = research.research_queue_items(
            self.conn,
            provider,
            limit=1,
        )
        self.assertFalse(results[0]["safe_to_apply"])

        applied = research.apply_safe_findings(
            self.conn,
            provider_name="fake",
        )
        self.assertEqual(applied["applied"], 0)
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT id FROM intelligence_entities WHERE id = ?",
                (entity_id,),
            ).fetchone()
        )

    def test_retry_unsafe_allows_researching_an_unsafe_resolved_finding_again(self):
        entity_id = self._provisional("Claude Code")
        self._link_resource(entity_id, 1, "Claude Code")

        first = FakeProvider(
            [
                research.ResearchFinding(
                    status="resolved",
                    canonical_name="Claude Code",
                    canonical_url="https://claude.com/product/claude-code",
                    entity_type="software",
                    primary_source_url="https://github.com/anthropics/claude-code",
                    aliases=["Claude Code"],
                )
            ]
        )
        initial = research.research_queue_items(
            self.conn,
            first,
            limit=1,
        )
        self.assertFalse(initial[0]["safe_to_apply"])

        second = FakeProvider(
            [
                research.ResearchFinding(
                    status="resolved",
                    canonical_name="Claude Code",
                    canonical_url="https://claude.com/product/claude-code",
                    entity_type="software",
                    primary_source_url="https://claude.com/product/claude-code",
                    aliases=["Claude Code"],
                )
            ]
        )

        skipped = research.research_queue_items(
            self.conn,
            second,
            limit=1,
            retry_unsafe=False,
        )
        self.assertEqual(skipped, [])

        retried = research.research_queue_items(
            self.conn,
            second,
            limit=1,
            retry_unsafe=True,
        )
        self.assertEqual(len(retried), 1)
        self.assertTrue(retried[0]["safe_to_apply"])

    def test_persistent_rate_limit_stops_batch_after_first_error(self):
        one = self._provisional("One")
        two = self._provisional("Two")
        self._link_resource(one, 1, "One")
        self._link_resource(two, 2, "Two")

        results = research.research_queue_items(
            self.conn,
            RateLimitedProvider(),
            limit=2,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")

    def test_prompt_requests_same_host_primary_evidence_when_available(self):
        prompt = research.build_identity_prompt(
            {
                "name": "Claude Code",
                "entity_type": "solution",
                "resource_names": ["Claude Code"],
                "source_count": 4,
            }
        )
        self.assertIn("SAME HOSTNAME", prompt)
        self.assertIn("Do not use a GitHub repository", prompt)

    def test_provider_response_parser_handles_nested_assistant_message(self):
        payload = {
            "conversation_id": "x",
            "outputs": [
                {
                    "role": "assistant",
                    "content": (
                        '{"status":"resolved","canonical_name":"Notion",'
                        '"canonical_url":"https://www.notion.so/",'
                        '"entity_type":"software",'
                        '"primary_source_url":"https://www.notion.so/product",'
                        '"aliases":["Notion"],"evidence_note":"official"}'
                    ),
                }
            ],
        }

        finding = research.parse_provider_response(payload)
        self.assertEqual(finding.status, "resolved")
        self.assertEqual(finding.canonical_name, "Notion")
        self.assertEqual(finding.canonical_url, "https://www.notion.so/")


if __name__ == "__main__":
    unittest.main()
