#!/usr/bin/env python3
"""
telegram_setup.py - Configuration locale du bot Telegram (a lancer par Rosy).

1. demande le token (saisie masquee) et l'ecrit dans .env (jamais affiche) ;
2. verifie le token (getMe) ;
3. attend un /start envoye au bot, detecte user_id et chat_id (getUpdates) ;
4. si plusieurs expediteurs : liste et demande lequel autoriser (aucune supposition) ;
5. ecrit TELEGRAM_ALLOWED_USER_ID / TELEGRAM_ALLOWED_CHAT_ID dans .env ;
6. envoie « 🧠 Life Intelligence connecté. ».

Usage :  .\\.venv\\Scripts\\python.exe telegram_setup.py
"""

import getpass
import os
import sys
import time

import telegram_api


def detect_senders(client, wait_seconds=120):
    """Expediteurs ayant ecrit /start au bot (user_id, chat_id, nom)."""
    deadline = time.monotonic() + wait_seconds
    offset = None
    senders = {}
    while time.monotonic() < deadline:
        for update in client.get_updates(offset=offset, timeout=10) or []:
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            if (message.get("text") or "").strip().startswith("/start"):
                user = message.get("from") or {}
                chat = message.get("chat") or {}
                senders[(user.get("id"), chat.get("id"))] = {
                    "user_id": user.get("id"), "chat_id": chat.get("id"),
                    "username": user.get("username") or "", "first_name": user.get("first_name") or "",
                    "chat_type": chat.get("type"),
                }
        if senders:
            # Acquitte les updates lues pour que la passerelle ne les rejoue pas.
            client.get_updates(offset=offset, timeout=0)
            return list(senders.values())
    return []


def choose(senders, input_func=input):
    if len(senders) == 1:
        return senders[0]
    print("Plusieurs expediteurs ont envoye /start :")
    for index, item in enumerate(senders, start=1):
        print(f"  [{index}] @{item['username']} {item['first_name']} - user_id {item['user_id']}"
              f" - chat_id {item['chat_id']} ({item['chat_type']})")
    while True:
        answer = input_func("Lequel autoriser ? (numero) > ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(senders):
            return senders[int(answer) - 1]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    telegram_api.load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        keep = input(f"Un token est deja configure ({telegram_api.mask(token)}). Le garder ? [O/n] ")
        if keep.strip().lower() in ("n", "non"):
            token = ""
    if not token:
        token = getpass.getpass("Colle le token du bot (BotFather), saisie masquee : ").strip()
    client = telegram_api.TelegramClient(token)
    try:
        me = client.get_me()
    except telegram_api.TelegramError as exc:
        sys.exit(f"Token refuse par Telegram : {exc}")
    telegram_api.write_env({"TELEGRAM_BOT_TOKEN": token})
    print(f"✓ Token valide pour @{me.get('username')} (enregistre dans .env, non versionne).")

    print(f"Envoie maintenant /start a @{me.get('username')} depuis ton compte Telegram "
          "(j'attends 2 minutes)…")
    senders = detect_senders(client)
    if not senders:
        sys.exit("Aucun /start recu. Relance telegram_setup.py quand tu es pret.")
    chosen = choose(senders)
    print(f"Expediteur detecte : @{chosen['username']} {chosen['first_name']} - "
          f"user_id {chosen['user_id']} - chat_id {chosen['chat_id']}")
    if input("Autoriser cet expediteur ? [O/n] ").strip().lower() in ("n", "non"):
        sys.exit("Aucune autorisation ecrite.")
    telegram_api.write_env({"TELEGRAM_ALLOWED_USER_ID": chosen["user_id"],
                            "TELEGRAM_ALLOWED_CHAT_ID": chosen["chat_id"]})
    client.send_message(chosen["chat_id"], "🧠 Life Intelligence connecté.")
    print("✓ Message de test envoye. Configuration terminee.")
    print("Demarrage : .\\.venv\\Scripts\\python.exe telegram_gateway.py")


if __name__ == "__main__":
    main()
