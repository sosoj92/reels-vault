import json
import sqlite3
import unittest

import vault_identity as identity
import vault_intelligence as vi
import vault_resolution as resolution


class AutomaticResolutionTests(unittest.TestCase):
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
            """
        )
        vi.ensure_schema(self.conn)
        identity.ensure_identity_schema(self.conn)
        self.conn.execute(
            """
            CREATE TABLE intelligence_resource_links (
                resource_id INTEGER PRIMARY KEY,
                entity_id INTEGER NOT NULL,
                linked_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        resolution.ensure_resolution_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _link_resource(self, resource_id, name, url, entity_id):
        self.conn.execute(
            "INSERT INTO resources(id, name, url, type) VALUES (?, ?, ?, 'outil')",
            (resource_id, name, url),
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

    def test_exact_name_plus_single_canonical_url_is_auto_safe(self):
        provisional = vi.upsert_entity(
            self.conn,
            entity_type="solution",
            canonical_key="named:solution:notion",
            name="Notion",
            metadata={"needs_review": True},
        )
        canonical = vi.upsert_entity(
            self.conn,
            entity_type="software",
            canonical_key="url:https://www.notion.so/",
            name="Notion",
            url="https://www.notion.so/",
            metadata={"needs_review": False},
        )

        self._link_resource(1, "Notion", None, provisional)
        self._link_resource(2, "Notion", "https://www.notion.so/", canonical)

        result = resolution.discover_local_candidates(self.conn)
        self.assertEqual(result["proposed_entities"], 1)

        candidate = self.conn.execute(
            """
            SELECT safe_to_apply, status
              FROM intelligence_resolution_candidates
             WHERE entity_id = ?
            """,
            (provisional,),
        ).fetchone()
        self.assertEqual(candidate[0], 1)
        self.assertEqual(candidate[1], "proposed")

        applied = resolution.apply_safe_candidates(self.conn)
        self.assertEqual(applied["applied"], 1)

        self.assertIsNone(
            self.conn.execute(
                "SELECT id FROM intelligence_entities WHERE id = ?",
                (provisional,),
            ).fetchone()
        )
        final = self.conn.execute(
            "SELECT * FROM intelligence_entities WHERE id = ?",
            (canonical,),
        ).fetchone()
        self.assertFalse(json.loads(final["metadata"])["needs_review"])

    def test_two_canonical_targets_are_ambiguous_and_never_auto_applied(self):
        provisional = vi.upsert_entity(
            self.conn,
            entity_type="solution",
            canonical_key="named:solution:acme",
            name="Acme",
            metadata={"needs_review": True},
        )
        a = vi.upsert_entity(
            self.conn,
            entity_type="website",
            canonical_key="url:https://acme.example/",
            name="Acme",
            url="https://acme.example/",
            metadata={"needs_review": False},
        )
        b = vi.upsert_entity(
            self.conn,
            entity_type="website",
            canonical_key="url:https://acme.test/",
            name="Acme",
            url="https://acme.test/",
            metadata={"needs_review": False},
        )

        self._link_resource(1, "Acme", None, provisional)
        self._link_resource(2, "Acme", "https://acme.example/", a)
        self._link_resource(3, "Acme", "https://acme.test/", b)

        discovered = resolution.discover_local_candidates(self.conn)
        self.assertEqual(discovered["ambiguous_entities"], 1)

        rows = self.conn.execute(
            """
            SELECT safe_to_apply, status
              FROM intelligence_resolution_candidates
             WHERE entity_id = ?
            """,
            (provisional,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row[0] == 0 for row in rows))
        self.assertTrue(all(row[1] == "ambiguous" for row in rows))

        applied = resolution.apply_safe_candidates(self.conn)
        self.assertEqual(applied["applied"], 0)
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT id FROM intelligence_entities WHERE id = ?",
                (provisional,),
            ).fetchone()
        )

    def test_person_name_match_is_never_auto_merged(self):
        provisional = vi.upsert_entity(
            self.conn,
            entity_type="person",
            canonical_key="named:person:alex-smith",
            name="Alex Smith",
            metadata={"needs_review": True},
        )
        canonical = vi.upsert_entity(
            self.conn,
            entity_type="person",
            canonical_key="url:https://example.com/alex-smith",
            name="Alex Smith",
            url="https://example.com/alex-smith",
            metadata={"needs_review": False},
        )

        self._link_resource(1, "Alex Smith", None, provisional)
        self._link_resource(2, "Alex Smith", "https://example.com/alex-smith", canonical)

        resolution.discover_local_candidates(self.conn)

        row = self.conn.execute(
            """
            SELECT safe_to_apply
              FROM intelligence_resolution_candidates
             WHERE entity_id = ?
            """,
            (provisional,),
        ).fetchone()
        self.assertEqual(row[0], 0)

        queue = resolution.research_queue(self.conn)
        item = next(x for x in queue if x["entity_id"] == provisional)
        self.assertEqual(
            item["reason"],
            "manual_or_external_confirmation_required",
        )

    def test_primary_evidence_does_not_inflate_research_priority(self):
        provisional = vi.upsert_entity(
            self.conn,
            entity_type="solution",
            canonical_key="named:solution:notion",
            name="Notion",
            metadata={"needs_review": True},
        )
        self._link_resource(1, "Notion", None, provisional)

        mention_a = vi.upsert_source(
            self.conn,
            source_url="https://example.com/mention-a",
        )
        mention_b = vi.upsert_source(
            self.conn,
            source_url="https://example.com/mention-b",
        )
        primary = vi.upsert_source(
            self.conn,
            source_url="https://www.notion.so/",
            source_type="primary_identity_source",
        )
        vi.link_entity_source(
            self.conn,
            entity_id=provisional,
            source_id=mention_a,
        )
        vi.link_entity_source(
            self.conn,
            entity_id=provisional,
            source_id=mention_b,
        )
        vi.link_entity_source(
            self.conn,
            entity_id=provisional,
            source_id=primary,
            role="primary",
        )

        queue = resolution.research_queue(self.conn)
        item = next(x for x in queue if x["entity_id"] == provisional)
        self.assertEqual(item["source_count"], 2)

    def test_unmatched_entity_goes_to_primary_source_research_queue(self):
        provisional = vi.upsert_entity(
            self.conn,
            entity_type="solution",
            canonical_key="named:solution:unknown-tool",
            name="Unknown Tool",
            metadata={"needs_review": True},
        )
        self._link_resource(1, "Unknown Tool", None, provisional)

        resolution.discover_local_candidates(self.conn)
        queue = resolution.research_queue(self.conn)

        item = next(x for x in queue if x["entity_id"] == provisional)
        self.assertEqual(item["reason"], "primary_source_research_required")
        self.assertEqual(item["local_candidates"], [])


if __name__ == "__main__":
    unittest.main()
