import json
import sqlite3
import unittest

import vault_intelligence as vi
import vault_intelligence_bridge as bridge


class IntelligenceBridgeTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

        self.conn.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE contents (
                id INTEGER PRIMARY KEY,
                source_url TEXT NOT NULL UNIQUE,
                platform TEXT,
                content_type TEXT,
                author TEXT,
                title TEXT,
                category TEXT,
                ingested_at TEXT,
                created_at TEXT
            );

            CREATE TABLE resources (
                id INTEGER PRIMARY KEY,
                content_id INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                url TEXT,
                type TEXT,
                description TEXT
            );

            CREATE TABLE subthemes (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT
            );

            CREATE TABLE content_subthemes (
                content_id INTEGER NOT NULL,
                subtheme_id INTEGER NOT NULL
            );

            CREATE TABLE tags (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );

            CREATE TABLE content_tags (
                content_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL
            );
            """
        )

        vi.ensure_schema(self.conn)
        bridge.ensure_bridge_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _insert_content_one(self):
        self.conn.execute(
            """
            INSERT INTO contents(
                id, source_url, platform, content_type, author, title,
                category, ingested_at, created_at
            ) VALUES (1, ?, 'TikTok', 'video', 'creator', 'Demo',
                      'Business', '2026-09-26', '2026-09-26')
            """,
            ("https://www.tiktok.com/@creator/video/123",),
        )
        self.conn.execute(
            """
            INSERT INTO resources(id, content_id, name, url, type, description)
            VALUES (10, 1, 'Notion', NULL, 'outil', 'Cockpit de travail')
            """
        )
        self.conn.execute(
            """
            INSERT INTO resources(id, content_id, name, url, type, description)
            VALUES (
                11, 1, 'Agency Agents',
                'https://github.com/msitarzewski/agency-agents',
                'autre', 'Bibliotheque d agents'
            )
            """
        )
        self.conn.commit()

    def test_promote_content_is_non_destructive_and_idempotent(self):
        self._insert_content_one()

        before_content = dict(
            self.conn.execute("SELECT * FROM contents WHERE id = 1").fetchone()
        )
        before_resources = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM resources WHERE content_id = 1 ORDER BY id"
            ).fetchall()
        ]

        first = bridge.promote_content(self.conn, 1)
        second = bridge.promote_content(self.conn, 1)

        self.assertEqual(first["source_id"], second["source_id"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM intelligence_sources"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM intelligence_entities"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM intelligence_resource_links"
            ).fetchone()[0],
            2,
        )

        after_content = dict(
            self.conn.execute("SELECT * FROM contents WHERE id = 1").fetchone()
        )
        after_resources = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM resources WHERE content_id = 1 ORDER BY id"
            ).fetchall()
        ]

        self.assertEqual(before_content, after_content)
        self.assertEqual(before_resources, after_resources)

    def test_github_resource_becomes_first_class_repo(self):
        self._insert_content_one()
        bridge.promote_content(self.conn, 1)

        repo = self.conn.execute(
            """
            SELECT * FROM intelligence_entities
             WHERE canonical_key = 'github:msitarzewski/agency-agents'
            """
        ).fetchone()

        self.assertIsNotNone(repo)
        self.assertEqual(repo["entity_type"], "github_repo")
        metadata = json.loads(repo["metadata"])
        self.assertEqual(metadata["owner"], "msitarzewski")
        self.assertEqual(metadata["repo"], "agency-agents")
        self.assertFalse(metadata["needs_review"])

    def test_name_only_resource_is_reused_across_sources_but_marked_for_review(self):
        self._insert_content_one()
        bridge.promote_content(self.conn, 1)

        self.conn.execute(
            """
            INSERT INTO contents(
                id, source_url, platform, content_type, author, title,
                category, ingested_at, created_at
            ) VALUES (2, ?, 'Instagram', 'carousel', 'other', 'Second',
                      'Business', '2026-09-26', '2026-09-26')
            """,
            ("https://www.instagram.com/p/abc",),
        )
        self.conn.execute(
            """
            INSERT INTO resources(id, content_id, name, url, type, description)
            VALUES (20, 2, 'Notion', NULL, 'outil', 'Autre mention')
            """
        )
        self.conn.commit()

        bridge.promote_content(self.conn, 2)

        notion = self.conn.execute(
            """
            SELECT * FROM intelligence_entities
             WHERE canonical_key = 'named:solution:notion'
            """
        ).fetchone()

        self.assertIsNotNone(notion)
        self.assertEqual(
            self.conn.execute(
                """
                SELECT COUNT(*) FROM intelligence_entities
                 WHERE canonical_key = 'named:solution:notion'
                """
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                """
                SELECT COUNT(*)
                  FROM intelligence_entity_sources
                 WHERE entity_id = ?
                """,
                (notion["id"],),
            ).fetchone()[0],
            2,
        )
        self.assertTrue(json.loads(notion["metadata"])["needs_review"])

    def test_tool_and_software_labels_converge_for_same_name(self):
        self._insert_content_one()
        bridge.promote_content(self.conn, 1)

        self.conn.execute(
            """
            INSERT INTO contents(
                id, source_url, platform, content_type, author, title,
                category, ingested_at, created_at
            ) VALUES (3, ?, 'Instagram', 'post', 'other', 'Third',
                      'Business', '2026-09-26', '2026-09-26')
            """,
            ("https://www.instagram.com/p/def",),
        )
        self.conn.execute(
            """
            INSERT INTO resources(id, content_id, name, url, type, description)
            VALUES (30, 3, 'Notion', NULL, 'application', 'Troisieme mention')
            """
        )
        self.conn.commit()

        bridge.promote_content(self.conn, 3)

        self.assertEqual(
            self.conn.execute(
                """
                SELECT COUNT(*) FROM intelligence_entities
                 WHERE canonical_key = 'named:solution:notion'
                """
            ).fetchone()[0],
            1,
        )

    def test_text_cannot_invent_a_github_repo(self):
        resource = {
            "id": 99,
            "name": "Jarvis assistant vocal",
            "url": None,
            "type": "autre",
            "description": None,
        }
        identity = bridge.resource_identity(resource)

        self.assertEqual(identity["entity_type"], "other")
        self.assertTrue(identity["needs_review"])
        self.assertFalse(identity["canonical_key"].startswith("github:"))


if __name__ == "__main__":
    unittest.main()
