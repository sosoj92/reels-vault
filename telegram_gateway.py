#!/usr/bin/env python3
"""
telegram_gateway.py - Life Gateway : Telegram, interface principale (PC + mobile).

Telegram est l'interface, pas le cerveau :
- REACTIF : texte, vocal (transcrit localement), URL, reponse a un message, commande
  -> retrieval du Vault (vault_ask) -> Qwen local -> Agency Agents (modes A/B)
  -> reponse ;
- PROACTIF : suggestions du Mentor (mentor_delivery : budget, digest, pause,
  heures calmes) -> 💡 message court + boutons -> decision ecrite dans vault.db
  -> cockpit Notion mis a jour.

Securite : whitelist utilisateur ET chat obligatoire (les autres updates sont
ignorees silencieusement), aucune commande shell, aucun acces fichier arbitraire
(seul ajout possible : une URL dans mobile-inbox/inbox.txt), aucune action externe
sans clic, aucun cloud (synthese forcee en local), token jamais journalise.

Usage :
    python telegram_gateway.py            # boucle de polling
    python telegram_gateway.py --once     # un cycle (tests, diagnostic)
"""

import argparse
import json
import logging
import os
import re
import sys
import time as time_module
from datetime import datetime, timedelta, timezone
from pathlib import Path

import telegram_api

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "vault.db"
INBOX = ROOT / "mobile-inbox" / "inbox.txt"
LOG_DIR = ROOT / "logs"
LOCK_FILE = LOG_DIR / "telegram_gateway.lock"
TICK_SECONDS = 60
NOTION_SYNC_SECONDS = 600
SESSION_MINUTES = 30

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
MENTOR_PHRASES = ("qu'est-ce que tu as pour moi", "qu est ce que tu as pour moi",
                  "quoi de neuf mentor", "/mentor")
ADVICE_MARKERS = ("comment exploiter", "qu'en pense", "que pense", "ton avis", "conseil",
                  "strategie", "idee pour", "comment utiliser", "demande a", "demande au",
                  "demande aux", "que ferait")

SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS telegram_messages (
    chat_id       TEXT NOT NULL,
    message_id    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    suggestion_id TEXT,
    question      TEXT,
    agents        TEXT NOT NULL DEFAULT '[]',
    mode          TEXT,
    payload       TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_telegram_messages_recent
    ON telegram_messages(chat_id, created_at);
"""

log = logging.getLogger("telegram_gateway")


def now_utc():
    return datetime.now(timezone.utc)


def fold(text):
    from vault_db import fold as vault_fold

    return " ".join(vault_fold(str(text or "")).replace("’", "'").split())


# ------------------------------------------------------------------ journal

class _Redact(logging.Filter):
    def __init__(self, token):
        super().__init__()
        self.token = token

    def filter(self, record):
        record.msg = telegram_api.redact(record.getMessage(), self.token)
        record.args = ()
        return True


def setup_logging(token):
    LOG_DIR.mkdir(exist_ok=True)
    handler = logging.FileHandler(LOG_DIR / "telegram_gateway.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(_Redact(token))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


# ------------------------------------------------------------------ passerelle

class Gateway:
    def __init__(self, conn, client, *, allowed_user, allowed_chat, backend=None,
                 notion=None, notion_tasks=None, tz=None, transcriber=None, ask_fn=None,
                 inbox=INBOX):
        self.conn = conn
        self.client = client
        self.allowed_user = str(allowed_user or "")
        self.allowed_chat = str(allowed_chat or "")
        self.chat_id = self.allowed_chat
        self.backend = backend
        self.notion = notion
        self.notion_tasks = notion_tasks
        self.tz = tz
        self.transcriber = transcriber
        self.ask_fn = ask_fn
        self.inbox = Path(inbox)
        self.last_tick = 0.0
        self.last_notion = 0.0
        conn.executescript(SCHEMA)
        conn.commit()

    # -------------------------------------------------------------- etat
    def state(self, key, default=None):
        row = self.conn.execute("SELECT value FROM telegram_state WHERE key = ?",
                                (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key, value):
        self.conn.execute("INSERT INTO telegram_state(key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                          (key, None if value is None else str(value)))
        self.conn.commit()

    def remember(self, message_id, kind, *, suggestion_id=None, question=None, agents=(),
                 mode=None, payload=None):
        if message_id is None:
            return
        import mentor_proactive_store as store

        self.conn.execute(
            "INSERT OR REPLACE INTO telegram_messages (chat_id, message_id, kind, suggestion_id, "
            "question, agents, mode, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.chat_id, str(message_id), kind, suggestion_id, question,
             json.dumps(list(agents), ensure_ascii=False), mode,
             json.dumps(payload or {}, ensure_ascii=False), store.now_iso()),
        )
        self.conn.commit()

    def context_of(self, message_id):
        row = self.conn.execute(
            "SELECT * FROM telegram_messages WHERE chat_id = ? AND message_id = ?",
            (self.chat_id, str(message_id)),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["agents"] = json.loads(item["agents"] or "[]")
        item["payload"] = json.loads(item["payload"] or "{}")
        return item

    def last_answer(self):
        import mentor_proactive_store as store

        since = store.now_iso(now_utc() - timedelta(minutes=SESSION_MINUTES))
        row = self.conn.execute(
            "SELECT message_id FROM telegram_messages WHERE chat_id = ? AND kind = 'answer' "
            "AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
            (self.chat_id, since),
        ).fetchone()
        return self.context_of(row[0]) if row else None

    # -------------------------------------------------------------- securite
    def authorized(self, user_id, chat_id):
        return bool(self.allowed_user and self.allowed_chat) \
            and str(user_id) == self.allowed_user and str(chat_id) == self.allowed_chat

    def send(self, text, **kwargs):
        result = self.client.send_message(self.chat_id, text, **kwargs)
        return (result or {}).get("message_id")

    # -------------------------------------------------------------- updates
    def handle_update(self, update):
        message = update.get("message")
        callback = update.get("callback_query")
        if message:
            user = (message.get("from") or {}).get("id")
            chat = (message.get("chat") or {}).get("id")
            if not self.authorized(user, chat):
                log.info("update ignoree : expediteur non autorise")
                return "ignored"
            return self.on_message(message)
        if callback:
            user = (callback.get("from") or {}).get("id")
            chat = ((callback.get("message") or {}).get("chat") or {}).get("id")
            if not self.authorized(user, chat):
                log.info("callback ignore : expediteur non autorise")
                return "ignored"
            return self.on_callback(callback)
        return "ignored"

    def on_message(self, message):
        reply_to = (message.get("reply_to_message") or {}).get("message_id")
        text = (message.get("text") or message.get("caption") or "").strip()
        voice = message.get("voice") or message.get("audio")
        if voice:
            try:
                data = self.client.download_file(voice["file_id"])
                text = (self.transcriber or _default_transcriber)(data)
            except Exception as exc:  # noqa: BLE001
                log.warning("transcription impossible : %s", type(exc).__name__)
                self.send("🎙️ Je n'ai pas pu transcrire ce vocal (transcription locale "
                          "indisponible). Tu peux me l'ecrire ?")
                return "voice_failed"
            self.send(f"🎙️ « {text} »")
        if not text:
            return "empty"
        return self.on_text(text, reply_to=reply_to, message_id=message.get("message_id"))

    # -------------------------------------------------------------- texte
    def on_text(self, text, *, reply_to=None, message_id=None):
        folded = fold(text)
        context = self.context_of(reply_to) if reply_to else None

        if folded in ("/start", "/help", "aide"):
            self.send(HELP_TEXT)
            return "help"
        if context and context["kind"] == "edit_prompt":
            return self.apply_edit(context["suggestion_id"], text)
        if any(folded.startswith(p) or folded == p for p in MENTOR_PHRASES):
            return self.show_pending()
        if folded.startswith("/status"):
            return self.show_status()
        paused = self.pause_command(folded)
        if paused:
            return paused
        urls = URL_RE.findall(text)
        if urls and len(URL_RE.sub("", text).strip()) < 4:
            return self.add_to_inbox(urls)

        adjustment = parse_adjustment(text)
        if adjustment and (context or self.last_answer()):
            base = context if context and context["kind"] == "answer" else self.last_answer()
            if base:
                return self.answer(base["question"], adjustment=adjustment, base=base,
                                   reply_to=message_id)
        suggestion_id = context["suggestion_id"] if context else None
        return self.answer(text, suggestion_id=suggestion_id, reply_to=message_id)

    def pause_command(self, folded):
        import mentor_delivery as delivery

        now = now_utc()
        if folded in ("/resume", "reprends le mentor", "reprends", "fin de pause",
                      "reactive le mentor"):
            delivery.resume(self.conn)
            self.send("▶️ Mentor reactive.")
            return "resumed"
        if "mode focus" in folded or folded == "/focus":
            until = delivery.pause(self.conn, delivery.focus_until(now, self.tz), now=now)
            self.send(f"🎯 Mode focus jusqu'a {self._fmt(until)}. J'observe et je note, "
                      "sans rien t'envoyer.")
            return "focus"
        if folded.startswith("/pause") or ("pause" in folded and "mentor" in folded):
            if "lundi" in folded:
                from datetime import time

                until = delivery.next_weekday(now, 0, time(8, 30), self.tz)
            elif "ce soir" in folded:
                until = delivery.snooze_until("tonight", now, self.tz)
            else:
                hours = re.search(r"(\d+)\s*h", folded)
                until = now + timedelta(hours=int(hours.group(1))) if hours \
                    else delivery.snooze_until("tomorrow", now, self.tz)
            delivery.pause(self.conn, until, now=now)
            self.send(f"⏸️ Mentor en pause jusqu'a {self._fmt(until)}. Il continue "
                      "d'observer et de journaliser, sans rien envoyer.")
            return "paused"
        return None

    def _fmt(self, moment):
        return moment.astimezone(self.tz).strftime("%a %d/%m %H:%M") if self.tz \
            else moment.astimezone().strftime("%a %d/%m %H:%M")

    def add_to_inbox(self, urls):
        # Seule ecriture fichier possible : l'inbox existante, lue par l'ingestion planifiee.
        self.inbox.parent.mkdir(parents=True, exist_ok=True)
        with self.inbox.open("a", encoding="utf-8") as handle:
            for url in urls:
                handle.write(url.strip() + "\n")
        self.send(f"📥 {len(urls)} lien(s) ajoute(s) a l'inbox : ingestion au prochain passage "
                  "planifie.")
        return "inbox"

    # -------------------------------------------------------------- questions
    def answer(self, question, *, suggestion_id=None, adjustment=None, base=None,
               reply_to=None):
        import mentor_council as council

        try:
            self.client.send_chat_action(self.chat_id)
        except telegram_api.TelegramError:
            pass
        full_question = question
        suggestion = None
        if suggestion_id:
            import mentor_proactive_store as store

            suggestion = store.get_suggestion(self.conn, suggestion_id)
            if suggestion:
                full_question = (f"{question}\n(Contexte : suggestion {suggestion_id} - "
                                 f"{suggestion['title']}. {suggestion['summary']})")

        if adjustment:
            prior = base.get("agents") or []
            mode, agents = council.select_for_question(
                self.conn, question,
                include=adjustment.get("include", ()),
                exclude=adjustment.get("exclude", ()),
                replace=adjustment.get("replace"),
            )
            if not adjustment.get("replace"):
                kept = [a for a in prior if a.casefold() not in
                        {e.casefold() for e in adjustment.get("exclude", ())}]
                names = kept + [a["name"] for a in agents if a["name"] not in kept]
                agents = [a for a in (_agent_entry(n, "conserve de la reponse precedente"
                                                   if n in kept else "ajoute a ta demande")
                                      for n in names[:3]) if a]
        else:
            wants_agents = any(marker in fold(question) for marker in ADVICE_MARKERS) \
                or bool(council.agents_in_text(question)) \
                or bool(council.projects_in_text(self.conn, question)) \
                or suggestion is not None
            mode, agents = council.select_for_question(self.conn, question) \
                if wants_agents else (None, [])

        result = self._ask(full_question, llm="off" if agents else "auto")
        lines = [result["answer"]]
        panel = None
        if agents:
            panel = council.answer_with_agents(
                self.conn, self.backend, question, agents, vault_answer=result["answer"],
                sources=result["sources"], mode=mode or "B",
                session_id=f"telegram:{self.chat_id}",
            )
            lines.append("")
            lines.append("🤝 Agents consultes (mode " + (mode or "B") + ") :")
            for agent in agents:
                lines.append(f"• {agent['name']} — {agent['reason']}")
            if panel:
                for opinion in panel["opinions"]:
                    lines.append(f"\n{opinion['agent']} : {opinion['opinion']}")
                if panel.get("synthesis"):
                    lines.append(f"\nSynthese (Qwen local, a verifier) : {panel['synthesis']}")
            else:
                lines.append("(Avis des agents indisponibles : Qwen local injoignable.)")
        message_id = self.send("\n".join(lines), reply_to=reply_to)
        self.remember(message_id, "answer", suggestion_id=suggestion_id, question=question,
                      agents=[a["name"] for a in agents], mode=mode,
                      payload={"sources": [s.get("source_id") for s in result["sources"]]})
        self._publish_question(question, result["sources"])
        return f"answer:{mode or 'ask'}"

    def _ask(self, question, llm):
        if self.ask_fn:
            return self.ask_fn(self.conn, question, llm=llm)
        import vault_ask

        return vault_ask.ask(self.conn, question, mode="consult", llm=llm)

    def _publish_question(self, question, sources):
        import mentor_proactive
        import mentor_proactive_store as store

        if not store.writes_allowed(self.conn):
            return
        try:
            mentor_proactive.publish_question(self.conn, question,
                                              [s["source_id"] for s in sources if
                                               s.get("source_id")])
        except Exception as exc:  # noqa: BLE001 - journal non bloquant
            log.warning("question non publiee : %s", type(exc).__name__)

    # -------------------------------------------------------------- suggestions
    def send_suggestion(self, suggestion):
        """Push proactif : message court + 3 boutons."""
        sid = suggestion["suggestion_id"]
        text = f"💡 {suggestion['title']}\n{suggestion['summary']}"
        message_id = self.send(text, buttons=[[
            {"text": "👀 Montre-moi", "callback_data": f"s:{sid}:show"},
            {"text": "⏰ Plus tard", "callback_data": f"s:{sid}:later"},
            {"text": "👎 Pas pertinent", "callback_data": f"s:{sid}:nope"},
        ]])
        self.remember(message_id, "suggestion", suggestion_id=sid)
        return message_id

    def send_digest(self, suggestions):
        import mentor_proactive_store as store

        lines = ["🧠 Mentor — Digest", ""]
        buttons = []
        for index, suggestion in enumerate(suggestions, start=1):
            project = _project_name(self.conn, suggestion.get("project_id"))
            lines.append(f"{index}. {project} — {suggestion['title']}")
            buttons.append([{"text": f"{index}. Ouvrir", "callback_data":
                             f"s:{suggestion['suggestion_id']}:show"}])
        message_id = self.send("\n".join(lines), buttons=buttons)
        for suggestion in suggestions:
            if suggestion["status"] == "ready":
                store.transition_suggestion(self.conn, suggestion["suggestion_id"], "sent",
                                            decided_by="mentor")
        self.remember(message_id, "digest",
                      payload={"suggestions": [s["suggestion_id"] for s in suggestions]})
        return message_id

    def available(self):
        return self.client.configured() and bool(self.chat_id)

    def show_pending(self):
        import mentor_proactive_store as store

        pending = store.list_suggestions(self.conn, ("ready", "sent", "opened", "snoozed"))
        if not pending:
            self.send("🧠 Rien d'assez bon pour l'instant. Je continue d'observer.")
            return "pending:0"
        lines = ["🧠 Suggestions en attente :"]
        buttons = []
        for index, suggestion in enumerate(pending[:8], start=1):
            project = _project_name(self.conn, suggestion.get("project_id"))
            extra = f" (reportee jusqu'au {suggestion['snoozed_until'][:16]})" \
                if suggestion["status"] == "snoozed" else ""
            lines.append(f"{index}. {project} — {suggestion['title']}{extra}")
            buttons.append([{"text": f"{index}. Ouvrir",
                             "callback_data": f"s:{suggestion['suggestion_id']}:show"}])
        self.send("\n".join(lines), buttons=buttons)
        return f"pending:{len(pending)}"

    def show_status(self):
        import mentor_delivery as delivery
        import mentor_proactive_store as store

        paused = delivery.paused_until(self.conn, now_utc()) if store.schema_present(self.conn) \
            else None
        counts = {}
        for suggestion in store.list_suggestions(self.conn):
            counts[suggestion["status"]] = counts.get(suggestion["status"], 0) + 1
        self.send("🧠 Etat du Mentor\n"
                  f"Pause : {self._fmt(paused) if paused else 'non'}\n"
                  f"Suggestions : {json.dumps(counts, ensure_ascii=False) if counts else 'aucune'}")
        return "status"

    def detail_text(self, suggestion):
        evidence = suggestion["evidence"]
        project = (evidence.get("project") or {}).get("name") or \
            _project_name(self.conn, suggestion.get("project_id"))
        lines = [f"💡 {suggestion['title']}  ({suggestion['suggestion_id']})",
                 f"Projet : {project}",
                 f"Hypothese : {evidence.get('hypothesis') or suggestion['summary']}",
                 f"Confiance : {evidence.get('confidence', '?')}", "", "Sources :"]
        for source in (evidence.get("sources") or [])[:4]:
            lines.append(f"• {source.get('title')} ({source.get('ref')})"
                         + (f" {source['url']}" if source.get("url") else ""))
        lines.append("")
        lines.append("Agents consultes :")
        objections = []
        for agent in evidence.get("agents") or []:
            lines.append(f"• {agent['agent']} : {agent.get('value_score', '?')}/5 "
                         f"({agent.get('verdict', '?')}) — {agent.get('proposed_action') or ''}")
            if agent.get("main_objection"):
                objections.append(f"• {agent['agent']} : {agent['main_objection']}"
                                  + (" [BLOQUANTE]" if agent.get("blocking_objection") else ""))
        if objections:
            lines.append("")
            lines.append("Objections :")
            lines.extend(objections)
        if evidence.get("disagreement"):
            lines.append("")
            lines.append("⚠️ Desaccord entre agents : "
                         + json.dumps(evidence["disagreement"]["scores"], ensure_ascii=False))
        actions = evidence.get("actions") or []
        if actions:
            lines.append("")
            lines.append("Actions proposees :")
            lines.extend(f"{i}. {a}" for i, a in enumerate(actions[:2], start=1))
        return "\n".join(lines)

    # -------------------------------------------------------------- boutons
    def on_callback(self, callback):
        import mentor_delivery as delivery
        import mentor_proactive_store as store

        data = callback.get("data") or ""
        message = callback.get("message") or {}
        message_id = message.get("message_id")
        parts = data.split(":")
        if len(parts) < 3 or parts[0] != "s":
            self.client.answer_callback(callback["id"])
            return "ignored"
        sid, action, arg = parts[1], parts[2], (parts[3] if len(parts) > 3 else None)
        suggestion = store.get_suggestion(self.conn, sid)
        if suggestion is None:
            self.client.answer_callback(callback["id"], "Suggestion introuvable.")
            return "missing"
        now = now_utc()
        result = action
        try:
            if action == "show":
                delivery.open_detail(self.conn, sid)
                suggestion = store.get_suggestion(self.conn, sid)
                detail_id = self.send(self.detail_text(suggestion), buttons=[
                    [{"text": "✅ Creer la tache", "callback_data": f"s:{sid}:task"},
                     {"text": "📌 Garder comme insight", "callback_data": f"s:{sid}:insight"}],
                    [{"text": "✏️ Modifier", "callback_data": f"s:{sid}:edit"},
                     {"text": "❌ Non merci", "callback_data": f"s:{sid}:no"}],
                ])
                store.record_notification(self.conn, kind="detail", channel="telegram",
                                          suggestion_ids=[sid], chat_id=self.chat_id,
                                          message_id=detail_id)
                self.remember(detail_id, "detail", suggestion_id=sid)
            elif action == "later" and not arg:
                self.client.edit_message(self.chat_id, message_id, message.get("text", ""),
                                         buttons=[[{"text": label, "callback_data":
                                                    f"s:{sid}:later:{key}"}
                                                   for key, label in
                                                   delivery.SNOOZE_CHOICES.items()]])
            elif action == "later":
                updated = delivery.snooze(self.conn, sid, arg, now=now, tz=self.tz)
                self._close(message, f"⏰ Reportee : {delivery.SNOOZE_CHOICES[arg]} "
                                     f"({updated['snoozed_until'][:16]} UTC). Je la "
                                     "reevaluerai avant de te la renvoyer.")
            elif action == "nope" and not arg:
                self.client.edit_message(self.chat_id, message_id, message.get("text", ""),
                                         buttons=[[{"text": label, "callback_data":
                                                    f"s:{sid}:nope:{key}"}]
                                                  for key, label in
                                                  delivery.DISMISS_REASONS.items()])
            elif action == "nope":
                delivery.dismiss(self.conn, sid, arg)
                self._close(message, f"👎 Note : {delivery.DISMISS_REASONS[arg]}. "
                                     "J'en tiens compte pour la suite.")
            elif action == "task":
                task = delivery.create_task(self.conn, sid)
                page = delivery.push_task(self.conn, task["id"], self.notion_tasks)
                self._close(message, f"✅ Tache #{task['id']} creee : {task['title']}"
                                     + (" (ajoutee aux Taches Notion)" if page else ""))
            elif action == "insight":
                delivery.keep_as_insight(self.conn, sid)
                self._close(message, "📌 Gardee comme insight.")
            elif action == "edit":
                prompt_id = self.send(f"✏️ Reponds a ce message avec le nouveau texte de "
                                      f"{sid} (premiere ligne = titre, suite = resume).")
                self.remember(prompt_id, "edit_prompt", suggestion_id=sid)
            elif action == "no":
                delivery.dismiss(self.conn, sid, "no_thanks")
                self._close(message, "❌ Ecartee.")
            else:
                result = "ignored"
        except delivery.DeliveryError as exc:
            self.client.answer_callback(callback["id"], str(exc)[:150])
            return "refused"
        self.client.answer_callback(callback["id"])
        self._sync_notion_row()
        return f"callback:{result}"

    def _close(self, message, note):
        """Retire les boutons d'un message traite et y note la decision."""
        text = (message.get("text") or "").split("\n\n— ")[0]
        try:
            self.client.edit_message(self.chat_id, message.get("message_id"),
                                     f"{text}\n\n— {note}")
        except telegram_api.TelegramError:
            self.send(note)

    def apply_edit(self, suggestion_id, text):
        import mentor_proactive_store as store

        title, _, summary = text.strip().partition("\n")
        store.update_suggestion_text(self.conn, suggestion_id, title=title.strip()[:90],
                                     summary=summary.strip()[:320] or None)
        self.send(f"✏️ {suggestion_id} mise a jour.")
        self._sync_notion_row()
        return "edited"

    def _sync_notion_row(self):
        import mentor_delivery as delivery

        if self.notion is not None and self.notion.configured():
            try:
                delivery.project_suggestions(self.conn, self.notion)
            except Exception as exc:  # noqa: BLE001 - Notion ne bloque jamais Telegram
                log.warning("projection Notion impossible : %s", type(exc).__name__)

    # -------------------------------------------------------------- tick proactif
    def tick(self, now=None, *, force=False):
        import mentor_delivery as delivery
        import mentor_proactive_store as store

        clock = time_module.monotonic()
        if not force and clock - self.last_tick < TICK_SECONDS:
            return None
        self.last_tick = clock
        if not store.writes_allowed(self.conn) or not store.schema_present(self.conn):
            return {"status": "mentor_inactive"}
        report = {}
        if self.notion is not None and self.notion.configured() and \
                (force or clock - self.last_notion >= NOTION_SYNC_SECONDS):
            self.last_notion = clock
            try:
                for sid, decision in delivery.apply_notion_decisions(
                        self.conn, self.notion, now=now, tz=self.tz, tasks=self.notion_tasks):
                    self.reflect_notion(sid, decision)
                    report.setdefault("notion_decisions", []).append((sid, decision))
            except Exception as exc:  # noqa: BLE001
                log.warning("decisions Notion illisibles : %s", type(exc).__name__)
        report.update(delivery.deliver(self.conn, now, sender=self, notion=self.notion,
                                       tz=self.tz))
        return report

    def reflect_notion(self, suggestion_id, decision):
        import mentor_proactive_store as store

        suggestion = store.get_suggestion(self.conn, suggestion_id)
        labels = {"task": "✅ traite dans Notion : tache creee",
                  "insight": "📌 traite dans Notion : garde comme insight",
                  "snooze": "⏰ traite dans Notion : reporte",
                  "dismiss": "⛔ traite dans Notion : ignore"}
        if suggestion and suggestion.get("telegram_message_id"):
            try:
                self.client.edit_message(
                    self.chat_id, int(suggestion["telegram_message_id"]),
                    f"💡 {suggestion['title']}\n{suggestion['summary']}\n\n— {labels[decision]}")
            except telegram_api.TelegramError:
                pass


def _agent_entry(name, reason):
    import mentor_council as council

    agent = council.registry().get(name.casefold())
    return {"name": agent.name, "slug": agent.slug, "reason": reason, "agent": agent} \
        if agent else None


def _project_name(conn, project_id):
    if project_id is None:
        return "Sans projet"
    row = conn.execute("SELECT name FROM intelligence_entities WHERE id = ?",
                       (project_id,)).fetchone()
    return row[0] if row else f"Projet #{project_id}"


def parse_adjustment(text):
    """« ajoute le CFO », « sans le Growth Hacker », « demande plutot au Recruitment Agent »."""
    import mentor_council as council

    folded = fold(text)
    agents = council.agents_in_text(text)
    if not agents:
        return None
    names = [a.name for a in agents]
    if re.match(r"^(demande |pose la question )?plutot (a|au|aux|a la) ", folded) or \
            folded.startswith("plutot "):
        return {"replace": names}
    if re.match(r"^(ajoute|rajoute|avec aussi|et aussi|ajoute aussi)\b", folded):
        return {"include": names}
    if re.match(r"^(sans|retire|enleve)\b", folded):
        return {"exclude": names}
    return None


def _default_transcriber(data):
    import vault_transcribe

    return vault_transcribe.transcribe_bytes(data)


HELP_TEXT = (
    "🧠 Life Intelligence\n"
    "• Pose une question : je cherche dans ton Vault (Qwen local).\n"
    "• « Demande au Growth Hacker… » : un agent nomme (mode A).\n"
    "• « Comment exploiter ca pour Nexdoor Talent ? » : je choisis 1 a 3 agents (mode B).\n"
    "• Reponds « ajoute le CFO », « sans le Growth Hacker », « demande plutot au "
    "Recruitment Agent ».\n"
    "• Envoie un vocal ou un lien (ajoute a l'inbox).\n"
    "• /mentor ou « qu'est-ce que tu as pour moi ? » : suggestions en attente.\n"
    "• « mets le Mentor en pause jusqu'a lundi », « mode focus », /resume, /status.\n"
    "Aucune action externe sans ton clic."
)


# ------------------------------------------------------------------ demarrage

def _single_instance():
    LOG_DIR.mkdir(exist_ok=True)
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid) \
                if os.name == "nt" else None
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return False
        except (ValueError, OSError, AttributeError):
            pass
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def build(db=DEFAULT_DB):
    telegram_api.load_env()
    # Aucun fallback cloud : la synthese de vault_ask reste locale.
    os.environ["VAULT_ASK_SYNTHESIZER"] = "local"
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    user = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "")
    chat = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "")
    if not (token and user and chat):
        # Code 0 : la tache planifiee ne boucle pas en redemarrage tant que rien n'est configure.
        print("Telegram non configure : lance d'abord  python telegram_setup.py", file=sys.stderr)
        raise SystemExit(0)
    setup_logging(token)
    import mentor_council
    import mentor_delivery
    from vault_db import connect
    from vault_notion_cockpit import NotionCockpitClient

    conn = connect(str(db))
    return Gateway(conn, telegram_api.TelegramClient(token), allowed_user=user,
                   allowed_chat=chat, backend=mentor_council.OllamaBackend(),
                   notion=NotionCockpitClient(), notion_tasks=mentor_delivery.tasks_client())


def run(gateway, *, once=False):
    offset = gateway.state("offset")
    offset = int(offset) if offset else None
    backoff = 5
    while True:
        try:
            updates = gateway.client.get_updates(offset=offset, timeout=1 if once else 25)
            backoff = 5
        except telegram_api.TelegramError as exc:
            log.warning("polling : %s", exc)
            if once:
                return
            time_module.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue
        for update in updates or []:
            try:
                gateway.handle_update(update)
            except Exception as exc:  # noqa: BLE001 - une update ne tue pas la passerelle
                log.error("update en erreur : %s", type(exc).__name__)
            offset = update["update_id"] + 1
            gateway.set_state("offset", offset)
        try:
            gateway.tick(force=once)
        except Exception as exc:  # noqa: BLE001
            log.error("tick proactif en erreur : %s", type(exc).__name__)
        if once:
            return


def main(argv=None):
    parser = argparse.ArgumentParser(description="Life Gateway Telegram")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if not args.once and not _single_instance():
        print("Une passerelle tourne deja.")
        return 0
    gateway = build(args.db)
    log.info("passerelle demarree")
    try:
        run(gateway, once=args.once)
    finally:
        if not args.once:
            LOCK_FILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
