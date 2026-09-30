"""
Regressions observees en reel (QA finale) :
A. « Que sais-tu de Nexdoor Talent ? » -> route projet, reponse fondee sur le Vault ;
B. SUG ouverte -> « Growth Hacker, qu'en penses-tu ? » -> contexte = la suggestion ;
C. « Comment exploiter ca pour Nexdoor Talent ? » -> « ca » = suggestion active ;
D. Qwen indisponible -> relances ; « selectionnes » != « consultes » ;
E. aucune source sans rapport injectee dans un agent quand un contexte explicite existe ;
+ vocabulaire interne (« le candidat ») jamais montre a Rosy.
"""

import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import json
import unittest
from unittest import mock

import _life_fixtures as fx

import mentor_council as council
import mentor_proactive as proactive
import mentor_proactive_store as store
import vault_ask
import vault_intelligence as intelligence
from test_telegram_gateway import NOON, GatewayCase, click, msg, real_ask


def _panel_payloads(backend):
    return [json.loads(user) for schema, user in backend.prompts
            if schema is council.PANEL_SCHEMA]


class ProjectRouteTests(GatewayCase):
    def test_a_describe_project_uses_the_project_view(self):
        self.assertEqual(vault_ask.clean_question("Bonjour, que sais-tu de Nexdoor Talent ?"),
                         "que sais-tu de Nexdoor Talent ?")
        result = vault_ask.ask(self.conn, "Bonjour, que sais-tu de Nexdoor Talent ?", llm="off")
        self.assertEqual(result["route"], "project_view")
        self.assertEqual(result["project_id"], self.ids["activity:nexdoor_talent"])
        answer = result["answer"]
        self.assertIn("📁 Nexdoor Talent", answer)
        self.assertIn("Description (profil) : Cabinet de recrutement", answer)
        self.assertIn("Sources rattachees (3)", answer)
        self.assertIn("Sourcing de profils data sur LinkedIn", answer)
        self.assertIn("Uniquement ce qui est enregistre dans ton Vault.", answer)
        self.assertEqual(sorted(s["source_id"] for s in result["sources"]), self.sources)

    def test_a_through_telegram(self):
        self.gw.handle_update(msg("Bonjour, que sais-tu de Nexdoor Talent ?"))
        self.assertIn("📁 Nexdoor Talent", self.last()["text"])

    def test_a_other_questions_keep_classic_retrieval(self):
        self.assertIsNone(vault_ask.project_intent(self.conn,
                                                   "Comment exploiter ca pour Nexdoor Talent ?"))
        self.assertIsNone(vault_ask.project_intent(self.conn, "Que sais-tu sur le sourcing ?"))
        self.assertEqual(vault_ask.project_intent(self.conn, "Nexdoor Talent ?"),
                         self.ids["activity:nexdoor_talent"])


class ActiveContextTests(GatewayCase):
    def setUp(self):
        super().setUp()
        # Source SANS rapport qui matcherait lexicalement une question vague.
        conn = self.conn
        conn.execute("INSERT INTO contents (source_url, title, description_original, created_at, "
                     "updated_at) VALUES ('https://example.invalid/github', 'GitHub repo pour AI "
                     "coders : penses-y', 'Un repo GitHub pour les AI coders, qu en penses-tu', "
                     "?, ?)", (fx.STAMP, fx.STAMP))
        content_id = conn.execute("SELECT MAX(id) FROM contents").fetchone()[0]
        self.github = intelligence.upsert_source(conn, source_url="https://example.invalid/github",
                                                 title="GitHub repo pour AI coders",
                                                 content_id=content_id)
        conn.commit()
        self.backend.prompts = []
        proactive.run_pass(self.conn, now=fx.NOW)
        council.review_shortlist(self.conn, self.backend, now=fx.NOW, limit=5)
        self.sug = next(s for s in store.list_suggestions(self.conn)
                        if s["suggestion_type"] == "opportunity")
        self.gw.tick(NOON, force=True)
        self.push = self.telegram.sent[-1]
        self.asked = []
        original = self.gw.ask_fn

        def spy(conn, question, llm="off"):
            self.asked.append(question)
            return original(conn, question, llm)

        self.gw.ask_fn = spy

    def open_suggestion(self):
        self.gw.handle_update(click(f"s:{self.sug['suggestion_id']}:show", self.push["message_id"]))

    def test_b_growth_hacker_gets_the_open_suggestion(self):
        self.open_suggestion()
        self.backend.prompts.clear()
        self.assertEqual(self.gw.handle_update(msg("Growth Hacker, qu'en penses-tu ?",
                                                   message_id=50)), "answer:A:anchored")
        self.assertEqual(self.asked, [])  # aucune recherche parasite
        payload = _panel_payloads(self.backend)[-1]
        context = payload["contexte_actif"]
        self.assertEqual(context["suggestion"], self.sug["suggestion_id"])
        self.assertEqual(context["projet"], "Nexdoor Talent")
        self.assertTrue(context["hypothese"])
        self.assertTrue(context["preuves"]["source_ids"])
        self.assertEqual(len(context["verdicts_existants"]), 3)
        self.assertEqual([s["agent"] for s in payload["specialistes"]], ["Growth Hacker"])
        text = self.last()["text"]
        self.assertIn(f"📌 Contexte : {self.sug['suggestion_id']}", text)
        self.assertIn("Agents consultes (mode A)", text)

    def test_c_mode_b_stays_anchored_on_the_active_suggestion(self):
        self.open_suggestion()
        self.gw.handle_update(msg("Growth Hacker, qu'en penses-tu ?", message_id=50))
        self.backend.prompts.clear()
        result = self.gw.handle_update(msg("Comment exploiter ca pour Nexdoor Talent ?",
                                           message_id=51))
        self.assertEqual(result, "answer:B:anchored")
        payload = _panel_payloads(self.backend)[-1]
        self.assertEqual(payload["contexte_actif"]["suggestion"], self.sug["suggestion_id"])
        self.assertIn("conseiller du projet Nexdoor Talent", self.last()["text"])
        self.assertEqual(self.asked, [])

    def test_e_no_unrelated_source_reaches_an_agent(self):
        self.gw.handle_update(msg("Tu en penses quoi, qu'en penses-tu ?",
                                  reply_to=self.push["message_id"], message_id=60))
        payload = _panel_payloads(self.backend)[-1]
        refs = [s["ref"] for s in payload["sources"]]
        self.assertTrue(refs)
        self.assertNotIn(f"source:{self.github}", refs)
        allowed = {f"source:{sid}" for sid in self.sug["evidence"]["candidate"]["source_ids"]}
        self.assertTrue(set(refs) <= allowed)

    def test_without_reference_to_context_classic_retrieval_is_used(self):
        self.open_suggestion()
        self.gw.handle_update(msg("Que sais-je sur le sourcing de profils data ?", message_id=70))
        self.assertEqual(len(self.asked), 1)

    def test_reply_to_an_answer_anchors_on_its_sources(self):
        self.gw.handle_update(msg("Que sais-je sur le sourcing de profils data ?", message_id=80))
        answer_id = self.last()["message_id"]
        self.backend.prompts.clear()
        result = self.gw.handle_update(msg("Demande au CFO", reply_to=answer_id, message_id=81))
        self.assertEqual(result, "answer:A:anchored")
        refs = [s["ref"] for s in _panel_payloads(self.backend)[-1]["sources"]]
        self.assertNotIn(f"source:{self.github}", refs)


class QwenReadinessTests(GatewayCase):
    def test_d_selected_but_not_consulted_when_qwen_is_down(self):
        self.backend._available = False
        self.gw.handle_update(msg("Demande au CFO ce qu'il pense du sourcing data"))
        text = self.last()["text"]
        self.assertIn("Agents selectionnes mais non consultes : Qwen local indisponible.", text)
        self.assertNotIn("Agents consultes", text)
        self.assertIn("Chief Financial Officer", text)

    def test_d_readiness_retries_and_starts_local_ollama_once(self):
        backend = council.OllamaBackend()
        sleeps = []
        with mock.patch.object(backend, "available", side_effect=[False, False, True]), \
                mock.patch.object(backend, "_reachable", return_value=False), \
                mock.patch.object(backend, "_start_local_server", return_value=True) as start, \
                mock.patch.object(backend, "warm_up", return_value=True) as warm:
            self.assertTrue(backend.ensure_ready(retries=5, delay=3, sleep=sleeps.append))
        start.assert_called_once()
        warm.assert_called_once()
        self.assertEqual(sleeps, [3, 3])

    def test_d_readiness_gives_up_without_cloud(self):
        import vault_cloud

        backend = council.OllamaBackend()
        with mock.patch.object(backend, "available", return_value=False), \
                mock.patch.object(backend, "_reachable", return_value=False), \
                mock.patch.object(backend, "_start_local_server", return_value=False), \
                mock.patch.object(vault_cloud, "get_provider",
                                  side_effect=AssertionError("cloud")):
            self.assertFalse(backend.ensure_ready(retries=3, delay=0, sleep=lambda _s: None))


class WordingTests(GatewayCase):
    def test_internal_candidate_wording_never_reaches_rosy(self):
        self.assertEqual(council.user_wording("Le candidat a identifié un risque."),
                         "Une source de ton Vault a identifié un risque.")
        # « candidats » au sens recrutement est conserve.
        self.assertEqual(council.user_wording("Qualifier les candidats data."),
                         "Qualifier les candidats data.")

    def test_generated_suggestion_is_cleaned(self):
        class Wordy(fx.FakeBackend):
            def chat_json(self, system, user, schema, *, num_predict=260):
                result = super().chat_json(system, user, schema, num_predict=num_predict)
                if schema is council.HYPOTHESIS_SCHEMA:
                    result["summary"] = "Le candidat présente un risque pour le sourcing."
                return result

        proactive.run_pass(self.conn, now=fx.NOW)
        council.review_shortlist(self.conn, Wordy(), now=fx.NOW, limit=1)
        suggestion = store.list_suggestions(self.conn)[0]
        self.assertTrue(suggestion["summary"].startswith("Une source de ton Vault présente"))


if __name__ == "__main__":
    unittest.main()
