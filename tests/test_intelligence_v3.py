import json
import sqlite3
import unittest

import vault_intelligence as vi


class IntelligenceV3Tests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

        # Tables V2 minimales necessaires aux foreign keys V3.
        self.conn.execute("CREATE TABLE contents (id INTEGER PRIMARY KEY)")
        self.conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")

        vi.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_schema_is_additive(self):
        self.conn.execute("INSERT INTO contents(id) VALUES (42)")
        self.assertEqual(
            self.conn.execute("SELECT id FROM contents").fetchone()[0],
            42,
        )

        version = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'intelligence_schema_version'"
        ).fetchone()[0]
        self.assertEqual(version, "1")

    def test_entity_is_deduplicated_and_enriched(self):
        first = vi.upsert_entity(
            self.conn,
            entity_type="tool",
            canonical_key="tool:notion",
            name="Notion",
            metadata={"category": "workspace"},
        )
        second = vi.upsert_entity(
            self.conn,
            entity_type="tool",
            canonical_key="tool:notion",
            name="Notion",
            summary="Cockpit",
            metadata={"use": "cockpit"},
        )

        self.assertEqual(first, second)

        row = vi.get_entity_by_key(self.conn, "tool:notion")
        self.assertEqual(row["summary"], "Cockpit")
        self.assertEqual(
            json.loads(row["metadata"]),
            {"category": "workspace", "use": "cockpit"},
        )

    def test_sources_relations_and_claim_verification(self):
        tool = vi.upsert_entity(
            self.conn,
            entity_type="tool",
            canonical_key="tool:notion",
            name="Notion",
        )
        repo = vi.upsert_github_repo(
            self.conn,
            url="https://github.com/msitarzewski/agency-agents",
        )
        video = vi.upsert_source(
            self.conn,
            source_url="https://www.tiktok.com/@example/video/123",
            platform="tiktok",
            source_type="video",
        )
        primary = vi.upsert_source(
            self.conn,
            source_url="https://github.com/msitarzewski/agency-agents",
            platform="github",
            source_type="repository",
        )

        vi.link_entity_source(
            self.conn,
            entity_id=repo,
            source_id=video,
            role="mentions",
        )

        relation_id = vi.add_relation(
            self.conn,
            subject_entity_id=repo,
            predicate="useful_for",
            object_entity_id=tool,
            source_id=video,
        )

        claim_id = vi.record_claim(
            self.conn,
            source_id=video,
            entity_id=repo,
            claim_text="The video says this repository is useful for orchestration.",
            verification_status="verified",
            verification_source_id=primary,
        )

        self.assertGreater(relation_id, 0)
        self.assertGreater(claim_id, 0)

    def test_github_repo_requires_explicit_repository_url(self):
        identity = vi.github_repo_identity(
            "https://github.com/sosoj92/jarvis-assistant-vocal.git"
        )
        self.assertEqual(
            identity["canonical_key"],
            "github:sosoj92/jarvis-assistant-vocal",
        )

        with self.assertRaises(vi.IntelligenceError):
            vi.github_repo_identity("Jarvis assistant vocal")


if __name__ == "__main__":
    unittest.main()
