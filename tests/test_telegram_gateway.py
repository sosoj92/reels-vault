import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import _life_fixtures as fx

import mentor_council as council
import mentor_proactive as proactive
import mentor_proactive_store as store
import telegram_api
import telegram_gateway as tg
import vault_ask
import vault_db

USER, CHAT = 1001, 1001
PARIS = timezone(timedelta(hours=2))
NOON = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)


def real_ask(conn, question, llm="off"):
    # Retrieval reel du Vault (fixture), sans LLM : les tests ne dependent pas d'Ollama.
    return vault_ask.ask(conn, question, mode="consult", llm="off", semantic="off")


def msg(text, *, user=USER, chat=CHAT, reply_to=None, message_id=1):
    message = {"message_id": message_id, "from": {"id": user}, "chat": {"id": chat},
               "text": text}
    if reply_to:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": message_id, "message": message}


def click(data, message_id, *, user=USER, text="💡 x"):
    return {"update_id": 5000, "callback_query": {
        "id": f"cb-{data}", "from": {"id": user}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": CHAT}, "text": text}}}


class GatewayCase(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()
        self.path, self.conn, self.ids, self.sources = fx.life_db()
        self.telegram = fx.FakeTelegram()
        self.backend = fx.FakeBackend()
        self.notion = fx.FakeNotion()
        self.tasks = fx.FakeNotion()
        self.inbox = _isolation.TMP / f"inbox-{id(self)}.txt"
        self.gw = self.make_gateway()

    def make_gateway(self, conn=None, transcriber=None):
        return tg.Gateway(conn or self.conn, self.telegram, allowed_user=USER, allowed_chat=CHAT,
                          backend=self.backend, notion=self.notion, notion_tasks=self.tasks,
                          tz=PARIS, ask_fn=real_ask, inbox=self.inbox,
                          transcriber=transcriber or (lambda data: data.decode("utf-8")))

    def tearDown(self):
        self.conn.close()

    def last(self):
        return self.telegram.sent[-1]

    def suggestions(self):
        proactive.run_pass(self.conn, now=fx.NOW)
        council.review_shortlist(self.conn, self.backend, now=fx.NOW, limit=5)
        return {s["suggestion_type"]: s for s in store.list_suggestions(self.conn)}


class SecurityTests(GatewayCase):
    def test_unauthorized_user_or_chat_is_silently_ignored(self):
        self.assertEqual(self.gw.handle_update(msg("salut", user=666)), "ignored")
        self.assertEqual(self.gw.handle_update(msg("salut", chat=777)), "ignored")
        self.assertEqual(self.gw.handle_update(click("s:SUG-0001:task", 1, user=666)), "ignored")
        self.assertEqual(self.telegram.sent, [])

    def test_missing_whitelist_rejects_everyone(self):
        gw = tg.Gateway(self.conn, self.telegram, allowed_user="", allowed_chat="",
                        ask_fn=real_ask, inbox=self.inbox)
        self.assertEqual(gw.handle_update(msg("salut")), "ignored")

    def test_no_shell_or_arbitrary_file_access_in_gateway(self):
        source = Path(tg.__file__).read_text(encoding="utf-8")
        for forbidden in ("subprocess", "os.system", "eval(", "exec(", "shutil"):
            self.assertNotIn(forbidden, source)

    def test_token_is_never_exposed_in_errors(self):
        token = "123456:SECRET-TOKEN-VALUE"
        client = telegram_api.TelegramClient(token)
        with mock.patch("urllib.request.urlopen", side_effect=OSError(f"boom {token}")):
            with self.assertRaises(telegram_api.TelegramError) as ctx:
                client.get_me()
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertEqual(telegram_api.redact(f"x {token} y", token), "x *** y")
        self.assertNotIn("SECRET", telegram_api.mask(token))

    def test_build_forces_local_synthesis_and_requires_config(self):
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_ALLOWED_USER_ID": "",
                                            "TELEGRAM_ALLOWED_CHAT_ID": ""}), \
                mock.patch.object(telegram_api, "load_env"):
            with self.assertRaises(SystemExit):
                tg.build(self.path)
        env = {"TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_ALLOWED_USER_ID": "1",
               "TELEGRAM_ALLOWED_CHAT_ID": "1", "VAULT_ASK_SYNTHESIZER": "auto"}
        with mock.patch.dict("os.environ", env), mock.patch.object(telegram_api, "load_env"), \
                mock.patch.object(tg, "LOG_DIR", _isolation.TMP / "logs"), \
                mock.patch.object(council.OllamaBackend, "ensure_ready",
                                  return_value=False) as ready:
            gateway = tg.build(self.path)
            import os

            self.assertEqual(os.environ["VAULT_ASK_SYNTHESIZER"], "local")
            ready.assert_called_once()  # test de disponibilite de Qwen au demarrage
            gateway.conn.close()


class ReactiveTests(GatewayCase):
    def test_text_question_answers_from_the_vault(self):
        self.assertEqual(self.gw.handle_update(msg("Que sais-je sur le sourcing de profils data ?")),
                         "answer:ask")
        self.assertIn("Sourcing de profils data", self.last()["text"])

    def test_voice_is_transcribed_then_answered(self):
        self.telegram.files["voice-1"] = "Que sais-je sur le sourcing de profils data ?".encode()
        update = {"update_id": 9, "message": {"message_id": 9, "from": {"id": USER},
                                              "chat": {"id": CHAT}, "voice": {"file_id": "voice-1"}}}
        self.assertEqual(self.gw.handle_update(update), "answer:ask")
        self.assertTrue(self.telegram.sent[0]["text"].startswith("🎙️ «"))
        self.assertIn("Sourcing de profils data", self.last()["text"])

    def test_voice_failure_is_reported_politely(self):
        def broken(data):
            raise RuntimeError("no model")

        gw = self.make_gateway(transcriber=broken)
        self.telegram.files["v"] = b"x"
        update = {"update_id": 9, "message": {"message_id": 9, "from": {"id": USER},
                                              "chat": {"id": CHAT}, "voice": {"file_id": "v"}}}
        self.assertEqual(gw.handle_update(update), "voice_failed")
        self.assertIn("pas pu transcrire", self.last()["text"])

    def test_mode_a_named_agent(self):
        self.gw.handle_update(msg("Demande au CFO ce qu'il pense du sourcing data"))
        text = self.last()["text"]
        self.assertIn("mode A", text)
        self.assertIn("Chief Financial Officer — nomme explicitement", text)
        self.assertIn("Avis de Chief Financial Officer", text)
        self.assertEqual(store.list_consultations(self.conn), [])  # schema proactif absent

    def test_mode_b_auto_agents_with_reasons(self):
        self.gw.handle_update(msg("Comment exploiter ca pour Nexdoor Talent ?"))
        text = self.last()["text"]
        self.assertIn("mode B", text)
        self.assertIn("conseiller du projet Nexdoor Talent", text)
        self.assertLessEqual(text.count("• "), 3)

    def test_agent_adjustments_on_reply(self):
        self.gw.handle_update(msg("Comment exploiter ca pour Nexdoor Talent ?", message_id=10))
        answer_id = self.last()["message_id"]
        self.gw.handle_update(msg("sans le Growth Hacker", reply_to=answer_id, message_id=11))
        self.assertNotIn("Growth Hacker —", self.last()["text"])
        self.gw.handle_update(msg("demande plutot au Recruitment Agent", reply_to=answer_id,
                                  message_id=12))
        text = self.last()["text"]
        self.assertIn("Recruitment Specialist", text)
        self.assertNotIn("Chief Financial Officer —", text)
        self.gw.handle_update(msg("ajoute le CFO", reply_to=answer_id, message_id=13))
        self.assertIn("Chief Financial Officer —", self.last()["text"])

    def test_url_goes_to_inbox_only(self):
        self.assertEqual(self.gw.handle_update(msg("https://www.tiktok.com/@x/video/1")), "inbox")
        self.assertEqual(self.inbox.read_text(encoding="utf-8").strip(),
                         "https://www.tiktok.com/@x/video/1")

    def test_mentor_command_and_phrase(self):
        self.assertEqual(self.gw.handle_update(msg("/mentor")), "pending:0")
        self.suggestions()
        self.assertEqual(self.gw.handle_update(msg("Qu'est-ce que tu as pour moi ?")), "pending:2")
        self.assertEqual(len(self.telegram.buttons_of()), 2)


class ProactiveTests(GatewayCase):
    def setUp(self):
        super().setUp()
        self.sug = self.suggestions()
        self.report = self.gw.tick(NOON, force=True)
        self.push = self.telegram.sent[-1]

    def test_push_is_short_with_three_buttons(self):
        sid = self.sug["opportunity"]["suggestion_id"]
        self.assertEqual(self.report["pushed"], sid)
        self.assertTrue(self.push["text"].startswith("💡 "))
        self.assertLessEqual(self.push["text"].count("\n"), 2)
        self.assertEqual(self.telegram.buttons_of(), [f"s:{sid}:show", f"s:{sid}:later",
                                                      f"s:{sid}:nope"])
        self.assertEqual(store.get_suggestion(self.conn, sid)["status"], "sent")

    def test_show_me_gives_the_full_picture(self):
        sid = self.sug["opportunity"]["suggestion_id"]
        self.gw.handle_update(click(f"s:{sid}:show", self.push["message_id"]))
        text = self.last()["text"]
        for part in ("Hypothese", "Projet : Nexdoor Talent", "Sources", "Agents consultes",
                     "Recruitment Specialist", "Confiance", "Actions proposees"):
            self.assertIn(part, text)
        self.assertEqual(self.telegram.buttons_of(), [f"s:{sid}:task", f"s:{sid}:insight",
                                                      f"s:{sid}:edit", f"s:{sid}:no"])
        self.assertEqual(store.get_suggestion(self.conn, sid)["status"], "opened")

    def test_later_asks_when_then_snoozes_in_vault(self):
        sid = self.sug["opportunity"]["suggestion_id"]
        self.gw.handle_update(click(f"s:{sid}:later", self.push["message_id"]))
        options = [b["callback_data"] for row in self.telegram.edits[-1]["buttons"] for b in row]
        self.assertEqual(options, [f"s:{sid}:later:tonight", f"s:{sid}:later:tomorrow",
                                   f"s:{sid}:later:monday"])
        self.gw.handle_update(click(f"s:{sid}:later:monday", self.push["message_id"]))
        suggestion = store.get_suggestion(self.conn, sid)
        self.assertEqual(suggestion["status"], "snoozed")
        self.assertIsNotNone(suggestion["snoozed_until"])

    def test_not_relevant_asks_why_and_feeds_fatigue(self):
        sid = self.sug["opportunity"]["suggestion_id"]
        self.gw.handle_update(click(f"s:{sid}:nope", self.push["message_id"]))
        reasons = [b["callback_data"] for row in self.telegram.edits[-1]["buttons"] for b in row]
        self.assertEqual(reasons, [f"s:{sid}:nope:{r}" for r in
                                   ("not_now", "already_known", "off_topic", "bad_idea")])
        self.gw.handle_update(click(f"s:{sid}:nope:already_known", self.push["message_id"]))
        self.assertEqual(store.get_suggestion(self.conn, sid)["status"], "dismissed")
        self.assertEqual(store.feedback_since(self.conn, "2000")[0]["reason"], "already_known")

    def test_create_task_updates_vault_and_notion_after_click_only(self):
        sid = self.sug["opportunity"]["suggestion_id"]
        self.assertEqual(self.tasks.created, [])
        self.gw.handle_update(click(f"s:{sid}:task", self.push["message_id"]))
        suggestion = store.get_suggestion(self.conn, sid)
        self.assertEqual(suggestion["status"], "approved")
        action = vault_db.get_action(self.conn, suggestion["evidence"]["task_action_id"])
        self.assertEqual(action["status"], "approved")
        page = self.notion.page_for(f"suggestion:{sid}", self.conn)
        self.assertEqual(self.notion.pages[page]["properties"]["Attention"]["select"]["name"],
                         "✅ En place")
        self.assertIn("Tache #", self.telegram.edits[-1]["text"])

    def test_keep_as_insight_edit_and_no_thanks(self):
        sid = self.sug["opportunity"]["suggestion_id"]
        self.gw.handle_update(click(f"s:{sid}:edit", self.push["message_id"]))
        prompt = self.last()["message_id"]
        self.gw.handle_update(msg("Nouveau titre\nNouveau resume", reply_to=prompt, message_id=30))
        edited = store.get_suggestion(self.conn, sid)
        self.assertEqual((edited["title"], edited["summary"]), ("Nouveau titre", "Nouveau resume"))
        self.gw.handle_update(click(f"s:{sid}:insight", self.push["message_id"]))
        self.assertEqual(store.get_suggestion(self.conn, sid)["decision"], "accepted")
        other = self.sug["convergence"]["suggestion_id"]
        self.gw.handle_update(click(f"s:{other}:no", self.push["message_id"]))
        self.assertEqual(store.get_suggestion(self.conn, other)["status"], "dismissed")

    def test_reply_to_a_suggestion_keeps_its_context(self):
        asked = []

        def spy(conn, question, llm="off"):
            asked.append(question)
            return real_ask(conn, question, llm)

        self.gw.ask_fn = spy
        sid = self.sug["opportunity"]["suggestion_id"]
        result = self.gw.handle_update(msg("Tu peux developper ?",
                                           reply_to=self.push["message_id"], message_id=40))
        self.assertTrue(result.endswith(":anchored"))
        self.assertEqual(asked, [])  # contexte explicite : pas de recherche parasite
        self.assertIn(f"📌 Contexte : {sid}", self.last()["text"])
        self.assertIn("Agents consultes", self.last()["text"])

    def test_pause_until_monday_blocks_pushes_but_mentor_keeps_observing(self):
        self.gw.handle_update(msg("mets le Mentor en pause jusqu'a lundi"))
        self.assertIn("pause", self.last()["text"])
        count = len(self.telegram.sent)
        report = self.gw.tick(NOON + timedelta(hours=8), force=True)
        self.assertTrue(report["silenced"].startswith("pause"))
        self.assertEqual(len(self.telegram.sent), count)
        self.gw.handle_update(msg("mode focus"))
        self.gw.handle_update(msg("/resume"))
        self.assertIn("reactive", self.last()["text"])

    def test_decision_in_notion_is_reflected_in_telegram(self):
        sid = self.sug["opportunity"]["suggestion_id"]
        page = self.notion.page_for(f"suggestion:{sid}", self.conn)
        self.notion.decide(page, "📌 Garder comme insight")
        self.gw.tick(NOON + timedelta(minutes=20), force=True)
        self.assertEqual(store.get_suggestion(self.conn, sid)["decision"], "accepted")
        self.assertIn("traite dans Notion", self.telegram.edits[-1]["text"])

    def test_digest_lists_openable_suggestions(self):
        report = self.gw.tick(NOON + timedelta(hours=7), force=True)
        self.assertEqual(report["digest"], [self.sug["convergence"]["suggestion_id"]])
        self.assertTrue(self.last()["text"].startswith("🧠 Mentor — Digest"))
        self.assertIn("Nexdoor Talent", self.last()["text"])
        self.assertEqual(self.telegram.buttons_of(),
                         [f"s:{self.sug['convergence']['suggestion_id']}:show"])


class RestartTests(GatewayCase):
    def test_polling_offset_survives_restart(self):
        self.telegram.updates = [msg("/help", message_id=7), msg("/help", message_id=8)]
        tg.run(self.gw, once=True)
        self.assertEqual(self.gw.state("offset"), "9")
        sent = len(self.telegram.sent)
        self.conn.close()
        self.conn = vault_db.connect(str(self.path))
        restarted = self.make_gateway(conn=self.conn)
        tg.run(restarted, once=True)  # les updates deja traitees ne sont pas rejouees
        self.assertEqual(len(self.telegram.sent), sent)


class SetupTests(unittest.TestCase):
    def test_several_senders_are_never_guessed(self):
        import telegram_setup

        senders = [{"user_id": 1, "chat_id": 1, "username": "a", "first_name": "A",
                    "chat_type": "private"},
                   {"user_id": 2, "chat_id": 2, "username": "b", "first_name": "B",
                    "chat_type": "private"}]
        answers = iter(["9", "2"])
        with mock.patch("builtins.print"):
            chosen = telegram_setup.choose(senders, input_func=lambda _p: next(answers))
        self.assertEqual(chosen["user_id"], 2)

    def test_env_writer_updates_keys_without_duplicates(self):
        env = _isolation.TMP / "test.env"
        telegram_api.write_env({"TELEGRAM_BOT_TOKEN": "a"}, env)
        telegram_api.write_env({"TELEGRAM_BOT_TOKEN": "b", "TELEGRAM_ALLOWED_USER_ID": "1"}, env)
        self.assertEqual(env.read_text(encoding="utf-8").splitlines(),
                         ["TELEGRAM_BOT_TOKEN=b", "TELEGRAM_ALLOWED_USER_ID=1"])


if __name__ == "__main__":
    unittest.main()
