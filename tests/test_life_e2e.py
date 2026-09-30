"""
Scenario de bout en bout (fixtures controlees, Qwen / Telegram / Notion simules) :

EVENT -> candidat -> score -> shortlist -> Qwen -> Agents C -> SUG -> push Telegram
-> 👀 Montre-moi -> ✅ tache (vault.db + Taches Notion apres clic seulement)
-> projection cockpit -> decision Notion sur une autre suggestion -> vault.db
-> redemarrage -> reprise ; utilisateur non autorise rejete ; aucun cloud.
"""

import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import unittest
from datetime import timedelta
from unittest import mock

import _life_fixtures as fx

import mentor_council as council
import mentor_proactive as proactive
import mentor_proactive_store as store
import telegram_gateway as tg
import vault_ask
import vault_db
from test_telegram_gateway import CHAT, NOON, PARIS, USER, click, msg


class EndToEndTests(unittest.TestCase):
    def test_full_life_intelligence_loop(self):
        _isolation.verifier()
        path, conn, ids, sources = fx.life_db()
        telegram, backend = fx.FakeTelegram(), fx.FakeBackend()
        notion, tasks = fx.FakeNotion(), fx.FakeNotion()
        import vault_cloud

        def gateway(c):
            return tg.Gateway(c, telegram, allowed_user=USER, allowed_chat=CHAT, backend=backend,
                              notion=notion, notion_tasks=tasks, tz=PARIS,
                              ask_fn=lambda cn, q, llm="off": vault_ask.ask(
                                  cn, q, mode="consult", llm="off", semantic="off"),
                              inbox=_isolation.TMP / "e2e-inbox.txt")

        with mock.patch.object(vault_cloud, "get_provider", side_effect=AssertionError("cloud")):
            # E. event -> candidat -> Qwen -> Agents C -> suggestion
            report = proactive.run_pass(conn, now=fx.NOW)
            self.assertGreaterEqual(report["events"]["processed"], 1)
            review = council.review_shortlist(conn, backend, now=fx.NOW, limit=5)
            self.assertEqual({r["result"] for r in review}, {"suggestion"})
            opp = next(s for s in store.list_suggestions(conn)
                       if s["suggestion_type"] == "opportunity")
            conv = next(s for s in store.list_suggestions(conn)
                        if s["suggestion_type"] == "convergence")
            self.assertEqual(len(store.list_consultations(conn, suggestion_id=opp["suggestion_id"])),
                             3)
            # Q. rien n'est encore sorti : ni Telegram, ni Notion, ni tache
            self.assertEqual((telegram.sent, notion.created, tasks.created), ([], [], []))

            # F. suggestion -> Telegram (1 push), K. projection Notion de ce qui est presente
            gw = gateway(conn)
            tick = gw.tick(NOON, force=True)
            self.assertEqual(tick["pushed"], opp["suggestion_id"])
            push = telegram.sent[-1]
            self.assertTrue(notion.page_for(f"suggestion:{opp['suggestion_id']}", conn))
            self.assertIsNone(notion.page_for(f"suggestion:{conv['suggestion_id']}", conn))

            # O. utilisateur non autorise
            self.assertEqual(gw.handle_update(click(f"s:{opp['suggestion_id']}:task",
                                                    push["message_id"], user=999)), "ignored")
            self.assertEqual(tasks.created, [])

            # G. Montre-moi, J/M. decision Telegram -> vault.db -> tache (apres clic)
            gw.handle_update(click(f"s:{opp['suggestion_id']}:show", push["message_id"]))
            self.assertIn("Agents consultes", telegram.sent[-1]["text"])
            gw.handle_update(click(f"s:{opp['suggestion_id']}:task", push["message_id"]))
            done = store.get_suggestion(conn, opp["suggestion_id"])
            self.assertEqual(done["status"], "approved")
            self.assertEqual(vault_db.get_action(conn, done["evidence"]["task_action_id"])
                             ["status"], "approved")

            # Digest du soir pour la suggestion de score moyen, puis L. decision Notion
            gw.tick(NOON + timedelta(hours=7), force=True)
            page = notion.page_for(f"suggestion:{conv['suggestion_id']}", conn)
            self.assertIsNotNone(page)
            notion.decide(page, "⏰ Reporter")

            # N. redemarrage : nouvelle connexion, nouvel objet passerelle
            conn.close()
            conn = vault_db.connect(str(path))
            gw = gateway(conn)
            gw.tick(NOON + timedelta(hours=8), force=True)
            self.assertEqual(store.get_suggestion(conn, conv["suggestion_id"])["status"], "snoozed")

            # H/I. le report echu est reevalue puis renvoye ; un rejet nourrit la fatigue
            wake = gw.tick(NOON + timedelta(days=2), force=True)
            self.assertIn((conv["suggestion_id"], "ready"), wake["woken"])
            gw.handle_update(click(f"s:{conv['suggestion_id']}:nope:off_topic",
                                   push["message_id"]))
            self.assertEqual(store.get_suggestion(conn, conv["suggestion_id"])["status"],
                             "dismissed")
            self.assertEqual(council.fatigue(conn, ids["activity:nexdoor_talent"], "convergence",
                                             NOON), 1.0)

            # A/C/D. questions reactives sur le meme Vault
            gw.handle_update(msg("Demande au CFO ce qu'il pense du sourcing data", message_id=70))
            self.assertIn("mode A", telegram.sent[-1]["text"])
            gw.handle_update(msg("Comment exploiter ca pour Nexdoor Talent ?", message_id=71))
            self.assertIn("mode B", telegram.sent[-1]["text"])
        conn.close()


if __name__ == "__main__":
    unittest.main()
