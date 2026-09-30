#!/usr/bin/env python3
"""
llm_setup.py - Configuration et verification des moteurs LLM (a lancer par Rosy).

1. detecte les moteurs deja configures ;
2. propose de saisir la cle Gemini (saisie masquee) puis la teste reellement ;
3. propose de saisir la cle OpenRouter (saisie masquee) puis la teste (lecture du
   credit, aucun cout) ;
4. n'affiche jamais une cle ; enregistre uniquement dans .env (ignore par Git) ;
5. affiche l'etat final :
     Qwen        : READY / UNAVAILABLE
     Gemini      : READY / NOT CONFIGURED / INVALID
     OpenRouter  : READY / NOT CONFIGURED / INVALID
     Anthropic   : READY / NOT CONFIGURED
Mistral n'est pas concerne.

Usage :
    .\\.venv\\Scripts\\python.exe llm_setup.py            # interactif
    .\\.venv\\Scripts\\python.exe llm_setup.py --status   # etat seul, sans saisie
"""

import argparse
import getpass
import os
import sys

import llm_router as lr


def check_qwen(provider=None):
    provider = provider or lr.OllamaProvider()
    if not provider.configured() or not provider.reachable():
        return "UNAVAILABLE", "Ollama arrete ou modele absent"
    ok, why = provider.pc_ok()
    return ("READY", None) if ok else ("UNAVAILABLE", f"installe, PC trop charge : {why}")


def check_gemini(provider=None):
    provider = provider or lr.GeminiProvider()
    if not provider.key():
        return "NOT CONFIGURED", None
    try:
        # Mini requete (quelques tokens) sur le modele leger : verifie la cle Developer API.
        provider.call("Reponds uniquement OK.", "ping", max_tokens=5, task="light", timeout=20)
        return "READY", None
    except lr.InvalidKey:
        return "INVALID", "cle refusee (pas une cle Gemini Developer API ?)"
    except lr.NetworkDown:
        return "UNAVAILABLE", "reseau indisponible"
    except Exception as exc:  # noqa: BLE001 - quota, modele... : jamais bloquant
        return "UNAVAILABLE", str(exc)[:80]


def check_openrouter(provider=None):
    provider = provider or lr.OpenRouterProvider()
    if not provider.key():
        return "NOT CONFIGURED", None
    import json
    import urllib.request

    request = urllib.request.Request(provider.URL + "/key",
                                     headers={"Authorization": f"Bearer {provider.key()}"})
    try:
        data = lr._http_json(request, 15, provider.key()).get("data") or {}
    except lr.InvalidKey:
        return "INVALID", "cle refusee"
    except lr.NetworkDown:
        return "UNAVAILABLE", "reseau indisponible"
    except Exception as exc:  # noqa: BLE001
        return "UNAVAILABLE", str(exc)[:80]
    remaining = provider.credits_remaining()
    detail = f"credit restant ~{remaining:.2f} USD" if remaining is not None else \
        f"credit non lisible (usage {json.dumps(data.get('usage'))})"
    return "READY", detail


def check_anthropic(provider=None):
    provider = provider or lr.AnthropicProvider()
    # Aucun appel payant : presence de la cle et du SDK seulement.
    return ("READY", None) if provider.configured() else ("NOT CONFIGURED", None)


def report(write=print):
    rows = [("Qwen", check_qwen()), ("Gemini", check_gemini()),
            ("OpenRouter", check_openrouter()), ("Anthropic", check_anthropic())]
    for name, (state, detail) in rows:
        write(f"{name:<11} : {state}" + (f"  ({detail})" if detail else ""))
    return dict((name, state) for name, (state, _) in rows)


def ask_key(label, env_name, checker, input_func=input, secret_input=getpass.getpass):
    """Saisie masquee ; la cle n'est ecrite dans .env qu'apres un test concluant."""
    import telegram_api

    current = os.environ.get(env_name, "")
    prompt = f"{label} : {'deja configuree (' + lr.mask(current) + '), ' if current else ''}" \
             f"saisir une nouvelle cle ? [o/N] "
    if input_func(prompt).strip().lower() not in ("o", "oui", "y", "yes"):
        return None
    key = secret_input(f"Cle {label} (saisie masquee) : ").strip()
    if not key:
        return None
    previous = os.environ.get(env_name)
    os.environ[env_name] = key
    state, detail = checker()
    if state == "READY":
        telegram_api.write_env({env_name: key})
        print(f"✓ {label} : READY, cle enregistree dans .env" + (f" ({detail})" if detail else ""))
    else:
        print(f"✗ {label} : {state}" + (f" ({detail})" if detail else "") +
              " — cle NON enregistree.")
        if previous is None:
            os.environ.pop(env_name, None)
        else:
            os.environ[env_name] = previous
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description="Moteurs LLM du Life Intelligence")
    parser.add_argument("--status", action="store_true", help="etat seul, sans saisie")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    print("Etat actuel :")
    report()
    if not args.status:
        print()
        ask_key("Gemini", "GEMINI_API_KEY", check_gemini)
        ask_key("OpenRouter", "OPENROUTER_API_KEY", check_openrouter)
        print("\nEtat final :")
        report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
