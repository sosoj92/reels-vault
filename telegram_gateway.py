#!/usr/bin/env python3
"""
telegram_gateway.py - Life Gateway : Telegram, interface principale (PC + mobile).

Telegram est l'interface, pas le cerveau :
- REACTIF : texte, vocal (transcrit localement), URL, reponse a un message, commande
  -> retrieval du Vault (vault_ask) -> Qwen local -> Agency Agents (modes A/B)
  -> reponse ; une longue demande d'analyse passe en CONSULTATION (Vault en appui,
  question complete au LLM via la tache complex du routeur) ; toute reponse longue
  est decoupee en plusieurs messages, jamais tronquee ;
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
INBOX = ROOT / "mobile-inbox" / "inbox.txt"          # file video du pipeline existant
TELEGRAM_FILES = ROOT / "inbox" / "telegram"            # fichiers recus par Telegram (local)
LOG_DIR = ROOT / "logs"
LOCK_FILE = LOG_DIR / "telegram_gateway.lock"
TICK_SECONDS = 60
NOTION_SYNC_SECONDS = 600
SESSION_MINUTES = 30

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
MENTOR_PHRASES = ("qu'est-ce que tu as pour moi", "qu est ce que tu as pour moi",
                  "quoi de neuf mentor", "/mentor")
# Expressions qui renvoient au contexte actif (« ca », « qu'en penses-tu »...).
ANAPHORA_RE = re.compile(
    r"(?<![a-z])(ca|ceci|cela|ce contenu|cette idee|cette suggestion|cette source|cette piste|"
    r"cette opportunite|cette analyse|analyse cette|ce point|ce risque|ces risques|"
    r"ce sujet|la dessus|la-dessus|qu'en penses|qu en penses|qu'en pense|qu en pense|"
    r"ce qu'il en pense|ce qu'elle en pense|ce qu'il pense|ce qu'elle pense|ton avis|"
    r"son avis|comment exploiter|comment l'exploiter|developpe|detaille)(?![a-z])")
ACTIVE_CONTEXT_MINUTES = 60
ADVICE_MARKERS = ("comment exploiter", "qu'en pense", "que pense", "ton avis", "conseil",
                  "strategie", "idee pour", "comment utiliser", "demande a", "demande au",
                  "demande aux", "que ferait", "analyse", "compare", "propose", "recommande",
                  "optimise", "optimiser")

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

    # -------------------------------------------------------------- contexte actif
    def set_active(self, kind, **data):
        import mentor_proactive_store as store

        self.set_state(f"active_context:{self.chat_id}", json.dumps(
            {"kind": kind, "at": store.now_iso(), **data}, ensure_ascii=False))

    def _fresh(self, key):
        raw = self.state(f"{key}:{self.chat_id}")
        if not raw:
            return None
        item = json.loads(raw)
        at = datetime.fromisoformat(item["at"])
        if now_utc() - at > timedelta(minutes=ACTIVE_CONTEXT_MINUTES):
            return None
        return item

    def active(self):
        return self._fresh("active_context")

    def open_suggestion(self, suggestion_id=None, *, close=False):
        """Suggestion ouverte dans cette conversation (prioritaire sur le reste du contexte)."""
        import mentor_proactive_store as store

        if close:
            self.set_state(f"open_suggestion:{self.chat_id}", None)
            return None
        if suggestion_id:
            self.set_state(f"open_suggestion:{self.chat_id}", json.dumps(
                {"suggestion_id": suggestion_id, "at": store.now_iso()}))
            return suggestion_id
        item = self._fresh("open_suggestion")
        if not item:
            return None
        suggestion = store.get_suggestion(self.conn, item["suggestion_id"])
        if suggestion is None or suggestion["status"] in ("dismissed", "expired"):
            return None
        return item["suggestion_id"]

    def resolve_context(self, text, reply_context):
        """
        Priorite : 1. message auquel Rosy repond ; 2. suggestion ouverte / derniere
        consultee ; 3. dernier contenu discute ; 4. sinon retrieval classique (None).
        Le contexte actif n'est utilise que si la phrase y renvoie (« ca »...).
        """
        if reply_context:
            if reply_context.get("suggestion_id"):
                return {"kind": "suggestion", "suggestion_id": reply_context["suggestion_id"],
                        "why": "message repondu"}
            sources = (reply_context.get("payload") or {}).get("sources") or []
            if sources:
                return {"kind": "sources", "source_ids": sources,
                        "question": reply_context.get("question"), "why": "message repondu"}
        if not refers_to_context(text):
            return None
        # 2. suggestion ouverte, 3. contexte actif du dernier echange, 4. dernier contenu.
        opened = self.open_suggestion()
        if opened:
            return {"kind": "suggestion", "suggestion_id": opened, "why": "suggestion ouverte"}
        active = self.active()
        if not active:
            return None
        return {**active, "why": "contexte actif"}

    # -------------------------------------------------------------- securite
    def authorized(self, user_id, chat_id):
        return bool(self.allowed_user and self.allowed_chat) \
            and str(user_id) == self.allowed_user and str(chat_id) == self.allowed_chat

    def send(self, text, **kwargs):
        ids = self.send_parts(text, **kwargs)
        return ids[-1] if ids else None

    def send_parts(self, text, *, buttons=None, reply_to=None, placeholder=None):
        """
        Envoie un texte de toute longueur : decoupe sans perte au-dela de la limite
        Telegram (jamais de troncature silencieuse). Le premier morceau remplace le
        message d'attente s'il y en a un ; les boutons vont sur le dernier morceau.
        Retourne les message_id de tous les morceaux.
        """
        chunks = telegram_api.split_text(text)
        ids = []
        for index, chunk in enumerate(chunks):
            last = index == len(chunks) - 1
            if index == 0 and placeholder is not None:
                try:
                    self.client.edit_message(self.chat_id, placeholder, chunk,
                                             buttons=buttons if last else None)
                    ids.append(placeholder)
                    continue
                except telegram_api.TelegramError:
                    pass
            result = self.client.send_message(self.chat_id, chunk,
                                              buttons=buttons if last else None,
                                              reply_to=reply_to if index == 0 else None)
            ids.append((result or {}).get("message_id"))
        return ids

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
        document = message.get("document")
        photos = message.get("photo") or []
        if document or photos:
            return self.receive_file(message, document, photos, caption=text)
        if fold(text).startswith("/save"):
            replied = message.get("reply_to_message") or {}
            target = (replied.get("text") or replied.get("caption") or "").strip()
            if not target:
                self.send("Reponds a un message avec /save pour l'ajouter au Vault.")
                return "save_missing"
            return self.save_text(target, message.get("message_id"))
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
            import vault_ingestion as ingestion

            if ingestion.wants_to_save(text):
                return self.save_text(text, message.get("message_id"), source_type="voice")
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
        if folded.startswith("/inbox"):
            return self.show_inbox()
        if folded.startswith("/project"):
            return self.attach_project(text, context)
        paused = self.pause_command(folded)
        if paused:
            return paused
        urls = URL_RE.findall(text)
        if urls:
            rest = URL_RE.sub("", text).strip()
            import vault_ingestion as ingestion

            if len(rest) < 4 or ingestion.wants_to_save(rest) or \
                    __import__("mentor_council").projects_in_text(self.conn, rest):
                return self.save_urls(urls, rest, message_id)
        import vault_ingestion as ingestion

        if ingestion.wants_to_save(text):
            return self.save_text(text, message_id)

        adjustment = parse_adjustment(text)
        if adjustment and (context or self.last_answer()):
            base = context if context and context["kind"] == "answer" else self.last_answer()
            if base:
                return self.answer(base["question"], adjustment=adjustment, base=base,
                                   reply_to=message_id)
        anchor = self.resolve_context(text, context)
        return self.answer(text, anchor=anchor, reply_to=message_id)

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

    # -------------------------------------------------------------- ingestion
    def _router(self):
        return self.backend if hasattr(self.backend, "chat_json") else None

    def save_urls(self, urls, note, message_id):
        import vault_ingestion as ingestion

        results = []
        for index, url in enumerate(urls):
            key = f"{message_id}:{index}" if message_id is not None else None
            results.append(ingestion.ingest_url(
                self.conn, url, channel="telegram", telegram_message_id=key, note=note,
                video_queue=self.inbox, router=self._router()))
        for result in results:
            self.confirm_ingestion(result)
        return "inbox"

    def save_text(self, text, message_id, source_type="note"):
        import vault_ingestion as ingestion

        result = ingestion.ingest_note(self.conn, text, channel="telegram",
                                       telegram_message_id=None if message_id is None
                                       else str(message_id), source_type=source_type,
                                       router=self._router())
        self.confirm_ingestion(result)
        return f"saved:{result['status']}"

    def receive_file(self, message, document, photos, caption=""):
        import vault_ingestion as ingestion

        if document:
            file_id, name = document["file_id"], document.get("file_name") or "document"
        else:
            best = max(photos, key=lambda p: p.get("file_size") or 0)
            file_id, name = best["file_id"], f"photo-{message.get('message_id')}.jpg"
        try:
            data = self.client.download_file(file_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("fichier non telecharge : %s", type(exc).__name__)
            self.send("📎 Je n'ai pas pu recuperer ce fichier (trop gros ?). Depose-le dans "
                      "inbox\\files sur le PC.")
            return "file_failed"
        folder = TELEGRAM_FILES / datetime.now().strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(name).name)[:120] or "fichier"
        target = folder / f"{message.get('message_id')}-{safe}"
        target.write_bytes(data)
        result = ingestion.ingest_file(self.conn, target, channel="telegram",
                                       original_filename=name, caption=caption or None,
                                       telegram_message_id=str(message.get("message_id")),
                                       router=self._router())
        self.confirm_ingestion(result)
        return f"file:{result['status']}"

    def confirm_ingestion(self, result):
        status = result.get("status")
        if status == "rejected_secret":
            self.send("⚠️ Ce contenu semble contenir un secret (mot de passe, cle, IBAN…). Je ne "
                      "l'enregistre pas et ne l'envoie a aucune IA. Pense a le supprimer de "
                      "Telegram.")
            return None
        if status == "duplicate":
            self.send("♻️ Deja dans ton Vault.")
            return None
        if status == "queued_video":
            lines = ["✅ Ajoute au Vault", "🎬 Video (transcription et analyse au prochain "
                                           "passage)"]
            if result.get("project"):
                lines.append(f"💼 {result['project']}")
            message_id = self.send("\n".join(lines))
            self.remember(message_id, "ingestion", payload={"item_id": result.get("item_id")})
            return message_id
        labels = {"article": "📰 Article", "note": "📝 Note", "voice": "🎙️ Note vocale",
                  "pdf": "📄 PDF", "document": "📄 Document", "image": "🖼️ Image",
                  "file": "📎 Fichier"}
        lines = ["✅ Ajoute au Vault", labels.get(result.get("kind"), "📎 Contenu")]
        classification = result.get("classification") or {}
        themes = [t for t in [classification.get("category")] + classification.get("subthemes", [])
                  if t]
        if themes:
            lines.append("🧠 " + ", ".join(themes[:3]))
        if classification.get("tools"):
            lines.append("🛠️ " + ", ".join(classification["tools"][:5]))
        if result.get("project"):
            lines.append(f"💼 {result['project']}")
        elif result.get("proposed_project"):
            lines.append(f"💼 propose : {result['proposed_project']} (a valider)")
        if not classification:
            lines.append("⏳ Classement en attente (moteur LLM indisponible)")
        if result.get("sensitivity") == "sensitive":
            lines.append("🔒 Sensible : classe en local uniquement. La prochaine fois, depose "
                         "plutot ce type de contenu dans inbox\\sensitive sur le PC.")
        message_id = self.send("\n".join(lines))
        self.remember(message_id, "ingestion", payload={
            "item_id": result.get("item_id"), "source_id": result.get("source_id")})
        if result.get("source_id"):
            self.set_active("sources", source_ids=[result["source_id"]], question=None)
            self.set_state(f"last_ingestion:{self.chat_id}", json.dumps({"source_id": result["source_id"],
                                                         "item_id": result.get("item_id")}))
        return message_id

    def show_inbox(self):
        import vault_ingestion as ingestion

        items = ingestion.recent(self.conn, limit=10)
        if not items:
            self.send("📥 Rien d'ajoute recemment.")
            return "inbox:0"
        icons = {"url_video": "🎬", "url_article": "📰", "note": "📝", "pdf": "📄",
                 "document": "📄", "image": "🖼️", "file": "📎"}
        lines = ["📥 Derniers ajouts :"]
        for item in items:
            title = item.get("title") or item.get("original_filename") or item.get("original_url")
            if item["status"] == "rejected_secret":
                title = "(secret detecte : non enregistre)"
            lines.append(f"{icons.get(item['kind'], '•')} {str(title)[:70]} — {item['status']}")
        self.send("\n".join(lines))
        return f"inbox:{len(items)}"

    def attach_project(self, text, context):
        import mentor_council as council
        import mentor_project_links as links

        name = re.sub(r"^/project(@\w+)?", "", text, flags=re.I).strip()
        projects = council.projects_in_text(self.conn, name)
        target = None
        if context and context.get("kind") == "ingestion":
            target = context["payload"].get("source_id")
        if target is None:
            last = json.loads(self.state(f"last_ingestion:{self.chat_id}") or "{}")
            target = last.get("source_id")
        if len(projects) != 1 or target is None:
            self.send("Usage : reponds a une confirmation d'ajout avec  /project Nexdoor Talent")
            return "project_usage"
        links.record_decision(self.conn, source_id=target, entity_id=projects[0],
                              decision="approve", level="high", confidence=1.0,
                              reason="rattachement manuel par Rosy (/project)")
        self.send(f"💼 Rattache a {_project_name(self.conn, projects[0])}.")
        return "project_linked"

    # -------------------------------------------------------------- questions
    def project_consult_context(self, project_ids):
        """Contexte projet compact transmis aux agents lors d'une consultation directe."""
        if not project_ids:
            return None
        import mentor_project_profiles as profiles
        import vault_ask

        projects = []
        for project_id in list(dict.fromkeys(project_ids))[:2]:
            profile = profiles.get_profile(self.conn, project_id) or {}
            view = vault_ask.project_view(self.conn, project_id) or {}
            entity = view.get("entity") or {}
            topics = []
            for item in profile.get("core_topics") or []:
                topic = item.get("topic") if isinstance(item, dict) else str(item)
                if topic:
                    topics.append(topic)
            projects.append({
                "id": project_id,
                "name": profile.get("project_name") or entity.get("name")
                        or _project_name(self.conn, project_id),
                "description": profile.get("description"),
                "core_topics": topics[:8],
                "tasks": [
                    {k: task.get(k) for k in ("id", "title", "status", "due_date")}
                    for task in (view.get("tasks") or [])[:5]
                ],
                "suggestions": [
                    {k: sug.get(k) for k in ("suggestion_id", "title", "status")}
                    for sug in (view.get("suggestions") or [])[-5:]
                ],
            })
        return {"projets": projects} if projects else None

    def consult(self, question, *, reply_to=None, project_ids=()):
        """
        Longue demande d'analyse : le Vault fournit des preuves (requete compacte, sans
        LLM), puis UN appel LLM recoit la question complete (tache complex du routeur).
        Les agents ne sont ajoutes que s'ils sont nommes, via le paquet portable.
        """
        import vault_ask
        import vault_orchestrator as orchestrator

        placeholder = self.send("🧠 Analyse en cours…", reply_to=reply_to)
        evidence = self._ask(question, llm="off")
        # Meme paquet portable que ChatGPT / CLI : playbooks des agents NOMMES seulement.
        packet = orchestrator.build_consultation_packet(question, auto_select=False)
        playbooks = {p["name"]: p["playbook"] for p in packet["agent_playbooks"]}
        agents = [{"name": a["name"], "role": a["description"],
                   "playbook": playbooks.get(a["name"])} for a in packet["selected_agents"]]
        router = self.backend if hasattr(self.backend, "complete_text") else None
        result = vault_ask.consult(question, evidence=evidence, router=router, agents=agents,
                                   project_context=self.project_consult_context(project_ids))
        ids = self.send_parts(result["answer"], reply_to=reply_to, placeholder=placeholder)
        source_ids = [s.get("source_id") for s in result["sources"] if s.get("source_id")]
        for message_id in ids:
            self.remember(message_id, "answer", question=question,
                          agents=[a["name"] for a in agents], mode="consult",
                          payload={"sources": source_ids, "anchor": None})
        if source_ids:
            self.set_active("sources", source_ids=source_ids, question=question)
        self._publish_question(question, result["sources"])
        return "answer:consult"

    def answer(self, question, *, anchor=None, adjustment=None, base=None, reply_to=None):
        import mentor_council as council
        import vault_ask

        try:
            self.client.send_chat_action(self.chat_id)
        except telegram_api.TelegramError:
            pass
        if adjustment and base and not anchor:
            anchor = base.get("payload", {}).get("anchor")
        grounded = self.grounding(anchor) if anchor else None
        mentioned_projects = council.projects_in_text(self.conn, question)
        project_ids = [grounded["project_id"]] if grounded and grounded.get("project_id") else []
        for pid in mentioned_projects:
            if pid not in project_ids:
                project_ids.append(pid)
        if grounded:
            grounded["agent_context"]["projets_mentionnes"] = [
                _project_name(self.conn, pid) for pid in project_ids]

        if not adjustment and not grounded and vault_ask.is_consultation(question):
            return self.consult(question, reply_to=reply_to, project_ids=project_ids)

        if adjustment:
            prior = base.get("agents") or []
            mode, agents = council.select_for_question(
                self.conn, question,
                include=adjustment.get("include", ()),
                exclude=adjustment.get("exclude", ()),
                replace=adjustment.get("replace"),
                project_ids=project_ids,
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
                or bool(mentioned_projects) \
                or grounded is not None
            mode, agents = council.select_for_question(
                self.conn, question, project_ids=project_ids) if wants_agents else (None, [])

        if grounded:
            # Contexte explicite : aucune recherche parasite, les agents recoivent ce contexte.
            result = {"answer": grounded["text"], "sources": grounded["sources"]}
        else:
            result = self._ask(question, llm="off" if agents else "auto")
            if result.get("route") == "project_view" and mode != "A":
                # Question factuelle sur un projet : la vue du Vault suffit, pas d'agents.
                mode, agents = None, []
            if result.get("route") == "project_view":
                self._anchor_project(result["project_id"])
        panel = None
        placeholder = None
        consultation_context = grounded["agent_context"] if grounded \
            else self.project_consult_context(project_ids)
        if agents and vault_ask.about_lis(question):
            # Meme contexte LIS que la consultation (configuration reelle, sans reseau ni secret).
            lis_context = vault_ask.lis_context(self.backend)
            if consultation_context:
                consultation_context = {**consultation_context, "lis_runtime": lis_context}
            else:
                consultation_context = {"lis_runtime": lis_context}
        if agents:
            placeholder = self.send("🧠 Analyse en cours…", reply_to=reply_to)
            # Les longues demandes structurees meritent le chemin complexe : Gemini si configure,
            # sinon OpenRouter. Les consultations courtes gardent la chaine standard/local.
            consult_task = "complex" if len(question) > 500 or question.count("\n") >= 5 \
                else "standard"
            ready = self.backend is not None and _backend_available(self.backend, consult_task)
            if ready:
                panel = council.answer_with_agents(
                    self.conn, self.backend, question, agents, vault_answer=result["answer"],
                    sources=result["sources"], mode=mode or "B",
                    session_id=f"telegram:{self.chat_id}",
                    context=consultation_context, task=consult_task,
                )

        if panel and panel["opinions"]:
            # Priorite a la reponse utile : le dump complet du retrieval reste dans le Vault.
            # Contrat V3 conserve : contexte actif, mode, agents consultes et pourquoi.
            lines = [grounded["text"], ""] if grounded else []
            lines.append("🧠 Analyse")
            if panel.get("synthesis"):
                lines.append(f"Synthèse : {panel['synthesis']}")
            lines.append("")
            lines.append("🤝 Agents consultes (mode " + (mode or "B") + ") :")
            for agent in agents:
                lines.append(f"• {agent['name']} — {agent['reason']}")
            for opinion in panel["opinions"]:
                lines.append(f"\n{opinion['agent']} : {opinion['opinion']}")
            if result["sources"] and not grounded:
                lines.append("")
                lines.append("📚 Sources Vault retenues :")
                for source in result["sources"][:3]:
                    title = source.get("title") or f"source {source.get('source_id')}"
                    url = source.get("url")
                    lines.append(f"- {str(title)[:110]}" + (f" — {url}" if url else ""))
        else:
            lines = [result["answer"]]
            if agents:
                why = ("aucun moteur LLM disponible pour cette consultation "
                       "(configuration, budget ou connectivite)") if not ready \
                    else "reponse du moteur LLM inexploitable"
                lines.append("")
                lines.append(f"🤝 Agents selectionnes mais non consultes : {why}.")
                for agent in agents:
                    lines.append(f"• {agent['name']} — {agent['reason']}")

        message_ids = self.send_parts("\n".join(lines), placeholder=placeholder,
                                      reply_to=reply_to)
        source_ids = [s.get("source_id") for s in result["sources"] if s.get("source_id")]
        suggestion_id = grounded.get("suggestion_id") if grounded else None
        for message_id in message_ids:
            self.remember(message_id, "answer", suggestion_id=suggestion_id, question=question,
                          agents=[a["name"] for a in agents], mode=mode,
                          payload={"sources": source_ids, "anchor": anchor})
        if suggestion_id:
            self.set_active("suggestion", suggestion_id=suggestion_id,
                            project_id=grounded.get("project_id"))
        elif source_ids and result.get("route") != "project_view":
            self.set_active("sources", source_ids=source_ids, question=question)
        if not grounded:
            self._publish_question(question, result["sources"])
        return f"answer:{mode or 'ask'}" + (":anchored" if grounded else "")

    def _anchor_project(self, project_id):
        """Vue projet : le contexte devient la suggestion ouverte du projet, sinon le projet."""
        row = self.conn.execute(
            "SELECT suggestion_id FROM mentor_suggestions WHERE project_id = ? AND status IN "
            "('ready', 'sent', 'opened', 'snoozed') ORDER BY updated_at DESC LIMIT 1",
            (project_id,)).fetchone() if self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'mentor_suggestions'").fetchone() else None
        opened = self.open_suggestion()
        if opened and row is None:
            self.open_suggestion(close=True)
        if row:
            self.set_active("suggestion", suggestion_id=row[0], project_id=project_id)
        else:
            self.set_active("project", project_id=project_id)

    def grounding(self, anchor):
        """Contexte explicite -> texte, sources et contexte complet transmis aux agents."""
        import mentor_council as council
        import mentor_proactive_store as store

        if anchor.get("kind") == "suggestion":
            suggestion = store.get_suggestion(self.conn, anchor["suggestion_id"])
            if suggestion is None:
                return None
            evidence = suggestion["evidence"]
            project = (evidence.get("project") or {}).get("name") or \
                _project_name(self.conn, suggestion.get("project_id"))
            sources = [{"source_id": int(src["ref"].split(":")[1]), "title": src.get("title"),
                        "snippet": src.get("excerpt"), "url": src.get("url")}
                       for src in evidence.get("sources") or [] if src.get("ref")]
            text = (f"📌 Contexte : {suggestion['suggestion_id']} — {suggestion['title']} "
                    f"({project})\n{suggestion['summary']}")
            return {
                "suggestion_id": suggestion["suggestion_id"],
                "project_id": suggestion.get("project_id"),
                "text": text,
                "sources": sources,
                "agent_context": {
                    "suggestion": suggestion["suggestion_id"],
                    "titre": suggestion["title"],
                    "resume": suggestion["summary"],
                    "projet": project,
                    "hypothese": evidence.get("hypothesis"),
                    "preuves": evidence.get("candidate"),
                    "sources": evidence.get("sources"),
                    "verdicts_existants": [
                        {k: a.get(k) for k in ("agent", "value_score", "verdict",
                                               "proposed_action", "main_objection")}
                        for a in evidence.get("agents") or []],
                    "actions_proposees": evidence.get("actions"),
                },
            }
        if anchor.get("kind") == "project":
            import vault_ask

            view = vault_ask.project_view(self.conn, anchor["project_id"])
            if view is None:
                return None
            profile = view.get("profile") or {}
            sources = [{"source_id": s["source_id"], "title": s["title"],
                        "snippet": (s.get("snippet") or "")[:300], "url": s.get("url")}
                       for s in view["sources"]]
            return {"project_id": anchor["project_id"], "sources": sources,
                    "text": f"📌 Contexte : {view['entity']['name']}",
                    "agent_context": {"projet": view["entity"]["name"],
                                      "description": profile.get("description"),
                                      "sources": sources}}
        if anchor.get("kind") == "sources":
            briefs = [b for b in (council._source_brief(self.conn, sid)
                                  for sid in anchor.get("source_ids") or []) if b]
            if not briefs:
                return None
            sources = [{"source_id": int(b["ref"].split(":")[1]), "title": b["title"],
                        "snippet": b["excerpt"], "url": b["url"]} for b in briefs]
            titles = "; ".join(b["title"] or b["ref"] for b in briefs[:3])
            return {"project_id": None, "sources": sources,
                    "text": f"📌 Contexte : {titles}",
                    "agent_context": {"question_precedente": anchor.get("question"),
                                      "sources": briefs}}
        return None

    def _ask(self, question, llm):
        if self.ask_fn:
            return self.ask_fn(self.conn, question, llm=llm)
        import vault_ask

        # Telegram gere deja la consultation Agency ; Ask reste un retrieval pur.
        return vault_ask.ask(self.conn, question, mode="ask", llm=llm)

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
                self.set_active("suggestion", suggestion_id=sid,
                                project_id=suggestion.get("project_id"))
                self.open_suggestion(sid)
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
        if action in ("task", "insight", "no") or (action in ("nope", "later") and arg):
            self.open_suggestion(close=True)
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


def _backend_available(backend, task="standard"):
    try:
        return backend.available(task=task)
    except TypeError:
        return backend.available()


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


def refers_to_context(text):
    """La phrase renvoie-t-elle au contexte (« ca », « qu'en penses-tu »...) ?"""
    import mentor_council as council

    folded = fold(text)
    if ANAPHORA_RE.search(folded):
        return True
    # « Demande au CFO » seul, sans autre sujet : il parle du contexte en cours.
    rest = folded
    for agent in council.agents_in_text(text):
        rest = rest.replace(fold(agent.name), " ")
    for alias in council.AGENT_ALIASES:
        rest = re.sub(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", " ", rest)
    rest = re.sub(r"(?<![a-z])(demande|demander|au|a|aux|le|la|les|du|de|et|stp|svp|"
                  r"merci|ce|qu|il|elle|en|pense|penses|avis)(?![a-z])", " ", rest)
    return len(re.sub(r"[^a-z]+", "", rest)) == 0


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
    "• Envoie un lien, un fichier, une photo ou « garde ca : … » : ajoute au Vault.\n"
    "• /inbox : derniers ajouts · /save (en reponse) : garder un message · "
    "/project Nexdoor Talent (en reponse a un ajout) : rattacher.\n"
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
    # La synthese de vault_ask passe par le routeur LLM central (politique .env).
    os.environ["VAULT_ASK_SYNTHESIZER"] = "router"
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

    import llm_router

    conn = connect(str(db))
    # Routeur LLM central (Qwen local, Mistral, Claude optionnel), adosse a vault.db.
    backend = llm_router.bind(conn)
    ready = backend.ensure_ready(retries=10, delay=3, start=True)
    log.info("moteurs LLM : %s", {k: v["usable"] for k, v in backend.status().items()})
    log.info("au moins un moteur %s", "pret" if ready else "indisponible pour l'instant")
    return Gateway(conn, telegram_api.TelegramClient(token), allowed_user=user,
                   allowed_chat=chat, backend=backend,
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
                try:
                    gateway.send(
                        "⚠️ Je n'ai pas pu traiter ce message. L'erreur a ete journalisee. "
                        "Tu peux reessayer ou envoyer /status."
                    )
                except Exception:  # noqa: BLE001 - Telegram peut etre la cause de l'erreur
                    pass
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
