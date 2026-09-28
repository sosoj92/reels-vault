import json
import sqlite3
import unittest

import vault_identity as identity
import vault_intelligence as vi


class IdentityResolverTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE contents (id INTEGER PRIMARY KEY);
            CREATE TABLE resources (id INTEGER PRIMARY KEY);
            """
        )
        vi.ensure_schema(self.conn)
        identity.ensure_identity_schema(self.conn)
        self.conn.execute(
            """
            CREATE TABLE intelligence_resource_links (
                resource_id INTEGER PRIMARY KEY REFERENCES resources(id) ON DELETE CASCADE,
                entity_id INTEGER NOT NULL REFERENCES intelligence_entities(id) ON DELETE CASCADE,
                linked_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_resolve_entity_replaces_provisional_identity(self):
        entity_id = vi.upsert_entity(
            self.conn,
            entity_type="solution",
            canonical_key="named:solution:notion",
            name="Notion",
            metadata={"needs_review": True},
        )

        resolved_id = identity.resolve_entity(
            self.conn,
            entity_id=entity_id,
            canonical_key="url:https://www.notion.so/",
            canonical_name="Notion",
            canonical_url="https://www.notion.so/",
            entity_type="software",
            aliases=["Notion.so"],
        )

        self.assertEqual(resolved_id, entity_id)
        row = self.conn.execute(
            "SELECT * FROM intelligence_entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        self.assertEqual(row["canonical_key"], "url:https://www.notion.so/")
        self.assertEqual(row["entity_type"], "software")
        self.assertFalse(json.loads(row["metadata"])["needs_review"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM intelligence_identity_events WHERE action='resolve'"
            ).fetchone()[0],
            1,
        )

    def test_resolution_collision_merges_everything_into_canonical_entity(self):
        source_a = vi.upsert_source(
            self.conn,
            source_url="https://example.com/a",
        )
        source_b = vi.upsert_source(
            self.conn,
            source_url="https://example.com/b",
        )

        canonical = vi.upsert_entity(
            self.conn,
            entity_type="software",
            canonical_key="url:https://chatgpt.com/",
            name="ChatGPT",
            url="https://chatgpt.com/",
            metadata={"needs_review": False},
        )
        provisional = vi.upsert_entity(
            self.conn,
            entity_type="solution",
            canonical_key="named:solution:chatgpt",
            name="OpenAI ChatGPT",
            metadata={"needs_review": True},
        )
        other = vi.upsert_entity(
            self.conn,
            entity_type="company",
            canonical_key="named:company:openai",
            name="OpenAI",
        )

        vi.link_entity_source(
            self.conn,
            entity_id=canonical,
            source_id=source_a,
        )
        vi.link_entity_source(
            self.conn,
            entity_id=provisional,
            source_id=source_b,
        )

        vi.add_relation(
            self.conn,
            subject_entity_id=provisional,
            predicate="related_to",
            object_entity_id=other,
            source_id=source_b,
        )

        vi.record_claim(
            self.conn,
            source_id=source_b,
            entity_id=provisional,
            claim_text="Example claim",
        )

        self.conn.execute("INSERT INTO resources(id) VALUES (1)")
        self.conn.execute(
            """
            INSERT INTO intelligence_resource_links(resource_id, entity_id, linked_at, updated_at)
            VALUES (1, ?, 'x', 'x')
            """,
            (provisional,),
        )
        self.conn.commit()

        resolved = identity.resolve_entity(
            self.conn,
            entity_id=provisional,
            canonical_key="url:https://chatgpt.com/",
            canonical_name="ChatGPT",
            canonical_url="https://chatgpt.com/",
            entity_type="software",
        )

        self.assertEqual(resolved, canonical)
        self.assertIsNone(
            self.conn.execute(
                "SELECT id FROM intelligence_entities WHERE id = ?",
                (provisional,),
            ).fetchone()
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(DISTINCT source_id) FROM intelligence_entity_sources WHERE entity_id = ?",
                (canonical,),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT entity_id FROM intelligence_claims WHERE claim_text='Example claim'"
            ).fetchone()[0],
            canonical,
        )
        relation = self.conn.execute(
            "SELECT subject_entity_id, object_entity_id FROM intelligence_relations"
        ).fetchone()
        self.assertEqual(relation[0], canonical)
        self.assertEqual(relation[1], other)
        self.assertEqual(
            self.conn.execute(
                "SELECT entity_id FROM intelligence_resource_links WHERE resource_id = 1"
            ).fetchone()[0],
            canonical,
        )
        aliases = [
            row[0]
            for row in self.conn.execute(
                "SELECT alias FROM intelligence_entity_aliases WHERE entity_id = ?",
                (canonical,),
            ).fetchall()
        ]
        self.assertIn("OpenAI ChatGPT", aliases)

    def test_merge_drops_relation_that_would_become_self_link(self):
        a = vi.upsert_entity(
            self.conn,
            entity_type="solution",
            canonical_key="named:solution:a",
            name="A",
        )
        b = vi.upsert_entity(
            self.conn,
            entity_type="solution",
            canonical_key="named:solution:b",
            name="B",
        )
        vi.add_relation(
            self.conn,
            subject_entity_id=a,
            predicate="same_as",
            object_entity_id=b,
        )

        identity.merge_entities(
            self.conn,
            keep_id=a,
            merge_id=b,
            reason="duplicate",
        )

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM intelligence_relations"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
