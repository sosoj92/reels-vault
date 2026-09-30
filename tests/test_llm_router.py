import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import json
import os
import socket
import time
import unittest
import uuid
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

    def configured(self):
        return self._configured

    def reachable(self):
        return self._reachable

    def pc_ok(self):
        return self._pc

    def key(self):
        return "sk-secret-key-value-123456"

    def call(self, system, user, *, schema=None, max_tokens=300, timeout=None):
        self.calls += 1
        reply = self.replies.pop(0) if self.replies else {"ok": True}
        if isinstance(reply, BaseException):
            raise reply
        return {"text": json.dumps(reply) if schema else "texte", "input_tokens": 1000,
                "output_tokens": 500, "model": self.model}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def router(**providers):
    base = {"local": FakeProvider("local", False), "mistral": FakeProvider("mistral", True),
            "anthropic": FakeProvider("anthropic", True)}
    base.update(providers)
    clock = Clock()
    r = lr.LLMRouter(providers=base, breaker=lr.Breaker(clock=clock), sleep=lambda s: None,
                     usage_db=_isolation.TMP / f"usage-{uuid.uuid4().hex[:8]}.db")
    r.clock = clock
    return r


SCHEMA = {"type": "object"}


class RoutingPolicyTests(unittest.TestCase):
    def test_deterministic_never_calls_a_llm(self):
        r = router()
        with self.assertRaises(lr.LLMError):
            r.chat_json("s", "u", SCHEMA, task="deterministic")
        self.assertEqual(sum(p.calls for p in r.providers.values()), 0)

    def test_light_prefers_idle_local_then_mistral(self):
        r = router()
        self.assertEqual(r.chat_json("s", "u", SCHEMA, task="light"), {"ok": True})
        self.assertEqual(r.last["provider"], "local")
        busy = router(local=FakeProvider("local", False, pc=(False, "CPU 95%")))
        busy.chat_json("s", "u", SCHEMA, task="light")
        self.assertEqual(busy.last["provider"], "mistral")
        self.assertEqual(busy.last["skipped"]["local"], "CPU 95%")
        down = router(local=FakeProvider("local", False, reachable=False))
        down.chat_json("s", "u", SCHEMA, task="light")
        self.assertEqual(down.last["provider"], "mistral")

    def test_standard_uses_mistral_then_local(self):
        r = router()
        r.chat_json("s", "u", SCHEMA, task="standard")
        self.assertEqual(r.last["provider"], "mistral")
        nokey = router(mistral=FakeProvider("mistral", True, configured=False))
        nokey.chat_json("s", "u", SCHEMA, task="standard")
        self.assertEqual(nokey.last["provider"], "local")

    def test_claude_only_for_complex_and_never_a_generic_fallback(self):
        r = router(mistral=FakeProvider("mistral", True, configured=False),
                   local=FakeProvider("local", False, reachable=False))
        self.assertIsNone(r.chat_json("s", "u", SCHEMA, task="standard"))
        self.assertEqual(r.providers["anthropic"].calls, 0)
        r.chat_json("s", "u", SCHEMA, task="complex")
        self.assertEqual(r.last["provider"], "anthropic")
        noclaude = router(anthropic=FakeProvider("anthropic", True, configured=False))
        noclaude.chat_json("s", "u", SCHEMA, task="complex")
        self.assertEqual(noclaude.last["provider"], "mistral")  # Claude absent : desactive


class PrivacyTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(lr.classify("mot de passe : Hunter2!"), "secret")
        self.assertEqual(lr.classify("ma cle sk-abcdefghijklmnopqrstuvwx"), "secret")
        self.assertEqual(lr.classify("IBAN FR76 3000 6000 0112 3456 7890 189"), "secret")
        self.assertEqual(lr.classify("carte 4539 1488 0343 6467"), "secret")
        self.assertEqual(lr.classify("Mon traitement contre le TDAH"), "sensitive")
        self.assertEqual(lr.classify("ecris a rosy@example.com"), "personal")
        self.assertEqual(lr.classify("Apify et Claude pour la prospection"), "internal")

    def test_sensitive_stays_local_and_secret_never_leaves(self):
        r = router()
        r.chat_json("s", "Mon diagnostic TDAH et mon traitement", SCHEMA, task="standard")
        self.assertEqual(r.last["provider"], "local")
        self.assertEqual(r.providers["mistral"].calls, 0)
        down = router(local=FakeProvider("local", False, reachable=False))
        self.assertIsNone(down.chat_json("s", "ordonnance et medicament", SCHEMA, task="light"))
        self.assertEqual(down.providers["mistral"].calls, 0)
        secret = router()
        self.assertIsNone(secret.chat_json("s", "token = abcdefghijklmnop123", SCHEMA))
        self.assertEqual(sum(p.calls for p in secret.providers.values()), 0)

    def test_personal_needs_explicit_policy(self):
        r = router(local=FakeProvider("local", False, reachable=False))
        self.assertIsNone(r.chat_json("s", "contact : jean@example.com", SCHEMA, task="standard"))
        with mock.patch.dict(os.environ, {"LLM_PERSONAL_CLOUD_ALLOWED": "true"}):
            r.chat_json("s", "contact : jean@example.com", SCHEMA, task="standard")
        self.assertEqual(r.last["provider"], "mistral")


class BudgetAndJournalTests(unittest.TestCase):
    def test_calls_are_journaled_without_content(self):
        r = router()
        r.chat_json("SYSTEME SECRET", "QUESTION PRIVEE", SCHEMA, task="standard",
                    purpose="test")
        row = r.conn.execute("SELECT * FROM llm_calls").fetchone()
        columns = [d[0] for d in r.conn.execute("SELECT * FROM llm_calls").description]
        record = dict(zip(columns, row))
        self.assertEqual((record["provider"], record["task"], record["success"]),
                         ("mistral", "standard", 1))
        self.assertEqual((record["input_tokens"], record["output_tokens"]), (1000, 500))
        self.assertAlmostEqual(record["cost_eur"], 1000 / 1e6 * 0.10 + 500 / 1e6 * 0.30)
        self.assertNotIn("QUESTION PRIVEE", json.dumps(record))

    def test_budget_reached_is_never_exceeded_silently(self):
        r = router()
        r.conn.execute("INSERT INTO llm_calls (provider, task, sensitivity, cost_eur, success, "
                       "created_at) VALUES ('mistral', 'standard', 'internal', 5.0, 1, ?)",
                       (lr.datetime.now(lr.timezone.utc).isoformat(),))
        r.conn.commit()
        r.chat_json("s", "u", SCHEMA, task="standard")
        self.assertEqual(r.last["provider"], "local")
        self.assertEqual(r.providers["mistral"].calls, 0)
        self.assertIn("budget", r.status()["mistral"]["reason"])

    def test_claude_daily_budget(self):
        r = router()
        r.conn.execute("INSERT INTO llm_calls (provider, task, sensitivity, cost_eur, success, "
                       "created_at) VALUES ('anthropic', 'complex', 'internal', 0.6, 1, ?)",
                       (lr.datetime.now(lr.timezone.utc).isoformat(),))
        r.conn.commit()
        r.chat_json("s", "u", SCHEMA, task="complex")
        self.assertEqual(r.last["provider"], "mistral")


class ResilienceTests(unittest.TestCase):
    def test_timeout_opens_the_circuit_then_recovers(self):
        slow = FakeProvider("mistral", True, replies=[socket.timeout()] * 5)
        r = router(mistral=slow)
        for _ in range(3):
            r.chat_json("s", "u", SCHEMA, task="standard")
            self.assertEqual(r.last["provider"], "local")
        self.assertEqual(slow.calls, 3)  # timeout : pas de relance immediate
        r.chat_json("s", "u", SCHEMA, task="standard")
        self.assertEqual(slow.calls, 3)  # coupe-circuit ouvert
        self.assertEqual(r.status()["mistral"]["reason"], "coupe-circuit ouvert")
        slow.replies = [{"ok": True}]
        r.clock.t += lr.BREAKER_COOLDOWN + 1  # « Internet revient »
        r.chat_json("s", "u", SCHEMA, task="standard")
        self.assertEqual(r.last["provider"], "mistral")

    def test_everything_offline_answers_none_fast(self):
        r = router(local=FakeProvider("local", False, reachable=False),
                   mistral=FakeProvider("mistral", True, replies=[OSError("offline")] * 9),
                   anthropic=FakeProvider("anthropic", True, replies=[OSError("offline")] * 9))
        started = time.perf_counter()
        self.assertIsNone(r.chat_json("s", "u", SCHEMA, task="complex"))
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual(r.last["reason"], "aucun fournisseur disponible")

    def test_invalid_json_falls_back(self):
        bad = FakeProvider("mistral", True)
        bad.call = lambda *a, **k: {"text": "pas du json", "model": "m"}
        r = router(mistral=bad)
        self.assertEqual(r.chat_json("s", "u", SCHEMA, task="standard"), {"ok": True})
        self.assertEqual((r.last["provider"], r.last["fallback_from"]), ("local", "mistral"))


class ProviderTests(unittest.TestCase):
    def test_mistral_key_never_in_errors_and_disabled_without_key(self):
        with mock.patch.dict(os.environ, {"MISTRAL_API_KEY": ""}):
            self.assertFalse(lr.MistralProvider().configured())
        import urllib.error

        key = "mst-super-secret-key-0123456789"
        error = urllib.error.HTTPError("u", 401, f"bad {key}", {}, None)
        with mock.patch.dict(os.environ, {"MISTRAL_API_KEY": key}), \
                mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(lr.LLMError) as ctx:
                lr.MistralProvider().call("s", "u")
        self.assertNotIn(key, str(ctx.exception))

    def test_models_and_keep_alive_come_from_configuration(self):
        with mock.patch.dict(os.environ, {"OLLAMA_MODEL": "qwen3:4b", "MISTRAL_MODEL": "m-x",
                                          "OLLAMA_IDLE_UNLOAD_MINUTES": "3"}):
            self.assertEqual(lr.OllamaProvider().model, "qwen3:4b")
            self.assertEqual(lr.OllamaProvider().keep_alive(), "3m")
            self.assertEqual(lr.MistralProvider().model, "m-x")

    def test_claude_disabled_without_key(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertFalse(lr.AnthropicProvider().configured())

    def test_local_refused_when_pc_is_busy(self):
        busy = lr.OllamaProvider(load_probe=lambda: (97.0, 4000))
        self.assertEqual(busy.pc_ok(), (False, "CPU 97.0%"))
        low_ram = lr.OllamaProvider(load_probe=lambda: (10.0, 500))
        self.assertFalse(low_ram.pc_ok()[0])

    def test_offline_socket_fails_immediately(self):
        started = time.perf_counter()
        self.assertFalse(lr.OllamaProvider().reachable())
        self.assertLess(time.perf_counter() - started, 1.0)


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
