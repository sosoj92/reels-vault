import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import json
import time
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import mentor_review
import mentor_worker
import vault_ask
import vault_cloud
from _v2_fixtures import add_source, make_db

# Valeur factice de test, jamais une vraie cle.
FAKE_KEY = "sk-ant-FAKE-TEST-VALUE-0000000000"


class FakeMessages:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kwargs):
        self.owner.calls.append(kwargs)
        if self.owner.error:
            raise RuntimeError(self.owner.error)
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=self.owner.reply)],
            stop_reason=self.owner.stop_reason,
            model=kwargs["model"],
            _request_id="req_test",
        )


class FakeClient:
    """Imite client.beta.messages.create du SDK anthropic."""

    def __init__(self, reply="{}", stop_reason="end_turn", error=None):
        self.reply = reply
        self.stop_reason = stop_reason
        self.error = error
        self.calls = []
        self.factory_args = []
        self.beta = SimpleNamespace(messages=FakeMessages(self))

    def factory(self, **kwargs):
        self.factory_args.append(kwargs)
        return self


class CloudAbsentTests(unittest.TestCase):
    """V2-8 : aucun provider cloud -> tout continue de fonctionner."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""})
        self.env.start()
        self.conn = make_db()
        mentor_worker.ensure_schema(self.conn)
        mentor_review.ensure_schema(self.conn)
        self.source = add_source(self.conn, "https://example.test/rtk", "RTK économise des tokens",
                                 transcript="RTK réduit la consommation de tokens de 90 %.")

    def tearDown(self):
        self.conn.close()
        self.env.stop()

    def test_no_provider_is_reported_without_error(self):
        self.assertIsNone(vault_cloud.get_provider())
        status = vault_cloud.status()["claude"]
        self.assertFalse(status["available"])
        self.assertIn("ANTHROPIC_API_KEY", status["reason"])

    def test_ask_still_answers_when_cloud_synthesizer_requested(self):
        with mock.patch.dict(os.environ, {"VAULT_ASK_SYNTHESIZER": "cloud"}):
            result = vault_ask.ask(self.conn, "RTK et les tokens", semantic="off")
        self.assertIn("RTK", result["answer"])
        self.assertIn("https://example.test/rtk", result["answer"])
        self.assertEqual(result["llm"]["status"], "unavailable")
        self.assertFalse(result["llm"]["used"])

    def test_review_queue_waits_untouched(self):
        mentor_review.enqueue(
            self.conn, {"source_id": self.source, "content_id": self.source},
            {"analysis_model": "m", "score": 0.5, "final_confidence": 0.5, "agency_agents": []})
        result = mentor_review.process_queue(self.conn)
        self.assertEqual(result["status"], "no_cloud_provider")
        self.assertEqual(result["pending"], 1)
        status = self.conn.execute(
            "SELECT status, cloud_decision FROM intelligence_mentor_review_queue").fetchone()
        self.assertEqual(tuple(status), ("review_required", None))


class LocalSynthesisDeadlineTests(unittest.TestCase):
    """La connexion Ollama doit se fermer a l'echeance (sinon Ollama continue
    de generer et sature le CPU : requete suivante 44,6 s au lieu de 2,7 s)."""

    def test_socket_timeout_matches_the_deadline(self):
        seen = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"message": {"content": "Synthèse [1]."}}).encode()

        def fake_urlopen(request, timeout=None):
            seen["timeout"] = timeout
            return Response()

        sources = [{"title": "RTK", "snippet": "RTK réduit les tokens.", "url": "u"}]
        with mock.patch.object(vault_ask, "_ollama_available", return_value=True), \
                mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            text, info = vault_ask.synthesize_local("RTK ?", sources, [], 3.0)
        self.assertEqual(info["status"], "ok")
        self.assertIsNotNone(seen.get("timeout"))
        self.assertLessEqual(seen["timeout"], 3.0)


class ColdModelTests(unittest.TestCase):
    """Modele qwen installe mais non charge : pas d'attente, prechauffage."""

    def test_cold_model_answers_immediately_and_warms_up(self):
        warmups = []
        sources = [{"title": "RTK", "snippet": "RTK réduit les tokens.", "url": "u"}]
        with mock.patch.object(vault_ask, "_ollama_available", return_value=False), \
                mock.patch.object(vault_ask, "_ollama_installed", return_value=True), \
                mock.patch.object(vault_ask, "_warm_up_async",
                                  side_effect=lambda: warmups.append(1) or True), \
                mock.patch.object(vault_ask, "_ollama_chat",
                                  side_effect=AssertionError("ne doit pas attendre qwen")):
            started = time.perf_counter()
            text, info = vault_ask.synthesize_local("RTK ?", sources, [], 25.0)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertIsNone(text)
        self.assertEqual(info["status"], "skipped_model_cold")
        self.assertTrue(info["warming_up"])
        self.assertEqual(warmups, [1])

    def test_ollama_down_is_unavailable_without_warmup(self):
        with mock.patch.object(vault_ask, "_ollama_available", return_value=False), \
                mock.patch.object(vault_ask, "_ollama_installed", return_value=False), \
                mock.patch.object(vault_ask, "_warm_up_async",
                                  side_effect=AssertionError("pas de prechauffage")):
            text, info = vault_ask.synthesize_local("RTK ?", [], [], 25.0)
        self.assertEqual(info["status"], "unavailable")

    def test_warmup_can_be_disabled(self):
        with mock.patch.dict(os.environ, {"VAULT_ASK_WARMUP": "0"}):
            self.assertFalse(vault_ask._warm_up_async())


class CloudAdapterMockTests(unittest.TestCase):
    """V2-9 : adapter mocke, review traitee sans exposer de secret."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": FAKE_KEY})
        self.env.start()
        self.conn = make_db()
        mentor_worker.ensure_schema(self.conn)
        mentor_review.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.env.stop()

    def provider(self, client):
        return vault_cloud.ClaudeProvider(client_factory=client.factory)

    def enqueue(self, source_id):
        mentor_review.enqueue(
            self.conn, {"source_id": source_id, "content_id": source_id},
            {"analysis_model": "qwen3:1.7b+agency x2", "score": 0.58,
             "final_confidence": 0.6, "agency_agents": ["Tool Evaluator"]},
            reason="agency_disagreement:classification_disagreement")
        self.conn.commit()

    def test_request_shape_follows_claude_api(self):
        client = FakeClient(reply="Synthèse [1].")
        response = self.provider(client).complete("sys", "user", max_tokens=512, timeout=12)
        call = client.calls[0]
        self.assertEqual(call["model"], "claude-opus-5")
        self.assertEqual(call["thinking"], {"type": "adaptive"})
        self.assertEqual(call["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", call["betas"])
        self.assertEqual(call["messages"], [{"role": "user", "content": "user"}])
        self.assertEqual(response.text, "Synthèse [1].")
        self.assertEqual(client.factory_args[0]["timeout"], 12)

    def test_refusal_is_an_error_not_a_result(self):
        client = FakeClient(reply="", stop_reason="refusal")
        with self.assertRaises(vault_cloud.CloudRefusal):
            self.provider(client).complete("s", "u")

    def test_errors_never_leak_the_key(self):
        client = FakeClient(error=f"401 invalid x-api-key: {FAKE_KEY}")
        with self.assertRaises(vault_cloud.CloudError) as ctx:
            self.provider(client).complete("s", "u")
        self.assertNotIn(FAKE_KEY, str(ctx.exception))
        self.assertNotIn("sk-ant-", str(ctx.exception))

    def test_cloud_approval_creates_insight_without_secret(self):
        source = add_source(self.conn, "https://example.test/tool", "Un outil d'automatisation",
                            transcript="Cet outil automatise la prospection LinkedIn.")
        self.enqueue(source)
        client = FakeClient(reply=json.dumps({
            "decision": "publish", "classification": "opportunity",
            "relevance": 0.8, "confidence": 0.85, "rationale": "Outil directement utile."}))
        result = mentor_review.process_queue(self.conn, self.provider(client))
        self.assertEqual(result["processed"][0]["status"], "cloud_approved")
        row = self.conn.execute(
            "SELECT status, cloud_provider, cloud_model, cloud_decision "
            "FROM intelligence_mentor_review_queue").fetchone()
        self.assertEqual(tuple(row)[:3], ("cloud_approved", "claude", "claude-opus-5"))
        insight = self.conn.execute(
            "SELECT insight_type, analysis_model, status FROM intelligence_mentor_insights_local"
        ).fetchone()
        self.assertEqual(tuple(insight), ("opportunity", "cloud:claude:claude-opus-5", "proposed"))
        # Aucun secret nulle part dans la base.
        dump = "\n".join(self.conn.iterdump())
        self.assertNotIn(FAKE_KEY, dump)
        # Seuls des champs publics sont envoyes (pas de description manuelle).
        sent = client.calls[0]["messages"][0]["content"]
        self.assertIn("transcript_excerpt", sent)
        self.assertNotIn("description_manual", sent)

    def test_sensitive_source_stays_with_human(self):
        source = add_source(self.conn, "https://example.test/tdah", "Traitement du TDAH",
                            category="Santé & bien-être",
                            transcript="Ce médicament réduirait les symptômes du TDAH.")
        self.enqueue(source)
        client = FakeClient(reply=json.dumps({
            "decision": "publish", "classification": "novelty",
            "relevance": 0.9, "confidence": 0.9, "rationale": "Intéressant."}))
        result = mentor_review.process_queue(self.conn, self.provider(client))
        self.assertEqual(result["processed"][0]["status"], "needs_human")
        self.assertTrue(result["processed"][0]["cloud"]["sensitive_kept_for_human"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM intelligence_mentor_insights_local")
            .fetchone()[0], 0)

    def test_reject_and_malformed_answers(self):
        s1 = add_source(self.conn, "https://example.test/a", "Promo", transcript="Lien en bio.")
        s2 = add_source(self.conn, "https://example.test/b", "Autre", transcript="Texte.")
        self.enqueue(s1)
        self.enqueue(s2)
        replies = iter([json.dumps({"decision": "reject", "classification": "low_signal",
                                    "relevance": 0.1, "confidence": 0.9, "rationale": "Bruit."}),
                        "pas du JSON"])
        client = FakeClient()
        original = client.beta.messages.create

        def create(**kwargs):
            client.reply = next(replies)
            return original(**kwargs)

        client.beta.messages.create = create
        result = mentor_review.process_queue(self.conn, self.provider(client))
        statuses = sorted(item["status"] for item in result["processed"])
        self.assertEqual(statuses, ["cloud_rejected", "needs_human"])

    def test_ask_cloud_synthesis_via_adapter(self):
        add_source(self.conn, "https://example.test/rtk", "RTK économise des tokens",
                   transcript="RTK réduit la consommation de tokens de 90 %.")
        client = FakeClient(reply="RTK réduit la consommation de tokens [1].")
        with mock.patch.dict(os.environ, {"VAULT_ASK_SYNTHESIZER": "cloud"}), \
                mock.patch.object(vault_cloud, "get_provider",
                                  return_value=self.provider(client)):
            result = vault_ask.ask(self.conn, "RTK et les tokens", semantic="off")
        self.assertTrue(result["llm"]["used"])
        self.assertIn("Synthèse cloud (claude:claude-opus-5)", result["answer"])
        # La reponse deterministe citee reste en tete.
        self.assertTrue(result["answer"].startswith("Tu avais notamment sauvegardé"))

    def test_manual_note_is_never_sent_to_cloud(self):
        source = add_source(self.conn, "https://example.test/rtk", "RTK économise des tokens",
                            transcript="RTK réduit la consommation de tokens de 90 %.")
        self.conn.execute("UPDATE contents SET description_manual = ? WHERE id = ?",
                          ("Ma note perso : tester RTK sur le projet client Dupont avant la "
                           "réunion de lundi, et comparer avec la facture du mois dernier.",
                           source))
        self.conn.commit()
        client = FakeClient(reply="Synthèse [1].")
        with mock.patch.dict(os.environ, {"VAULT_ASK_SYNTHESIZER": "cloud"}), \
                mock.patch.object(vault_cloud, "get_provider",
                                  return_value=self.provider(client)):
            result = vault_ask.ask(self.conn, "RTK et les tokens", semantic="off")
        # En local, la note reste affichee a l'utilisateur...
        self.assertIn("Ma note perso", result["answer"])
        # ... mais elle ne part jamais vers le provider cloud.
        sent = client.calls[0]["messages"][0]["content"]
        self.assertNotIn("note perso", sent)
        self.assertNotIn("Dupont", sent)
        self.assertIn("RTK", sent)

    def test_auto_synthesizer_falls_back_to_cloud(self):
        add_source(self.conn, "https://example.test/rtk", "RTK économise des tokens",
                   transcript="RTK réduit la consommation de tokens de 90 %.")
        client = FakeClient(reply="Synthèse cloud [1].")
        with mock.patch.dict(os.environ, {"VAULT_ASK_SYNTHESIZER": "auto"}), \
                mock.patch.object(vault_ask, "_ollama_available", return_value=False), \
                mock.patch.object(vault_cloud, "get_provider",
                                  return_value=self.provider(client)):
            result = vault_ask.ask(self.conn, "RTK et les tokens", semantic="off")
        self.assertEqual([a["synthesizer"] for a in result["llm"]["attempts"]],
                         ["local", "cloud"])
        self.assertTrue(result["llm"]["used"])


if __name__ == "__main__":
    unittest.main()
