import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import unittest
from datetime import datetime, timedelta, timezone

import _life_fixtures as fx

import mentor_council as council
import mentor_delivery as delivery
import mentor_proactive as proactive
import mentor_proactive_store as store
import mentor_project_profiles as profiles
import vault_db

PARIS = timezone(timedelta(hours=2))
# Mardi 29/09/2026, 12h00 heure de Paris.
NOON = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)


class Sender:
    chat_id = "42"

    def __init__(self):
        self.pushed = []
        self.digests = []

    def available(self):
        return True

    def send_suggestion(self, suggestion):
        self.pushed.append(suggestion["suggestion_id"])
        return 900 + len(self.pushed)

    def send_digest(self, suggestions):
        self.digests.append([s["suggestion_id"] for s in suggestions])
        return 800


def _two_suggestions(conn):
    proactive.run_pass(conn, now=fx.NOW)
    council.review_shortlist(conn, fx.FakeBackend(), now=fx.NOW, limit=5)
    return {s["suggestion_type"]: s for s in store.list_suggestions(conn)}


class DeliveryPolicyTests(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()
        self.path, self.conn, self.ids, _ = fx.life_db()
        self.sug = _two_suggestions(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_one_push_per_day_then_digest_then_silence(self):
        self.assertEqual(set(self.sug), {"opportunity", "convergence"})
        sender = Sender()
        first = delivery.deliver(self.conn, NOON, sender=sender, tz=PARIS)
        self.assertEqual(first["pushed"], self.sug["opportunity"]["suggestion_id"])  # 0.76 >= 0.75
        self.assertEqual(first["digest"], [])  # digest pas encore du (18h30)
        second = delivery.deliver(self.conn, NOON + timedelta(hours=1), sender=sender, tz=PARIS)
        self.assertIsNone(second["pushed"])  # budget de 1 push/jour
        evening = delivery.deliver(self.conn, NOON + timedelta(hours=7), sender=sender, tz=PARIS)
        self.assertEqual(evening["digest"], [self.sug["convergence"]["suggestion_id"]])
        again = delivery.deliver(self.conn, NOON + timedelta(hours=8), sender=sender, tz=PARIS)
        self.assertEqual((again["pushed"], again["digest"]), (None, []))
        self.assertEqual(len(sender.pushed), 1)
        self.assertEqual(store.get_suggestion(self.conn, sender.pushed[0])["telegram_message_id"],
                         "901")

    def test_quiet_hours_and_pause_send_nothing(self):
        sender = Sender()
        night = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)  # 23h Paris
        self.assertEqual(delivery.deliver(self.conn, night, sender=sender, tz=PARIS)["silenced"],
                         "heures calmes")
        delivery.pause(self.conn, NOON + timedelta(days=6), now=NOON)
        report = delivery.deliver(self.conn, NOON, sender=sender, tz=PARIS)
        self.assertTrue(report["silenced"].startswith("pause"))
        self.assertEqual(sender.pushed, [])
        delivery.resume(self.conn)
        self.assertIsNotNone(delivery.deliver(self.conn, NOON, sender=sender, tz=PARIS)["pushed"])

    def test_silence_when_nothing_is_good_enough(self):
        for suggestion in store.list_suggestions(self.conn):
            delivery.dismiss(self.conn, suggestion["suggestion_id"], "not_now")
        report = delivery.deliver(self.conn, NOON, sender=Sender(), tz=PARIS)
        self.assertEqual(report["silenced"], "rien d'assez bon")

    def test_snooze_is_stored_and_reevaluated_before_resend(self):
        sid = self.sug["opportunity"]["suggestion_id"]
        snoozed = delivery.snooze(self.conn, sid, "monday", now=NOON, tz=PARIS)
        self.assertEqual(snoozed["status"], "snoozed")
        self.assertEqual(snoozed["snoozed_until"], "2026-10-05T07:00:00+00:00")  # lundi 9h Paris
        self.assertEqual(delivery.wake_due(self.conn, NOON + timedelta(days=1)), [])
        self.assertEqual(delivery.wake_due(self.conn, NOON + timedelta(days=7)), [(sid, "ready")])
        other = self.sug["convergence"]["suggestion_id"]
        delivery.snooze(self.conn, other, "tomorrow", now=NOON, tz=PARIS)
        profile = profiles.get_profile(self.conn, self.ids["activity:nexdoor_talent"])
        profile["active"] = False
        profiles.upsert_profile(self.conn, self.ids["activity:nexdoor_talent"], profile)
        self.assertEqual(delivery.wake_due(self.conn, NOON + timedelta(days=2)),
                         [(other, "expired")])

    def test_snooze_choices(self):
        self.assertEqual(delivery.snooze_until("tonight", NOON, PARIS).isoformat(),
                         "2026-09-29T17:00:00+00:00")
        self.assertEqual(delivery.snooze_until("tomorrow", NOON, PARIS).isoformat(),
                         "2026-09-30T07:00:00+00:00")

    def test_dismiss_feeds_fatigue(self):
        sid = self.sug["opportunity"]["suggestion_id"]
        delivery.dismiss(self.conn, sid, "off_topic")
        self.assertEqual(store.get_suggestion(self.conn, sid)["decision"], "dismissed")
        self.assertEqual(council.fatigue(self.conn, self.ids["activity:nexdoor_talent"],
                                         "opportunity", fx.NOW), 1.0)

    def test_task_creation_only_on_decision(self):
        before = self.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
        delivery.deliver(self.conn, NOON, sender=Sender(), tz=PARIS)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0], before)
        sid = self.sug["opportunity"]["suggestion_id"]
        action = delivery.create_task(self.conn, sid)
        self.assertEqual((action["status"], action["proposed_by"]), ("approved", "mentor"))
        self.assertIn(sid, action["description"])
        suggestion = store.get_suggestion(self.conn, sid)
        self.assertEqual(suggestion["status"], "approved")
        self.assertEqual([h["status"] for h in suggestion["status_history"]][-3:],
                         ["accepted", "action_proposed", "approved"])
        self.assertEqual(delivery.create_task(self.conn, sid)["id"], action["id"])  # idempotent

    def test_settings_and_pause_survive_restart(self):
        delivery.pause(self.conn, NOON + timedelta(days=3), now=NOON)
        delivery.snooze(self.conn, self.sug["opportunity"]["suggestion_id"], "tomorrow",
                        now=NOON, tz=PARIS)
        self.conn.close()
        self.conn = vault_db.connect(str(self.path))
        self.assertIsNotNone(delivery.paused_until(self.conn, NOON))
        self.assertEqual(store.get_suggestion(self.conn, self.sug["opportunity"]["suggestion_id"])
                         ["status"], "snoozed")


class NotionTests(unittest.TestCase):
    def setUp(self):
        _isolation.verifier()
        self.path, self.conn, self.ids, _ = fx.life_db()
        self.sug = _two_suggestions(self.conn)
        self.notion = fx.FakeNotion()

    def tearDown(self):
        self.conn.close()

    def test_only_presented_suggestions_are_projected_never_candidates(self):
        delivery.project_suggestions(self.conn, self.notion)
        self.assertEqual(self.notion.created, [])  # rien n'a encore ete presente
        delivery.deliver(self.conn, NOON, sender=Sender(), notion=self.notion, tz=PARIS)
        sid = self.sug["opportunity"]["suggestion_id"]
        page = self.notion.page_for(f"suggestion:{sid}", self.conn)
        self.assertIsNotNone(page)
        props = self.notion.pages[page]["properties"]
        self.assertEqual(props["Kind"]["select"]["name"], "Suggestion")
        self.assertTrue(props["Item"]["title"][0]["text"]["content"].startswith("💡"))
        refs = [p["properties"]["Vault ref"]["rich_text"][0]["text"]["content"]
                for p in self.notion.pages.values()]
        self.assertTrue(all(r.startswith("suggestion:SUG-") for r in refs))
        self.assertEqual(store.get_suggestion(self.conn, sid)["notion_page_id"], page)

    def test_notion_decisions_are_applied_in_vault(self):
        delivery.deliver(self.conn, NOON, sender=Sender(), notion=self.notion, tz=PARIS)
        delivery.deliver(self.conn, NOON + timedelta(hours=7), sender=Sender(),
                         notion=self.notion, tz=PARIS)
        opp = self.sug["opportunity"]["suggestion_id"]
        conv = self.sug["convergence"]["suggestion_id"]
        self.notion.decide(self.notion.page_for(f"suggestion:{opp}", self.conn), "✅ Approuver")
        self.notion.decide(self.notion.page_for(f"suggestion:{conv}", self.conn), "⏰ Reporter")
        changed = delivery.apply_notion_decisions(self.conn, self.notion, now=NOON, tz=PARIS)
        self.assertEqual(sorted(changed), sorted([(opp, "task"), (conv, "snooze")]))
        self.assertEqual(store.get_suggestion(self.conn, opp)["status"], "approved")
        self.assertIsNotNone(store.get_suggestion(self.conn, opp)["evidence"]["task_action_id"])
        self.assertEqual(store.get_suggestion(self.conn, conv)["status"], "snoozed")
        page = self.notion.page_for(f"suggestion:{opp}", self.conn)
        self.assertIsNone(self.notion.pages[page]["properties"]["Décision"]["select"])
        self.assertEqual(self.notion.pages[page]["properties"]["Attention"]["select"]["name"],
                         "✅ En place")
        # Une decision deja traitee n'est pas rejouee.
        self.assertEqual(delivery.apply_notion_decisions(self.conn, self.notion, now=NOON), [])

    def test_ignore_in_notion_dismisses(self):
        delivery.deliver(self.conn, NOON, sender=Sender(), notion=self.notion, tz=PARIS)
        opp = self.sug["opportunity"]["suggestion_id"]
        self.notion.decide(self.notion.page_for(f"suggestion:{opp}", self.conn), "⛔ Ignorer")
        delivery.apply_notion_decisions(self.conn, self.notion, now=NOON)
        self.assertEqual(store.get_suggestion(self.conn, opp)["status"], "dismissed")

    def test_decision_options_added_to_existing_base(self):
        added = delivery.ensure_notion_options(self.notion)
        self.assertEqual(added, ["Décision", "Kind"])
        self.assertIn("⏰ Reporter", self.notion.options["Décision"])
        self.assertIn("✅ Approuver", self.notion.options["Décision"])  # existant conserve
        self.assertEqual(delivery.ensure_notion_options(self.notion), [])  # idempotent


if __name__ == "__main__":
    unittest.main()
