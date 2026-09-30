import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import io
import json
import os
import socket
import time
import unittest
import urllib.error
import uuid
from contextlib import redirect_stdout
from unittest import mock

import llm_router as lr


class FakeProvider:
    def __init__(self, name, cloud, *, configured=True, reachable=True, pc=(True, None),
                 replies=None, model=None):
        self.name, self.cloud = name, cloud
        self.model = model or f"{name}-model"
        self._configured, self._reachable, self._pc = configured, reachable, pc
        self.replies = list(replies or [])
        self.calls = 0
        self.tasks = []

    def configured(self):
        return self._configured

    def reachable(self):
        return self._reachable

    def pc_ok(self):
        return self._pc

    def key(self):
        return "sk-secret-key-value-123456"

    def credits_remaining(self):
        return getattr(self, "credits", None)

    def call(self, system, user, *, schema=None, max_tokens=300, timeout=None, task=None):
        self.calls += 1
        self.tasks.append(task)
        reply = self.replies.pop(0) if self.replies else {"ok": True}
        if isinstance(reply, BaseException):
            raise reply
        return {"text": json.dumps(reply) if schema else "texte", "input_tokens": 1000,
                "output_tokens": 500, "model": self.model, "cost_usd": 0.001}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def router(**providers):
    base = {"local": FakeProvider("local", False), "gemini": FakeProvider("gemini", True),
            "openrouter": FakeProvider("openrouter", True),
            "mistral": FakeProvider("mistral", True),
            "anthropic": FakeProvider("anthropic", True)}
    base.update(providers)
    clock = Clock()
    r = lr.LLMRouter(providers=base, breaker=lr.Breaker(clock=clock), sleep=lambda s: None,
                     usage_db=_isolation.TMP / f"usage-{uuid.uuid4().hex[:8]}.db")
    r.test_clock = clock
    return r


def calls(r):
    return {name: p.calls for name, p in r.providers.items()}


SCHEMA = {"type": "object"}


class RoutingPolicyTests(unittest.TestCase):
    def test_deterministic_never_calls_a_llm(self):
        r = router()
        with self.assertRaises(lr.LLMError):
            r.chat_json("s", "u", SCHEMA, task="deterministic")
        self.assertEqual(sum(calls(r).values()), 0)

    def test_light_local_then_gemini_then_openrouter_then_queue(self):
        r = router()
        r.chat_json("s", "u", SCHEMA, task="light")
        self.assertEqual(r.last["provider"], "local")
        busy = router(local=FakeProvider("local", False, pc=(False, "RAM libre 900 Mo")))
        busy.chat_json("s", "u", SCHEMA, task="light")
        self.assertEqual(busy.last["provider"], "gemini")
        self.assertEqual(busy.last["skipped"]["local"], "RAM libre 900 Mo")
        nogem = router(local=FakeProvider("local", False, reachable=False),
                       gemini=FakeProvider("gemini", True, configured=False))
        nogem.chat_json("s", "u", SCHEMA, task="light")
        self.assertEqual(nogem.last["provider"], "openrouter")
        nothing = router(local=FakeProvider("local", False, reachable=False),
                         gemini=FakeProvider("gemini", True, configured=False),
                         openrouter=FakeProvider("openrouter", True, configured=False))
        self.assertIsNone(nothing.chat_json("s", "u", SCHEMA, task="light"))
        self.assertTrue(nothing.last["queued"])

    def test_standard_gemini_then_local_then_openrouter(self):
        r = router()
        r.chat_json("s", "u", SCHEMA, task="standard")
        self.assertEqual(r.last["provider"], "gemini")
        down = router(gemini=FakeProvider("gemini", True, replies=[lr.LLMError("HTTP 429 quota")]))
        down.chat_json("s", "u", SCHEMA, task="standard")
        self.assertEqual((down.last["provider"], down.last["fallback_from"]), ("local", "gemini"))
        busy = router(gemini=FakeProvider("gemini", True, configured=False),
                      local=FakeProvider("local", False, pc=(False, "CPU 95%")))
        busy.chat_json("s", "u", SCHEMA, task="standard")
        self.assertEqual(busy.last["provider"], "openrouter")

    def test_complex_uses_gemini_reasoning_then_openrouter_never_local(self):
        r = router()
        r.chat_json("s", "u", SCHEMA, task="complex")
        self.assertEqual((r.last["provider"], r.providers["gemini"].tasks), ("gemini", ["complex"]))
        fallback = router(gemini=FakeProvider("gemini", True, replies=[socket.timeout()]))
        fallback.chat_json("s", "u", SCHEMA, task="complex")
        self.assertEqual(fallback.last["provider"], "openrouter")
        self.assertEqual(fallback.providers["local"].calls, 0)

    def test_mistral_and_anthropic_are_never_in_the_default_chains(self):
        r = router(gemini=FakeProvider("gemini", True, configured=False),
                   openrouter=FakeProvider("openrouter", True, configured=False),
                   local=FakeProvider("local", False, reachable=False))
        for task in ("light", "standard", "complex"):
            self.assertIsNone(r.chat_json("s", "u", SCHEMA, task=task))
        self.assertEqual((r.providers["mistral"].calls, r.providers["anthropic"].calls), (0, 0))


class PrivacyTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(lr.classify("mot de passe : Hunter2!"), "secret")
        self.assertEqual(lr.classify("ma cle sk-abcdefghijklmnopqrstuvwx"), "secret")
        self.assertEqual(lr.classify("IBAN FR76 3000 6000 0112 3456 7890 189"), "secret")
        self.assertEqual(lr.classify("carte 4539 1488 0343 6467"), "secret")
        self.assertEqual(lr.classify("Mon traitement contre le TDAH"), "sensitive")
        self.assertEqual(lr.classify("ecris a rosy@example.com"), "personal")
        self.assertEqual(lr.classify("Apify et Claude pour la prospection"), "internal")

    def test_sensitive_never_goes_to_the_cloud(self):
        r = router()
        r.chat_json("s", "Mon diagnostic TDAH et mon traitement", SCHEMA, task="standard")
        self.assertEqual(r.last["provider"], "local")
        down = router(local=FakeProvider("local", False, reachable=False))
        self.assertIsNone(down.chat_json("s", "ordonnance et medicament", SCHEMA,
                                         task="complex"))
        self.assertTrue(down.last["queued"])
        self.assertEqual((down.providers["gemini"].calls, down.providers["openrouter"].calls),
                         (0, 0))

    def test_secret_never_leaves(self):
        r = router()
        self.assertIsNone(r.chat_json("s", "token = abcdefghijklmnop123", SCHEMA))
        self.assertEqual(sum(calls(r).values()), 0)

    def test_personal_needs_explicit_policy(self):
        r = router(local=FakeProvider("local", False, reachable=False))
        self.assertIsNone(r.chat_json("s", "contact : jean@example.com", SCHEMA, task="standard"))
        with mock.patch.dict(os.environ, {"LLM_PERSONAL_CLOUD_ALLOWED": "true"}):
            r.chat_json("s", "contact : jean@example.com", SCHEMA, task="standard")
        self.assertEqual(r.last["provider"], "gemini")


class OpenRouterBudgetTests(unittest.TestCase):
    def _openrouter_only(self, **kwargs):
        return router(local=FakeProvider("local", False, reachable=False),
                      gemini=FakeProvider("gemini", True, configured=False), **kwargs)

    def _spend(self, r, usd, provider="openrouter", days_ago=0):
        when = lr.datetime.now(lr.timezone.utc) - lr.timedelta(days=days_ago)
        r.conn.execute("INSERT INTO llm_calls (provider, task, sensitivity, cost_usd, success, "
                       "created_at) VALUES (?, 'standard', 'internal', ?, 1, ?)",
                       (provider, usd, when.isoformat()))
        r.conn.commit()

    def test_daily_budget_reached_queues(self):
        r = self._openrouter_only()
        self._spend(r, 0.25)
        self.assertIsNone(r.chat_json("s", "u", SCHEMA, task="standard"))
        self.assertEqual(r.providers["openrouter"].calls, 0)
        self.assertEqual(r.last["skipped"]["openrouter"], "budget OpenRouter journalier atteint")

    def test_reserve_reached_queues(self):
        provider = FakeProvider("openrouter", True)
        provider.credits = 0.95  # credit reel sous la reserve de 1 USD
        r = self._openrouter_only(openrouter=provider)
        self.assertIsNone(r.chat_json("s", "u", SCHEMA, task="standard"))
        self.assertEqual(r.last["skipped"]["openrouter"], "reserve OpenRouter atteinte")
        self.assertTrue(r.last["queued"])

    def test_reserve_estimated_locally_when_credit_unknown(self):
        r = self._openrouter_only()
        with mock.patch.dict(os.environ, {"OPENROUTER_INITIAL_CREDIT_USD": "7.00",
                                          "OPENROUTER_DAILY_BUDGET_USD": "100"}):
            self._spend(r, 6.10, days_ago=60)  # 7.00 - 6.10 = 0.90 <= reserve
            self.assertIsNone(r.chat_json("s", "u", SCHEMA, task="standard"))
        self.assertEqual(r.last["skipped"]["openrouter"], "reserve OpenRouter atteinte")

    def test_real_credit_takes_priority_again_when_readable(self):
        provider = FakeProvider("openrouter", True)
        r = self._openrouter_only(openrouter=provider)
        with mock.patch.dict(os.environ, {"OPENROUTER_INITIAL_CREDIT_USD": "7.00",
                                          "OPENROUTER_DAILY_BUDGET_USD": "100"}):
            self._spend(r, 6.50, days_ago=60)
            provider.credits = None            # API illisible : estimation 7.00 - 6.50
            self.assertAlmostEqual(r.openrouter_remaining(), 0.50)
            self.assertEqual(r.over_budget("openrouter"), "reserve OpenRouter atteinte")
            provider.credits = 4.20            # l'API repond de nouveau : solde reel prioritaire
            self.assertEqual(r.openrouter_remaining(), 4.20)
            self.assertIsNone(r.over_budget("openrouter"))

    def test_cost_is_journaled_in_usd_without_content(self):
        r = self._openrouter_only()
        r.chat_json("s", "QUESTION PRIVEE", SCHEMA, task="standard", purpose="test")
        row = dict(zip([d[0] for d in r.conn.execute("SELECT * FROM llm_calls").description],
                       r.conn.execute("SELECT * FROM llm_calls").fetchone()))
        self.assertEqual((row["provider"], row["task"], row["success"], row["cost_usd"]),
                         ("openrouter", "standard", 1, 0.001))
        self.assertNotIn("QUESTION PRIVEE", json.dumps(row))

    def test_expensive_models_and_claude_for_simple_tasks_are_refused(self):
        provider = lr.OpenRouterProvider()
        with mock.patch.dict(os.environ, {
                "OPENROUTER_STANDARD_MODEL": "anthropic/claude-sonnet-4",
                "OPENROUTER_COMPLEX_MODEL": "anthropic/claude-opus-4",
                "OPENROUTER_FALLBACK_MODELS": "openai/gpt-4o-mini,openai/o3-pro,a,b"}):
            self.assertEqual(provider.allowed_models("standard"), ["openai/gpt-4o-mini",
                                                                   "openai/o3-pro"][:1])
            self.assertEqual(provider.allowed_models("complex"), ["openai/gpt-4o-mini"])


class ResilienceTests(unittest.TestCase):
    def test_no_double_billing_cloud_is_never_retried(self):
        gemini = FakeProvider("gemini", True, replies=[lr.LLMError("HTTP 500")])
        r = router(gemini=gemini)
        r.chat_json("s", "u", SCHEMA, task="standard")
        self.assertEqual(gemini.calls, 1)
        self.assertEqual(r.last["provider"], "local")

    def test_gemini_to_openrouter_fallback(self):
        r = router(local=FakeProvider("local", False, pc=(False, "RAM")),
                   gemini=FakeProvider("gemini", True, replies=[lr.LLMError("HTTP 429 quota")]))
        self.assertEqual(r.chat_json("s", "u", SCHEMA, task="light"), {"ok": True})
        self.assertEqual((r.last["provider"], r.last["fallback_from"]), ("openrouter", "gemini"))

    def test_internet_down_goes_offline_local_only(self):
        gemini = FakeProvider("gemini", True, replies=[lr.NetworkDown("URLError")])
        r = router(gemini=gemini)
        r.chat_json("s", "u", SCHEMA, task="standard")
        self.assertEqual(r.last["provider"], "local")
        self.assertEqual(r.providers["openrouter"].calls, 0)
        self.assertTrue(r.offline())
        r.chat_json("s", "u", SCHEMA, task="standard")
        self.assertEqual(gemini.calls, 1)  # hors ligne : cloud ignore
        r.test_clock.t += lr.OFFLINE_COOLDOWN + 1  # « Internet revient »
        r.chat_json("s", "u", SCHEMA, task="standard")
        self.assertEqual(r.last["provider"], "gemini")

    def test_offline_without_local_queues_quickly(self):
        r = router(local=FakeProvider("local", False, reachable=False),
                   gemini=FakeProvider("gemini", True, replies=[lr.NetworkDown("x")]))
        started = time.perf_counter()
        self.assertIsNone(r.chat_json("s", "u", SCHEMA, task="standard"))
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertTrue(r.last["queued"])

    def test_breaker_opens_after_repeated_timeouts(self):
        gemini = FakeProvider("gemini", True, replies=[socket.timeout()] * 5)
        r = router(gemini=gemini)
        for _ in range(4):
            r.chat_json("s", "u", SCHEMA, task="standard")
        self.assertEqual(gemini.calls, 3)
        self.assertEqual(r.status()["gemini"]["reason"], "coupe-circuit ouvert")

    def test_invalid_key_disables_provider(self):
        gemini = lr.GeminiProvider()
        error = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(
            b'{"error": {"status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}}'))
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "AIza-fake-key-0123456789"}), \
                mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(lr.InvalidKey):
                gemini.call("s", "u")
            self.assertFalse(gemini.configured())


class ProviderTests(unittest.TestCase):
    def test_gemini_request_and_parsing(self):
        seen = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, timeout=None):
            seen["url"], seen["headers"] = request.full_url, dict(request.header_items())
            seen["body"] = json.loads(request.data)
            return Response(json.dumps({"candidates": [{"content": {"parts": [{"text": '{"a": 1}'}]}}],
                                        "usageMetadata": {"promptTokenCount": 10,
                                                          "candidatesTokenCount": 4},
                                        "modelVersion": "gemini-x"}).encode())

        key = "AIza-secret-key-0123456789"
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": key,
                                          "GEMINI_LIGHT_MODEL": "g-light",
                                          "GEMINI_COMPLEX_MODEL": "g-pro"}), \
                mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            raw = lr.GeminiProvider().call("s", "u", schema=SCHEMA, task="light")
            self.assertIn("/models/g-light:generateContent", seen["url"])
            self.assertNotIn(key, seen["url"])  # cle en en-tete, jamais dans l'URL
            self.assertEqual(seen["body"]["generationConfig"]["thinkingConfig"]["thinkingBudget"], 0)
            lr.GeminiProvider().call("s", "u", task="complex")
            self.assertIn("/models/g-pro:", seen["url"])
        self.assertEqual((raw["text"], raw["input_tokens"], raw["output_tokens"]),
                         ('{"a": 1}', 10, 4))

    def test_quota_and_keys_never_in_errors(self):
        key = "sk-or-v1-super-secret-0123456789"
        error = urllib.error.HTTPError("u", 429, "quota", {}, io.BytesIO(f"limit {key}".encode()))
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": key}), \
                mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(lr.LLMError) as ctx:
                lr.OpenRouterProvider().call("s", "u")
        self.assertIn("429", str(ctx.exception))
        self.assertNotIn(key, str(ctx.exception))

    def test_disabled_without_keys(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "", "OPENROUTER_API_KEY": ""}):
            self.assertFalse(lr.GeminiProvider().configured())
            self.assertFalse(lr.OpenRouterProvider().configured())
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k", "GEMINI_ENABLED": "false"}):
            self.assertFalse(lr.GeminiProvider().configured())

    def test_local_refused_when_pc_is_busy(self):
        busy = lr.OllamaProvider(load_probe=lambda: (97.0, 4000))
        self.assertEqual(busy.pc_ok(), (False, "CPU 97.0%"))

    def test_offline_socket_fails_immediately(self):
        started = time.perf_counter()
        self.assertFalse(lr.OllamaProvider().reachable())
        self.assertLess(time.perf_counter() - started, 1.0)


class SetupTests(unittest.TestCase):
    def test_status_report_never_prints_keys(self):
        key = "AIza-never-print-me-0123456789"
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": key, "OPENROUTER_API_KEY": ""}), \
                mock.patch("llm_setup.check_gemini", return_value=("READY", None)), \
                mock.patch("llm_setup.check_qwen", return_value=("UNAVAILABLE", "RAM")), \
                redirect_stdout(out):
            import llm_setup

            states = llm_setup.report()
        self.assertEqual(states, {"Qwen": "UNAVAILABLE", "Gemini": "READY",
                                  "OpenRouter": "NOT CONFIGURED", "Anthropic": "NOT CONFIGURED"})
        self.assertNotIn(key, out.getvalue())

    def test_invalid_key_is_reported_and_not_saved(self):
        import llm_setup

        with mock.patch("telegram_api.write_env") as write, \
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}), \
                mock.patch("llm_setup.check_gemini", return_value=("INVALID", "cle refusee")), \
                redirect_stdout(io.StringIO()):
            state = llm_setup.ask_key("Gemini", "GEMINI_API_KEY", llm_setup.check_gemini,
                                      input_func=lambda _p: "o",
                                      secret_input=lambda _p: "AIza-bad-key")
            self.assertEqual(os.environ.get("GEMINI_API_KEY"), "")
        self.assertEqual(state, "INVALID")
        write.assert_not_called()

    def test_valid_key_is_saved_to_env_only(self):
        import llm_setup

        with mock.patch("telegram_api.write_env") as write, \
                mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}), \
                mock.patch("llm_setup.check_openrouter", return_value=("READY", "credit ~7")), \
                redirect_stdout(io.StringIO()):
            llm_setup.ask_key("OpenRouter", "OPENROUTER_API_KEY", llm_setup.check_openrouter,
                              input_func=lambda _p: "o", secret_input=lambda _p: "sk-or-good")
        write.assert_called_once_with({"OPENROUTER_API_KEY": "sk-or-good"})


class DeterministicPathsTests(unittest.TestCase):
    def test_passes_and_linking_use_zero_llm(self):
        import _life_fixtures as fx
        import mentor_project_links as links
        import mentor_proactive as proactive

        _, conn, _, _ = fx.life_db()
        with mock.patch.object(lr.LLMRouter, "_run", side_effect=AssertionError("LLM")), \
                mock.patch("mentor_council.OllamaBackend.chat_json",
                           side_effect=AssertionError("LLM")):
            proactive.run_pass(conn, now=fx.NOW)
            links.analyze(conn)
        conn.close()


if __name__ == "__main__":
    unittest.main()
