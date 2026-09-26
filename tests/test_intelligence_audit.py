import json
import sqlite3
import unittest

import vault_intelligence as vi
import vault_intelligence_audit as audit


class IntelligenceAuditTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("CREATE TABLE contents (id INTEGER PRIMARY KEY)")
        self.conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        vi.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_audit_reports_reuse_and_review(self):
        s1 = vi.upsert_source(
            self.conn,
            source_url="https://example.com/1",
            platform="web",
            source_type="article",
        )
        s2 = vi.upsert_source(
            self.conn,
            source_url="https://example.com/2",
            platform="web",
            source_type="article",
        )
        entity = vi.upsert_entity(
            self.conn,
            entity_type="software",
            canonical_key="named:solution:notion",
            name="Notion",
            metadata={"needs_review": True},
        )
        vi.link_entity_source(self.conn, entity_id=entity, source_id=s1)
        vi.link_entity_source(self.conn, entity_id=entity, source_id=s2)
        primary = vi.upsert_source(
            self.conn,
            source_url="https://www.notion.so/",
            platform="web",
            source_type="primary_identity_source",
        )
        vi.link_entity_source(
            self.conn,
            entity_id=entity,
            source_id=primary,
            role="primary",
        )

        result = audit.audit(self.conn, top=5)

        self.assertEqual(result["sources"], 3)
        self.assertEqual(result["entities"], 1)
        self.assertEqual(result["needs_review"], 1)
        self.assertEqual(result["reused_entities"], 1)
        self.assertEqual(
            result["top_reused_entities"][0]["canonical_key"],
            "named:solution:notion",
        )
        self.assertEqual(
            result["top_reused_entities"][0]["source_count"],
            2,
        )


if __name__ == "__main__":
    unittest.main()
