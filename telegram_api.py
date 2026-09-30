#!/usr/bin/env python3
"""
telegram_api.py - Client minimal de l'API Bot Telegram (polling, sans serveur public).

- Le token vient uniquement de l'environnement / du fichier .env local (non versionne).
- Il n'apparait jamais dans les erreurs ni dans les journaux (redaction).
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
API = "https://api.telegram.org"
MAX_TEXT = 4000


class TelegramError(RuntimeError):
    pass


def load_env(path=ENV_FILE):
    """Charge .env sans ecraser l'environnement existant."""
    if not Path(path).exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(path, override=False)
    except ImportError:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def write_env(values, path=ENV_FILE):
    """Met a jour des cles dans .env (sans jamais les afficher)."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen = set()
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in values:
            lines[index] = f"{key}={values[key]}"
            seen.add(key)
    for key, value in values.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def redact(text, token):
    text = str(text)
    if token:
        text = text.replace(token, "***")
    return text


def mask(token):
    return f"{token[:4]}…{token[-2:]}" if token and len(token) > 10 else "***"


def clip(text, limit=MAX_TEXT):
    """Garde-fou pour les textes courts (boutons, notifications) ; les reponses passent par split_text."""
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _units(text):
    # Telegram compte la limite en unites UTF-16 (un emoji peut en valoir 2).
    return len(text.encode("utf-16-le")) // 2


def split_text(text, limit=MAX_TEXT):
    """
    Decoupe un texte en morceaux <= limit, sans rien perdre : coupe de preference
    entre paragraphes, puis entre lignes, puis entre mots. Seuls les blancs situes
    au point de coupe disparaissent.
    """
    text = str(text or "")
    chunks = []
    while _units(text) > limit:
        window = text[:limit]
        while _units(window) > limit:
            window = window[:-1]
        cut = -1
        for separator in ("\n\n", "\n", " "):
            cut = window.rfind(separator)
            if cut > limit // 3:
                break
        if cut <= 0:
            cut = len(window)  # mot unique plus long que la limite : coupe franche
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n ")
    if text.strip() or not chunks:
        chunks.append(text)
    return chunks


class TelegramClient:
    def __init__(self, token=None, timeout=35):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.timeout = timeout

    def configured(self):
        return bool(self.token)

    def call(self, method, params=None, *, timeout=None):
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN absent")
        data = json.dumps(params or {}).encode("utf-8")
        request = urllib.request.Request(
            f"{API}/bot{self.token}/{method}", data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            raise TelegramError(redact(f"{method} HTTP {exc.code}: {body}", self.token)) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TelegramError(redact(f"{method}: {type(exc).__name__}", self.token)) from None
        if not payload.get("ok"):
            raise TelegramError(redact(f"{method}: {payload.get('description')}", self.token))
        return payload.get("result")

    # Methodes utilisees
    def get_me(self):
        return self.call("getMe")

    def get_updates(self, offset=None, timeout=25):
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params, timeout=timeout + 10)

    def send_message(self, chat_id, text, *, buttons=None, reply_to=None):
        params = {"chat_id": chat_id, "text": clip(text), "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to,
                                          "allow_sending_without_reply": True}
        return self.call("sendMessage", params)

    def edit_message(self, chat_id, message_id, text, *, buttons=None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": clip(text),
                  "disable_web_page_preview": True}
        params["reply_markup"] = {"inline_keyboard": buttons or []}
        return self.call("editMessageText", params)

    def answer_callback(self, callback_id, text=None):
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = clip(text, 180)
        return self.call("answerCallbackQuery", params)

    def send_chat_action(self, chat_id, action="typing"):
        return self.call("sendChatAction", {"chat_id": chat_id, "action": action})

    def download_file(self, file_id, max_bytes=20_000_000):
        info = self.call("getFile", {"file_id": file_id})
        path = info.get("file_path")
        if not path or int(info.get("file_size") or 0) > max_bytes:
            raise TelegramError("fichier indisponible ou trop gros")
        try:
            with urllib.request.urlopen(f"{API}/file/bot{self.token}/{path}",
                                        timeout=self.timeout) as response:
                return response.read(max_bytes + 1)[:max_bytes]
        except (urllib.error.URLError, OSError) as exc:
            raise TelegramError(redact(f"download: {type(exc).__name__}", self.token)) from None
