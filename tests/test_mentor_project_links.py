import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import hashlib
import io
import sqlite3
import unittest
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import mentor_proactive as proactive
import mentor_project_links as links
import vault_intelligence as intelligence

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
STAMP = "2026-09-29T12:00:00+00:00"

BINDINGS = """
CREATE TABLE IF NOT EXISTS intelligence_entity_bindings (
    entity_id INTEGER PRIMARY KEY REFERENCES intelligence_entities(id) ON DELETE CASCADE,
    parent_entity_id INTEGER REFERENCES intelligence_entities(id) ON DELETE SET NULL,
    notion_page_id TEXT, notion_url TEXT, drive_folder_id TEXT, drive_url TEXT,
    canonical_path TEXT NOT NULL, synced_at TEXT NOT NULL
)
"""

PROJECTS = (
    ("activity", "activity:nexdoor_partners", "Nexdoor Partners", None),
    ("activity", "activity:nexdoor_talent", "Nexdoor Talent", None),
    ("activity", "activity:recherche_emploi_remote", "Recherche emploi remote", None),
    ("vertical", "vertical:nexdoor_partners:logistique", "Verticale Logistique",
     "activity:nexdoor_partners"),
    ("client", "client:nexdoor_partners:logistique:halidi_transport_international",
     "Halidi Transport International", "vertical:nexdoor_partners:logistique"),
)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Vault:
    """Petit Vault fictif avec les projets du workspace."""

    def __init__(self):
        self.path = _isolation.TMP / f"links-{uuid.uuid4().hex[:8]}.db"
        self.conn = intelligence.connect(str(self.path))
        self.conn.execute(BINDINGS)
        self.ids = {}
        for entity_type, key, name, parent in PROJECTS:
            entity_id = intelligence.upsert_entity(
                self.conn, entity_type=entity_type, canonical_key=key, name=name)
            self.ids[key] = entity_id
            self.conn.execute(
                "INSERT INTO intelligence_entity_bindings (entity_id, parent_entity_id, "
                "canonical_path, synced_at) VALUES (?, ?, ?, ?)",
                (entity_id, self.ids.get(parent), name, STAMP),
            )
        self.conn.commit()
        self.count = 0

    def project(self, suffix):
        return next(v for k, v in self.ids.items() if k.endswith(suffix))

    def source(self, title, text="", *, category=None, tags=(), author="auteur"):
        self.count += 1
        url = f"https://example.invalid/links/{self.count}"
        self.conn.execute(
            "INSERT INTO contents (source_url, title, description_original, category, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (url, title, text, category, STAMP, STAMP),
        )
        content_id = self.conn.execute("SELECT MAX(id) FROM contents").fetchone()[0]
        for tag in tags:
            self.conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
            tag_id = self.conn.execute("SELECT id FROM tags WHERE name = ?", (tag,)).fetchone()[0]
            self.conn.execute("INSERT INTO content_tags VALUES (?, ?)", (content_id, tag_id))
        source_id = intelligence.upsert_source(
            self.conn, source_url=url, title=title, author=author, content_id=content_id,
            captured_at="2026-09-28",
        )
        self.conn.commit()
        return source_id

    def candidates(self, source_id):
        analysis = links.analyze(self.conn)
        result = next(r for r in analysis["results"] if r["source"]["id"] == source_id)
        return {c["project"]: c for c in result["candidates"]}


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()
        self.vault = Vault()

    def tearDown(self):
        self.vault.conn.close()

    def test_explicit_project_name_is_high_and_explained(self):
        sid = self.vault.source("Point hebdo Nexdoor Talent : sourcing de profils data")
        cand = self.vault.candidates(sid)["Nexdoor Talent"]
        self.assertEqual(cand["level"], "high")
        self.assertGreaterEqual(cand["confidence"], 0.85)
        explanation = "\n".join(links.explain(cand))
        self.assertIn("mention explicite 'nexdoor talent'", explanation)
        self.assertIn("title", explanation)

    def test_generic_tool_gives_no_project(self):
        sid = self.vault.source("ChatGPT : 10 prompts pour gagner du temps",
                                "Automatisation avec ChatGPT et Claude, workflow IA.")
        self.assertEqual(self.vault.candidates(sid), {})

    def test_automation_and_prospecting_are_not_enough(self):
        sid = self.vault.source("Automatiser la prospection LinkedIn",
                                "automation, prospection commerciale sur LinkedIn",
                                tags=("prospection", "automatisation"))
        levels = {c["level"] for c in self.vault.candidates(sid).values()}
        self.assertFalse(levels & {"high", "medium"})

    def test_single_keyword_is_low(self):
        sid = self.vault.source("Comment recruter un freelance")
        cand = self.vault.candidates(sid)["Nexdoor Talent"]
        self.assertEqual(cand["level"], "low")
        self.assertIn("simple mot-cle", cand["reason"])

    def test_synonyms_of_one_idea_do_not_add_up(self):
        sid = self.vault.source("Recrutement : le hiring change",
                                "recrutement, recruteurs, hiring, embauche")
        cand = self.vault.candidates(sid)["Nexdoor Talent"]
        self.assertEqual(cand["level"], "low")
        self.assertEqual(sum(1 for s in cand["signals"] if s["kind"] == "core"), 1)

    def test_talent_theme_with_commercial_context_is_medium(self):
        sid = self.vault.source(
            "Sourcing de candidats pour un cabinet de recrutement",
            "Methode de sourcing et de recrutement, avec un angle commercial.",
        )
        cand = self.vault.candidates(sid)["Nexdoor Talent"]
        self.assertEqual(cand["level"], "medium")
        kinds = [s["kind"] for s in cand["signals"]]
        self.assertGreaterEqual(kinds.count("core"), 2)
        self.assertIn("context", kinds)

    def test_named_client_wins_over_its_vertical(self):
        sid = self.vault.source(
            "Halidi : optimiser l'import/export",
            "Halidi Transport veut automatiser sa logistique et le suivi transport.",
        )
        found = self.vault.candidates(sid)
        self.assertEqual(found["Halidi Transport International"]["level"], "high")
        self.assertEqual(found["Verticale Logistique"]["level"], "low")
        self.assertIn("plus precis", found["Verticale Logistique"]["reason"])

    def test_logistics_theme_without_client_name_never_targets_client(self):
        sid = self.vault.source("Logistique : reduire le cout du transport",
                                "logistique, transporteurs, douane et livraison")
        found = self.vault.candidates(sid)
        self.assertEqual(found["Verticale Logistique"]["level"], "medium")
        self.assertNotIn("Halidi Transport International", found)

    def test_shared_alias_is_ambiguous(self):
        sid = self.vault.source("Nexdoor : bilan du trimestre")
        found = self.vault.candidates(sid)
        self.assertEqual(len(found), 1)
        cand = next(iter(found.values()))
        self.assertEqual(cand["level"], "ambiguous")
        self.assertIn("Nexdoor Partners", cand["project"])
        self.assertIn("Nexdoor Talent", cand["project"])

    def test_two_projects_equally_plausible_are_ambiguous(self):
        sid = self.vault.source(
            "CV et entretien : recrutement de candidats",
            "offre d'emploi en full remote, recrutement de candidats, CV, entretien",
        )
        found = self.vault.candidates(sid)
        self.assertEqual(found["Nexdoor Talent"]["level"], "ambiguous")
        self.assertEqual(found["Recherche emploi remote"]["level"], "ambiguous")

    def test_sensitive_source_is_never_reviewable_without_name(self):
        sid = self.vault.source("TDAH : sourcing de candidats et recrutement",
                                "traitement medical, recrutement de candidats, commercial",
                                category="Santé & bien-être")
        found = self.vault.candidates(sid)
        self.assertEqual(found["Nexdoor Talent"]["level"], "low")
        self.assertIn("sensible", found["Nexdoor Talent"]["reason"])
        for cand in found.values():
            self.assertTrue(cand["signals"])  # jamais de proposition vide

    def test_second_hand_ai_summary_weighs_less(self):
        sid = self.vault.source("Une video", "rien de specifique")
        self.vault.conn.execute(
            "UPDATE contents SET description_ai = 'recrutement de candidats, sourcing' "
            "WHERE id = (SELECT content_id FROM intelligence_sources WHERE id = ?)", (sid,))
        self.vault.conn.commit()
        cand = self.vault.candidates(sid)["Nexdoor Talent"]
        self.assertTrue(all(s["kind"] == "core_ai_only" for s in cand["signals"]))
        self.assertEqual(cand["level"], "low")

    def test_analysis_is_read_only_and_offline(self):
        self.vault.source("Nexdoor Talent : sourcing")
        before = self.vault.conn.total_changes
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("reseau")) as net:
            links.analyze(self.vault.conn)
        net.assert_not_called()
        self.assertEqual(self.vault.conn.total_changes, before)


class StorageAndReviewTests(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()
        self.vault = Vault()
        self.talent = self.vault.project("nexdoor_talent")
        self.named = self.vault.source("Nexdoor Talent : process de sourcing")
        self.low = self.vault.source("Comment recruter un freelance")

    def tearDown(self):
        self.vault.conn.close()

    def _proposal(self, source_id, entity_id):
        row = self.vault.conn.execute(
            "SELECT proposal_id FROM mentor_project_link_proposals WHERE source_id = ? "
            "AND entity_id = ? AND status = 'proposed'", (source_id, entity_id)).fetchone()
        return row[0]

    def test_proposals_are_stored_without_duplicates(self):
        conn = self.vault.conn
        first = links.record_proposals(conn, links.analyze(conn), at=STAMP)
        second = links.record_proposals(conn, links.analyze(conn), at=STAMP)
        self.assertGreaterEqual(first["created"], 2)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["unchanged"], first["created"])
        total = conn.execute("SELECT COUNT(*) FROM mentor_project_link_proposals").fetchone()[0]
        self.assertEqual(total, first["created"])
        row = links.get_proposal(conn, "PL-0001")
        for field in ("proposal_id", "source_id", "entity_id", "confidence",
                      "confidence_level", "reason", "evidence_json", "status",
                      "created_at", "reviewed_at"):
            self.assertIn(field, row)
        self.assertEqual(row["status"], "proposed")
        self.assertTrue(row["evidence"]["explanation"])
        with self.assertRaises(sqlite3.IntegrityError):  # une seule ouverte par paire
            conn.execute(
                "INSERT INTO mentor_project_link_proposals (proposal_id, source_id, entity_id, "
                "confidence, confidence_level, reason, rules_version, fingerprint, created_at, "
                "updated_at) SELECT 'PL-9999', source_id, entity_id, 0.5, 'medium', 'x', 'v', "
                "'autre', created_at, updated_at FROM mentor_project_link_proposals "
                "WHERE proposal_id = 'PL-0001'")

    def test_changed_evidence_supersedes_open_proposal(self):
        conn = self.vault.conn
        links.record_proposals(conn, links.analyze(conn), at=STAMP)
        old = self._proposal(self.low, self.talent)
        conn.execute("UPDATE contents SET description_original = 'sourcing et chasse de tetes, "
                     "angle commercial' WHERE id = (SELECT content_id FROM intelligence_sources "
                     "WHERE id = ?)", (self.low,))
        conn.commit()
        counts = links.record_proposals(conn, links.analyze(conn), at=STAMP)
        self.assertEqual(counts["superseded"], 1)
        self.assertEqual(links.get_proposal(conn, old)["status"], "superseded")
        new = links.get_proposal(conn, self._proposal(self.low, self.talent))
        self.assertEqual(new["confidence_level"], "medium")

    def test_approval_applies_real_link_and_is_never_reproposed(self):
        conn = self.vault.conn
        links.record_proposals(conn, links.analyze(conn), at=STAMP)
        proposal_id = self._proposal(self.named, self.talent)
        self.assertFalse(proactive.VaultView(conn).projects_for_source(self.named))
        approved = links.approve(conn, proposal_id, at=STAMP)
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["reviewed_at"], STAMP)
        role, notes = conn.execute(
            "SELECT role, notes FROM intelligence_entity_sources WHERE entity_id = ? "
            "AND source_id = ?", (self.talent, self.named)).fetchone()
        self.assertEqual(role, "relevant_to")
        self.assertIn(proposal_id, notes)
        self.assertEqual(proactive.VaultView(conn).projects_for_source(self.named), [self.talent])
        links.record_proposals(conn, links.analyze(conn), at=STAMP)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM mentor_project_link_proposals WHERE source_id = ? "
            "AND entity_id = ?", (self.named, self.talent)).fetchone()[0], 1)

    def test_low_is_never_applied_and_rejection_is_final(self):
        conn = self.vault.conn
        links.record_proposals(conn, links.analyze(conn), at=STAMP)
        low_id = self._proposal(self.low, self.talent)
        with self.assertRaises(links.ProjectLinkError):
            links.approve(conn, low_id)
        rejected = links.reject(conn, low_id, note="hors sujet", at=STAMP)
        self.assertEqual((rejected["status"], rejected["review_note"]), ("rejected", "hors sujet"))
        counts = links.record_proposals(conn, links.analyze(conn), at=STAMP)
        self.assertEqual(counts["created"], 0)
        self.assertFalse(conn.execute(
            "SELECT 1 FROM intelligence_entity_sources WHERE source_id = ?", (self.low,)
        ).fetchone())

    def test_review_cli_approve_reject_skip(self):
        conn = self.vault.conn
        other = self.vault.source("Logistique : reduire le cout du transport",
                                  "logistique, transporteurs, douane et livraison")
        third = self.vault.source("Sourcing de candidats pour un cabinet de recrutement",
                                  "sourcing et recrutement, angle commercial")
        links.record_proposals(conn, links.analyze(conn), at=STAMP)
        queue = links.reviewable(conn)
        self.assertEqual({p["confidence_level"] for p in queue} - {"high", "medium"}, set())
        answers = iter(["1", "2", "raison test", "3"])
        decided = links.review(conn, input_func=lambda _prompt: next(answers), out=lambda *_: None)
        self.assertEqual(decided, {"approved": 1, "rejected": 1, "skipped": 1})
        self.assertEqual(queue[0]["confidence_level"], "high")
        self.assertEqual(links.get_proposal(conn, queue[0]["proposal_id"])["status"], "approved")
        self.assertEqual(links.get_proposal(conn, queue[1]["proposal_id"])["review_note"],
                         "raison test")
        self.assertEqual(links.get_proposal(conn, queue[2]["proposal_id"])["status"], "proposed")
        self.assertTrue({other, third})

    def test_approved_links_unlock_project_convergence(self):
        conn = self.vault.conn
        for index, author in enumerate(("a", "b", "c")):
            self.vault.source(f"Nexdoor Talent : retour client {index}", author=author)
        links.record_proposals(conn, links.analyze(conn), at=STAMP)
        for proposal in links.reviewable(conn):
            if proposal["entity_id"] == self.talent:
                links.approve(conn, proposal["proposal_id"], at=STAMP)
        proactive.run_pass(conn, now=NOW)
        convergence = [c for c in proactive.store.list_candidates(conn)
                       if c["candidate_type"] == "convergence" and c["project_id"] == self.talent]
        self.assertEqual(len(convergence), 1)
        self.assertEqual(convergence[0]["status"], "shortlisted")


class CliTests(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()

    def test_dry_run_writes_nothing(self):
        vault = Vault()
        vault.source("Nexdoor Talent : process de sourcing")
        vault.conn.close()
        before = _sha(vault.path)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(links.main(["--db", str(vault.path), "--dry-run"]), 0)
        self.assertEqual(_sha(vault.path), before)
        text = out.getvalue()
        self.assertIn("DRY-RUN, aucune ecriture", text)
        self.assertIn("Projet propose : Nexdoor Talent", text)
        self.assertIn("HIGH 1", text)
        check = sqlite3.connect(vault.path)
        self.assertIsNone(check.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'mentor_project_link_proposals'").fetchone())
        check.close()


if __name__ == "__main__":
    unittest.main()
