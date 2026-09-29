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
import mentor_project_profiles as profiles
import setup_project_profiles
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
    ("activity", "activity:side_businesses", "Side businesses / opportunités", None),
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
        setup_project_profiles.seed(self.conn)
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
                                "Des astuces ChatGPT, Claude et IA au quotidien.")
        self.assertEqual(self.vault.candidates(sid), {})

    def test_generic_automation_is_never_reviewable(self):
        sid = self.vault.source("ChatGPT : 10 prompts pour gagner du temps",
                                "Automatisation avec ChatGPT et Claude, workflow IA.")
        levels = {c["level"] for c in self.vault.candidates(sid).values()}
        self.assertFalse(levels & {"high", "medium"})

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

    def test_named_client_wins_over_its_vertical(self):
        sid = self.vault.source(
            "Halidi : optimiser l'import/export",
            "Halidi Transport veut automatiser sa logistique et ses devis.",
        )
        found = self.vault.candidates(sid)
        self.assertEqual(found["Halidi Transport International"]["level"], "high")
        self.assertEqual(found["Verticale Logistique"]["level"], "low")
        self.assertIn("plus precis", found["Verticale Logistique"]["reason"])

    def test_logistics_theme_without_client_name_never_targets_client(self):
        sid = self.vault.source("Logistique : reduire le cout du transport",
                                "logistique, douane, fret et devis clients")
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
            "candidature via job board en full remote, recrutement de candidats, CV, entretien",
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
                                  "logistique, douane, fret et devis clients")
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


class ProfileTests(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()
        self.vault = Vault()
        self.conn = self.vault.conn
        self.talent = self.vault.project("nexdoor_talent")

    def tearDown(self):
        self.conn.close()

    def test_profiles_live_in_the_vault_and_reference_existing_projects(self):
        stored = profiles.get_profile(self.conn, self.talent)
        self.assertEqual(stored["project_name"], "Nexdoor Talent")
        self.assertEqual(stored["profile_status"], "complete")
        self.assertEqual(stored["named_entities"], ["Nexdoor Talent"])
        self.assertIn("recrutement", [t["topic"] for t in stored["core_topics"]])
        self.assertEqual([t["topic"] for t in stored["specific_tools"]], ["Manatal"])
        with self.assertRaises(profiles.ProfileError):
            profiles.upsert_profile(self.conn, 99999, {"description": "x"})
        self.assertFalse(hasattr(links, "PROJECT_PROFILES"))

    def test_seed_is_idempotent_and_never_overwrites_edits(self):
        profiles.upsert_profile(self.conn, self.talent, {
            "description": "Edite par Rosy", "core_topics": ["recrutement"],
            "profile_source": "rosy-edit"})
        result = setup_project_profiles.seed(self.conn)
        self.assertEqual(result["activity:nexdoor_talent"], "kept")
        self.assertEqual(profiles.get_profile(self.conn, self.talent)["description"],
                         "Edite par Rosy")

    def test_editing_a_profile_changes_results_without_code(self):
        sid = self.vault.source("Onboarding et recrutement", "onboarding des recrues, ATS")
        self.assertEqual(self.vault.candidates(sid)["Nexdoor Talent"]["level"], "low")
        stored = profiles.get_profile(self.conn, self.talent)
        stored["core_topics"].append({"topic": "onboarding", "terms": ["onboarding"]})
        profiles.upsert_profile(self.conn, self.talent, stored)
        self.assertEqual(self.vault.candidates(sid)["Nexdoor Talent"]["level"], "medium")

    def test_export_import_roundtrip(self):
        exported = profiles.export_profiles(self.conn)
        exported[0]["description"] = "modifie"
        profiles.import_profiles(self.conn, exported)
        self.assertEqual(
            profiles.get_profile(self.conn, exported[0]["entity_id"])["description"], "modifie")

    def test_without_profile_only_the_exact_name_counts(self):
        self.conn.execute("DELETE FROM mentor_project_profiles")
        self.conn.commit()
        named = self.vault.source("Nexdoor Talent : bilan")
        themed = self.vault.source("Sourcing de candidats et recrutement", "angle commercial")
        self.assertEqual(self.vault.candidates(named)["Nexdoor Talent"]["level"], "high")
        self.assertEqual(self.vault.candidates(themed), {})

    def test_weak_terms_never_count_alone(self):
        sid = self.vault.source("Serveur MCP remote", "remote, freelance, certification, job")
        self.assertNotIn("Recherche emploi remote", self.vault.candidates(sid))

    def test_ambiguous_tool_name_make_is_not_matched(self):
        sid = self.vault.source("How to make money with Zapier workflows")
        cand = self.vault.candidates(sid)["Nexdoor Partners"]
        terms = [str(s.get("term")).lower() for s in cand["signals"]]
        self.assertIn("zapier", terms)
        self.assertNotIn("make", terms)
        self.assertNotIn(cand["level"], ("high", "medium"))

    def test_personal_health_block_on_neuropsychology(self):
        parent = self.vault.project("nexdoor_partners")
        neuro = intelligence.upsert_entity(
            self.conn, entity_type="vertical",
            canonical_key="vertical:nexdoor_partners:neuropsychologues",
            name="Verticale Neuropsychologues")
        self.conn.execute(
            "INSERT INTO intelligence_entity_bindings (entity_id, parent_entity_id, "
            "canonical_path, synced_at) VALUES (?, ?, 'x', ?)", (neuro, parent, STAMP))
        self.conn.commit()
        setup_project_profiles.seed(self.conn)
        business = self.vault.source(
            "Atelier TDAH : formulaire d'orientation pour cabinet de neuropsychologue",
            "prise de rendez-vous et agenda du cabinet")
        personal = self.vault.source(
            "Mon TDAH : nouveau traitement en cabinet de neuropsychologue",
            "bilan neuropsychologique, medicament, agenda")
        self.assertEqual(
            self.vault.candidates(business)["Verticale Neuropsychologues"]["level"], "medium")
        cand = self.vault.candidates(personal)["Verticale Neuropsychologues"]
        self.assertEqual(cand["level"], "low")
        self.assertIn("anti-signal", cand["reason"])


class DecisionTests(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()
        self.vault = Vault()
        self.conn = self.vault.conn
        self.talent = self.vault.project("nexdoor_talent")
        self.remote = self.vault.project("recherche_emploi_remote")
        self.cv = self.vault.source("Prompt injections cachees dans des CV")
        self.aws = self.vault.source("Se lancer sur AWS")

    def tearDown(self):
        self.conn.close()

    def test_recorded_decisions_apply_only_the_approval(self):
        approved = links.record_decision(
            self.conn, source_id=self.cv, entity_id=self.talent, decision="approve",
            level="medium", confidence=0.62, reason="dry-run v1",
            explanation=["recrutement dans le titre"], rules_version="project-links-v1",
            note="decision Rosy", at=STAMP)
        rejected = links.record_decision(
            self.conn, source_id=self.aws, entity_id=self.remote, decision="reject",
            level="medium", confidence=0.60, reason="dry-run v1",
            rules_version="project-links-v1", note="decision Rosy", at=STAMP)
        self.assertEqual((approved["status"], rejected["status"]), ("approved", "rejected"))
        self.assertEqual(approved["rules_version"], "project-links-v1")
        rows = self.conn.execute(
            "SELECT entity_id, source_id, role FROM intelligence_entity_sources "
            "WHERE role = 'relevant_to'").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(self.talent, self.cv, "relevant_to")])
        again = links.record_decision(
            self.conn, source_id=self.aws, entity_id=self.remote, decision="approve",
            level="medium", confidence=0.6, reason="x")
        self.assertEqual(again["status"], "rejected")  # une decision n'est pas rejouee
        links.record_proposals(self.conn, links.analyze(self.conn), at=STAMP)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM mentor_project_link_proposals WHERE source_id = ? "
            "AND entity_id = ?", (self.aws, self.remote)).fetchone()[0], 1)
        with self.assertRaises(links.ProjectLinkError):
            links.record_decision(self.conn, source_id=9999, entity_id=self.talent,
                                  decision="approve", level="medium", confidence=0.6,
                                  reason="x")


class CompoundRuleAndSideBusinessTests(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()
        self.vault = Vault()
        self.conn = self.vault.conn

    def tearDown(self):
        self.conn.close()

    def test_partners_compound_rule_gives_medium_with_two_business_contexts(self):
        sid = self.vault.source(
            "Automatiser la generation de leads",
            "Un workflow qui qualifie les leads, remplit le CRM et envoie les emails.")
        cand = self.vault.candidates(sid)["Nexdoor Partners"]
        self.assertEqual(cand["level"], "medium")
        self.assertIn("regle composee 'automatisation metier'", cand["reason"])
        rule = [s for s in cand["signals"] if s["kind"] == "compound_rule"][0]
        self.assertGreaterEqual(len(rule["contexts"]), 2)
        self.assertIn("regle composee", "\n".join(links.explain(cand)))

    def test_compound_rule_needs_two_distinct_contexts(self):
        sid = self.vault.source("Automatiser ses emails", "automation des emails")
        cand = self.vault.candidates(sid)["Nexdoor Partners"]
        self.assertEqual(cand["level"], "low")

    def test_compound_rule_lives_in_profile_data(self):
        partners = self.vault.project("nexdoor_partners")
        stored = profiles.get_profile(self.conn, partners)
        self.assertEqual(stored["compound_rules"][0]["name"], "automatisation metier")
        sid = self.vault.source("Automatiser ses relances",
                                "relance automatique des leads par emails")
        self.assertEqual(self.vault.candidates(sid)["Nexdoor Partners"]["level"], "medium")
        stored["compound_rules"] = []
        profiles.upsert_profile(self.conn, partners, stored)
        self.assertNotEqual(self.vault.candidates(sid)["Nexdoor Partners"]["level"], "medium")

    def test_side_business_needs_its_own_topics_and_is_never_a_fallback(self):
        unclassified = self.vault.source("Une video sans rapport", "recette de cuisine")
        self.assertEqual(self.vault.candidates(unclassified), {})
        sid = self.vault.source(
            "Lancer un micro-SaaS en 30 jours",
            "Trouver une niche, valider le marche avec un MVP, pricing et business model.")
        cand = self.vault.candidates(sid)["Side businesses / opportunités"]
        self.assertIn(cand["level"], ("medium", "high"))

    def test_side_business_defers_to_partners(self):
        sid = self.vault.source(
            "Nouveau service : automatiser la generation de leads",
            "business model du nouveau service : workflow, leads, CRM, emails, pricing")
        found = self.vault.candidates(sid)
        self.assertEqual(found["Nexdoor Partners"]["level"], "medium")
        self.assertEqual(found["Side businesses / opportunités"]["level"], "low")
        self.assertIn("s'efface", found["Side businesses / opportunités"]["reason"])

    def test_personal_development_is_blocked_for_side_business(self):
        sid = self.vault.source("Motivation : lancer un side hustle",
                                "developpement personnel, monetisation, niche, MVP")
        cand = self.vault.candidates(sid)["Side businesses / opportunités"]
        self.assertEqual(cand["level"], "low")
        self.assertIn("anti-signal", cand["reason"])

    def test_old_profile_table_is_migrated_without_loss(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE intelligence_entities (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO intelligence_entities VALUES (1, 'P')")
        conn.execute(
            "CREATE TABLE mentor_project_profiles (entity_id INTEGER PRIMARY KEY, "
            "project_name TEXT NOT NULL, description TEXT, core_topics TEXT NOT NULL DEFAULT '[]', "
            "context_topics TEXT NOT NULL DEFAULT '[]', named_entities TEXT NOT NULL DEFAULT '[]', "
            "specific_tools TEXT NOT NULL DEFAULT '[]', excluded_topics TEXT NOT NULL DEFAULT '[]', "
            "profile_status TEXT NOT NULL DEFAULT 'incomplete', profile_source TEXT NOT NULL, "
            "notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        conn.execute("INSERT INTO mentor_project_profiles (entity_id, project_name, description, "
                     "profile_source, created_at, updated_at) VALUES (1, 'P', 'garde', 'x', 'a', 'a')")
        self.assertEqual(profiles.get_profile(conn, 1)["advisors"], [])  # lecture avant migration
        profiles.ensure_schema(conn)
        profiles.ensure_schema(conn)
        item = profiles.get_profile(conn, 1)
        self.assertEqual((item["description"], item["active"], item["compound_rules"]),
                         ("garde", 1, []))
        conn.close()


if __name__ == "__main__":
    unittest.main()
