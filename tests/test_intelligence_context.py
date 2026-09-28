import sqlite3
import unittest

import vault_intelligence as vi
import vault_intelligence_context as context


class IntelligenceContextTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("CREATE TABLE contents (id INTEGER PRIMARY KEY)")
        self.conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        vi.ensure_schema(self.conn)

        self.notion = vi.upsert_entity(
            self.conn,
            entity_type="solution",
            canonical_key="url:https://www.notion.so/",
            name="Notion",
            url="https://www.notion.so/",
            summary="Workspace used as a cockpit.",
            attention_status="now",
        )
        self.make = vi.upsert_entity(
            self.conn,
            entity_type="solution",
            canonical_key="url:https://www.make.com/",
            name="Make",
            url="https://www.make.com/",
            summary="Automation platform.",
            attention_status="later",
        )

        self.video = vi.upsert_source(
            self.conn,
            source_url="https://example.test/video/automation",
            platform="test",
            source_type="video",
            title="Automating a research workflow",
        )
        self.official = vi.upsert_source(
            self.conn,
            source_url="https://www.notion.so/help",
            platform="web",
            source_type="official",
            title="Notion Help",
        )

        vi.link_entity_source(
            self.conn,
            entity_id=self.notion,
            source_id=self.video,
            role="recommends",
        )
        vi.link_entity_source(
            self.conn,
            entity_id=self.notion,
            source_id=self.official,
            role="primary",
        )
        vi.link_entity_source(
            self.conn,
            entity_id=self.make,
            source_id=self.video,
            role="mentions",
        )

        vi.record_claim(
            self.conn,
            entity_id=self.notion,
            source_id=self.video,
            claim_text="The source recommends Notion for research capture.",
            verification_status="asserted",
        )
        vi.record_claim(
            self.conn,
            entity_id=self.notion,
            source_id=self.video,
            claim_text="Notion provides an official help center.",
            verification_status="verified",
            verification_source_id=self.official,
        )
        vi.record_claim(
            self.conn,
            entity_id=self.make,
            source_id=self.video,
            claim_text="Make is presented as useful for automation workflows.",
            verification_status="asserted",
        )

        vi.add_relation(
            self.conn,
            subject_entity_id=self.notion,
            predicate="integrates_with",
            object_entity_id=self.make,
            source_id=self.video,
        )

    def tearDown(self):
        self.conn.close()

    def test_context_can_match_claim_text_not_only_entity_name(self):
        pack = context.build_context_pack(
            self.conn,
            "research capture",
        )

        self.assertGreaterEqual(pack["result_count"], 1)
        self.assertEqual(pack["entities"][0]["entity"]["name"], "Notion")
        self.assertIn("claim", pack["entities"][0]["match"]["matched_on"])
        self.assertFalse(pack["generation"]["llm_used"])
        self.assertFalse(pack["generation"]["embeddings_used"])

    def test_context_preserves_epistemic_status_and_verification_source(self):
        pack = context.build_context_pack(
            self.conn,
            "official help center",
        )
        notion = next(
            item for item in pack["entities"]
            if item["entity"]["name"] == "Notion"
        )
        verified = next(
            claim for claim in notion["claims"]
            if claim["verification_status"] == "verified"
        )

        self.assertEqual(
            verified["verification_source_url"],
            "https://www.notion.so/help",
        )
        self.assertEqual(
            notion["epistemic"]["verification_counts"]["asserted"],
            1,
        )
        self.assertEqual(
            notion["epistemic"]["verification_counts"]["verified"],
            1,
        )

    def test_context_includes_grounded_relation(self):
        pack = context.build_context_pack(
            self.conn,
            "Make automation",
        )
        names = {
            item["entity"]["name"] for item in pack["entities"]
        }
        self.assertIn("Make", names)

        notion = next(
            item for item in pack["entities"]
            if item["entity"]["name"] == "Notion"
        )
        relation = next(
            rel for rel in notion["relations"]
            if rel["predicate"] == "integrates_with"
        )
        self.assertEqual(relation["related_entity_name"], "Make")
        self.assertEqual(
            relation["source_url"],
            "https://example.test/video/automation",
        )

    def test_context_is_bounded(self):
        pack = context.build_context_pack(
            self.conn,
            "automation",
            entity_limit=1,
            claims_per_entity=1,
            sources_per_entity=1,
            relations_per_entity=1,
        )

        self.assertEqual(len(pack["entities"]), 1)
        self.assertLessEqual(len(pack["entities"][0]["claims"]), 1)
        self.assertLessEqual(len(pack["entities"][0]["sources"]), 1)
        self.assertLessEqual(len(pack["entities"][0]["relations"]), 1)

    def test_empty_query_is_rejected(self):
        with self.assertRaises(context.IntelligenceContextError):
            context.build_context_pack(self.conn, "   ")


if __name__ == "__main__":
    unittest.main()
