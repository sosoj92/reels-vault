import sqlite3
import unittest

import vault_intelligence as vi
import vault_intelligence_signals as signals


class IntelligenceSignalTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE contents (
                id INTEGER PRIMARY KEY,
                category TEXT,
                title TEXT,
                description_manual TEXT,
                description_ai TEXT,
                description_original TEXT,
                transcript TEXT
            );
            CREATE TABLE resources (
                id INTEGER PRIMARY KEY,
                content_id INTEGER,
                name TEXT
            );
            CREATE TABLE actions (
                id INTEGER PRIMARY KEY,
                resource_id INTEGER,
                status TEXT
            );
            CREATE TABLE intelligence_resource_links (
                resource_id INTEGER PRIMARY KEY,
                entity_id INTEGER NOT NULL,
                linked_at TEXT,
                updated_at TEXT
            );
            """
        )
        vi.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _entity(self, name, status="later", needs_review=False):
        return vi.upsert_entity(
            self.conn,
            entity_type="solution",
            canonical_key=f"named:solution:{name.lower()}",
            name=name,
            attention_status=status,
            metadata={"needs_review": needs_review},
        )

    def _source(self, source_url, content_id, transcript, title="Saved source"):
        self.conn.execute(
            """
            INSERT INTO contents(
                id, category, title, description_manual,
                description_ai, description_original, transcript
            ) VALUES (?, ?, ?, NULL, NULL, NULL, ?)
            """,
            (content_id, "General", title, transcript),
        )
        self.conn.commit()
        return vi.upsert_source(
            self.conn,
            source_url=source_url,
            platform="test",
            source_type="video",
            title=title,
            content_id=content_id,
        )

    def test_contested_claim_is_priority_one(self):
        entity = self._entity("Notion")
        source = self._source(
            "https://example.test/1",
            1,
            "Notion is discussed.",
        )
        vi.link_entity_source(
            self.conn,
            entity_id=entity,
            source_id=source,
        )
        claim = vi.record_claim(
            self.conn,
            entity_id=entity,
            source_id=source,
            claim_text="A contested statement.",
            verification_status="contested",
            verification_source_id=source,
        )

        queue = signals.collect_signals(self.conn)
        item = next(
            x for x in queue["items"]
            if x["key"] == f"contested_claim:{claim}"
        )

        self.assertEqual(item["priority"], 1)
        self.assertEqual(item["verification_status"], "contested")
        self.assertFalse(queue["writes_performed"])

    def test_sensitive_unverified_claim_is_priority_one(self):
        entity = self._entity("Health tool")
        source = self._source(
            "https://example.test/medical",
            2,
            "This medical treatment discusses dosage for a patient.",
            title="Medical treatment",
        )
        vi.link_entity_source(
            self.conn,
            entity_id=entity,
            source_id=source,
        )
        claim = vi.record_claim(
            self.conn,
            entity_id=entity,
            source_id=source,
            claim_text="The source asserts a treatment claim.",
            verification_status="asserted",
        )

        queue = signals.collect_signals(self.conn)
        item = next(
            x for x in queue["items"]
            if x["key"] == f"sensitive_claim_review:{claim}"
        )

        self.assertEqual(item["priority"], 1)
        self.assertEqual(item["kind"], "sensitive_claim_review")

    def test_identity_review_and_unanalysed_source_are_derived(self):
        entity = self._entity("Unknown Tool", needs_review=True)
        source = self._source(
            "https://example.test/tool",
            3,
            "A workflow automation tool is presented.",
        )
        vi.link_entity_source(
            self.conn,
            entity_id=entity,
            source_id=source,
        )

        queue = signals.collect_signals(self.conn)
        keys = {x["key"]: x for x in queue["items"]}

        self.assertEqual(
            keys[f"identity_review:{entity}"]["priority"],
            3,
        )
        self.assertEqual(
            keys[f"source_analysis_needed:{source}"]["priority"],
            3,
        )

    def test_now_identity_review_is_priority_two(self):
        entity = self._entity("Current Tool", status="now", needs_review=True)

        queue = signals.collect_signals(self.conn)
        item = next(
            x for x in queue["items"]
            if x["key"] == f"identity_review:{entity}"
        )

        self.assertEqual(item["priority"], 2)

    def test_sensitive_unanalysed_source_is_priority_two(self):
        entity = self._entity("Admin source")
        source = self._source(
            "https://example.test/admin",
            4,
            "France Travail allocation administrative procedure.",
            title="France Travail update",
        )
        vi.link_entity_source(
            self.conn,
            entity_id=entity,
            source_id=source,
        )

        queue = signals.collect_signals(self.conn)
        item = next(
            x for x in queue["items"]
            if x["key"] == f"source_analysis_needed:{source}"
        )

        self.assertEqual(item["priority"], 2)

    def test_now_entity_only_signals_when_no_open_linked_action(self):
        no_action = self._entity("No Action", status="now")
        with_action = self._entity("With Action", status="now")

        self.conn.execute(
            "INSERT INTO resources(id, content_id, name) VALUES (10, NULL, 'No Action')"
        )
        self.conn.execute(
            "INSERT INTO resources(id, content_id, name) VALUES (11, NULL, 'With Action')"
        )
        self.conn.execute(
            """
            INSERT INTO intelligence_resource_links(resource_id, entity_id, linked_at, updated_at)
            VALUES (10, ?, 'now', 'now'), (11, ?, 'now', 'now')
            """,
            (no_action, with_action),
        )
        self.conn.execute(
            "INSERT INTO actions(id, resource_id, status) VALUES (1, 11, 'todo')"
        )
        self.conn.commit()

        queue = signals.collect_signals(self.conn)
        keys = {x["key"] for x in queue["items"]}

        self.assertIn(f"now_without_action:{no_action}", keys)
        self.assertNotIn(f"now_without_action:{with_action}", keys)

    def test_priority_filter_is_explicit(self):
        self._entity("Needs Review", needs_review=True)

        queue = signals.collect_signals(self.conn, priority=2)

        self.assertTrue(queue["items"])
        self.assertTrue(all(x["priority"] == 2 for x in queue["items"]))


if __name__ == "__main__":
    unittest.main()
