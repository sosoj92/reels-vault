import sqlite3
import unittest

import vault_intelligence as vi
import vault_intelligence_query as iq


class IntelligenceQueryTests(unittest.TestCase):
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
            summary="Workspace and knowledge tool.",
            attention_status="now",
        )
        self.make = vi.upsert_entity(
            self.conn,
            entity_type="solution",
            canonical_key="url:https://www.make.com/",
            name="Make",
            url="https://www.make.com/",
            attention_status="later",
        )
        vi.add_alias(self.conn, self.notion, "Notion AI")

        self.video = vi.upsert_source(
            self.conn,
            source_url="https://example.test/video/1",
            platform="test",
            source_type="video",
            title="Workflow demo",
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
            claim_text="The source recommends Notion for this workflow.",
            verification_status="asserted",
        )
        vi.record_claim(
            self.conn,
            entity_id=self.notion,
            source_id=self.video,
            claim_text="Notion has an official help center.",
            verification_status="verified",
            verification_source_id=self.official,
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

    def test_list_entities_filters_and_searches_aliases(self):
        result = iq.list_entities(
            self.conn,
            q="Notion AI",
            attention_status="now",
        )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["id"], self.notion)
        self.assertEqual(result["items"][0]["source_count"], 2)
        self.assertEqual(result["items"][0]["claim_count"], 2)
        self.assertEqual(result["items"][0]["verified_claim_count"], 1)
        self.assertEqual(result["items"][0]["relation_count"], 1)

    def test_get_entity_keeps_evidence_and_verification_distinct(self):
        item = iq.get_entity(self.conn, self.notion)

        self.assertEqual(item["name"], "Notion")
        self.assertIn("Notion AI", item["aliases"])
        self.assertEqual(item["epistemic"]["source_count"], 2)
        self.assertEqual(
            item["epistemic"]["verification_counts"]["asserted"],
            1,
        )
        self.assertEqual(
            item["epistemic"]["verification_counts"]["verified"],
            1,
        )

        verified = next(
            claim
            for claim in item["claims"]
            if claim["verification_status"] == "verified"
        )
        self.assertEqual(
            verified["verification_source_url"],
            "https://www.notion.so/help",
        )

        relation = item["relations"][0]
        self.assertEqual(relation["direction"], "outgoing")
        self.assertEqual(relation["predicate"], "integrates_with")
        self.assertEqual(relation["related_entity_name"], "Make")
        self.assertEqual(
            relation["source_url"],
            "https://example.test/video/1",
        )

    def test_overview_exposes_attention_and_epistemic_counts(self):
        overview = iq.overview(self.conn)

        self.assertEqual(overview["entities"], 2)
        self.assertEqual(overview["sources"], 2)
        self.assertEqual(overview["claims"], 2)
        self.assertEqual(overview["relations"], 1)
        self.assertEqual(overview["attention"]["now"], 1)
        self.assertEqual(overview["attention"]["later"], 1)
        self.assertEqual(overview["verification"]["asserted"], 1)
        self.assertEqual(overview["verification"]["verified"], 1)

    def test_invalid_attention_status_is_rejected(self):
        with self.assertRaises(iq.IntelligenceQueryError):
            iq.list_entities(self.conn, attention_status="to_test")


if __name__ == "__main__":
    unittest.main()
