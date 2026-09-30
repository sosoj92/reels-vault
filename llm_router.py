#!/usr/bin/env python3
"""
llm_router.py - Routeur LLM central du Life Intelligence System.

Aucun module metier ne choisit son fournisseur : il declare une TACHE et une
SENSIBILITE, le routeur decide. Repli ultime : FILE D'ATTENTE (None -> l'analyse
attend et reprend plus tard) ; une indisponibilite LLM ne casse jamais Vault,
Telegram, ingestion ou Notion.

Chaines (configurables : LLM_CHAIN_LIGHT / _STANDARD / _COMPLEX) :
  deterministic -> aucun LLM (erreur si appele) ;
  light         -> Qwen local (si dispo et RAM/CPU suffisants) -> Gemini -> OpenRouter -> file ;
  standard      -> Gemini -> Qwen local (si le PC peut) -> OpenRouter -> file ;
  complex       -> Gemini (modele de raisonnement) -> OpenRouter -> file ;
  hors ligne    -> Qwen local uniquement, sinon deterministe + file.
Sensibilite (classify) :
  public / internal -> cloud autorise selon le routage ;
  personal          -> cloud seulement si LLM_PERSONAL_CLOUD_ALLOWED=true ;
  sensitive         -> LOCAL UNIQUEMENT (sinon file d'attente) ;
  secret            -> JAMAIS envoye a un LLM.

OpenRouter = reserve : budgets locaux en USD (jour / mois) et plancher de credit
(OPENROUTER_RESERVE_USD) ; modeles couteux jamais utilises automatiquement ; pas de
Claude via OpenRouter pour une tache simple. Mistral : present mais hors des chaines.
Un appel cloud n'est jamais relance (pas de double facturation) : le repli passe au
fournisseur suivant. Journal `llm_calls` (logs/llm_usage.db) : backend, modele, tache,
tokens, cout, duree, succes, repli ; jamais de cle ni de contenu.
"""

import ctypes
import json
import os
import re
import socket
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Journal et budget LLM : base dediee (vault.db n'est jamais modifie par le routeur).
DEFAULT_USAGE_DB = ROOT / "logs" / "llm_usage.db"

TASKS = ("deterministic", "light", "standard", "complex")
SENSITIVITIES = ("public", "internal", "personal", "sensitive", "secret")

DEFAULTS = {
    "OLLAMA_MODEL": "qwen3:1.7b",
    "OLLAMA_URL": "http://localhost:11434",
    "OLLAMA_IDLE_UNLOAD_MINUTES": "10",
    "GEMINI_ENABLED": "true",
    "GEMINI_LIGHT_MODEL": "gemini-2.5-flash-lite",
    "GEMINI_STANDARD_MODEL": "gemini-2.5-flash",
    "GEMINI_COMPLEX_MODEL": "gemini-2.5-pro",
    "GEMINI_COMPLEX_THINKING_BUDGET": "1024",
    "GEMINI_DAILY_BUDGET_USD": "",
    "GEMINI_PRICE_IN_USD": "0", "GEMINI_PRICE_OUT_USD": "0",
    "OPENROUTER_ENABLED": "true",
    "OPENROUTER_STANDARD_MODEL": "openai/gpt-4o-mini",
    "OPENROUTER_COMPLEX_MODEL": "google/gemini-2.5-flash",
    "OPENROUTER_FALLBACK_MODELS": "",
    "OPENROUTER_DAILY_BUDGET_USD": "0.25",
    "OPENROUTER_MONTHLY_BUDGET_USD": "",
    "OPENROUTER_RESERVE_USD": "1.00",
    "OPENROUTER_INITIAL_CREDIT_USD": "7.00",
    "OPENROUTER_PRICE_IN_USD": "0.15", "OPENROUTER_PRICE_OUT_USD": "0.60",
    "MISTRAL_MODEL": "mistral-small-latest",
    "ANTHROPIC_MODEL": "",
    "LLM_LOCAL_ENABLED": "true",
    "LLM_CLOUD_ENABLED": "true",
    "LLM_CHAIN_LIGHT": "local,gemini,openrouter",
    "LLM_CHAIN_STANDARD": "gemini,local,openrouter",
    "LLM_CHAIN_COMPLEX": "gemini,openrouter",
    "LLM_SENSITIVE_CLOUD_ALLOWED": "false",
    "LLM_PERSONAL_CLOUD_ALLOWED": "false",
    "LLM_DAILY_BUDGET_EUR": "1.00",
    "LLM_MONTHLY_BUDGET_EUR": "10.00",
    "LLM_CLAUDE_DAILY_BUDGET_EUR": "0.50",
    "LLM_USD_TO_EUR": "0.92",
    "LLM_LOCAL_MAX_CPU": "75",
    "LLM_LOCAL_MIN_FREE_MB": "1800",
    # Prix estimes (EUR par million de tokens) des fournisseurs historiques.
    "LLM_PRICE_MISTRAL_IN": "0.10", "LLM_PRICE_MISTRAL_OUT": "0.30",
    "LLM_PRICE_ANTHROPIC_IN": "5.00", "LLM_PRICE_ANTHROPIC_OUT": "25.00",
}
TIMEOUTS = {"local": 75.0, "gemini": 40.0, "openrouter": 45.0, "mistral": 30.0,
            "anthropic": 60.0}
BREAKER_FAILURES = 3
BREAKER_COOLDOWN = 300.0
OFFLINE_COOLDOWN = 120.0
# Jamais automatiquement : modeles tres couteux via OpenRouter.
EXPENSIVE_MODEL = re.compile(r"(claude-(3-)?opus|claude-opus|gpt-4\.5|o1-pro|o3-pro|"
                             r"gpt-5-pro|grok-4-heavy)", re.I)

LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider      TEXT NOT NULL,
    model         TEXT,
    task          TEXT NOT NULL,
    purpose       TEXT,
    sensitivity   TEXT NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_eur      REAL NOT NULL DEFAULT 0,
    duration_ms   INTEGER,
    success       INTEGER NOT NULL,
    fallback_from TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at, provider);
"""


class LLMError(RuntimeError):
    pass


class InvalidKey(LLMError):
    """Cle refusee par le fournisseur : le fournisseur est traite comme non configure."""


class NetworkDown(LLMError):
    """Pas de reseau : le routeur passe en mode hors ligne (local uniquement)."""


def _load_llm_env(path=ROOT / ".env"):
    """Reglages LLM du .env local (prefixes LLM), sans ecraser l'environnement."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key.startswith(("LLM_", "OLLAMA_", "MISTRAL_", "ANTHROPIC_", "GEMINI_",
                           "OPENROUTER_")):
            os.environ.setdefault(key, value)


_load_llm_env()


def cfg(name):
    value = os.environ.get(name)
    return value if value not in (None, "") else DEFAULTS.get(name, "")


def flag(name):
    return str(cfg(name)).strip().lower() in ("1", "true", "on", "oui", "yes")


def money(name):
    """Montant configure, ou None si vide (= pas de plafond)."""
    value = str(cfg(name)).strip()
    return float(value) if value else None


def mask(key):
    return f"{key[:3]}…{key[-2:]}" if key and len(key) > 8 else "***"


def redact(text, *secrets):
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


# ------------------------------------------------------------------ confidentialite

_SECRET_PATTERNS = [
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{16,}"),                     # cles API
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                                  # AWS
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),                          # GitHub
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"),                     # token bot Telegram
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # « mot de passe Manatal = … » : jusqu'a 3 mots entre le libelle et la valeur.
    re.compile(r"(?i)\b(mot de passe|password|passwd|pwd|passcode|code pin|pin)"
               r"(\s+[\w.@-]+){0,3}\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(api[_ -]?key|secret|token|bearer)(\s+[\w.-]+){0,2}\s*[:=]\s*"
               r"[A-Za-z0-9_\-\.]{12,}"),
    re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b"),              # IBAN
]
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SENSITIVE_WORDS = (
    "diagnostic", "diagnosis", "traitement", "treatment", "medicament", "medication",
    "ordonnance", "prescription", "posologie", "symptome", "symptom", "maladie", "pathologie",
    "tdah", "adhd", "depression", "anxiete", "therapie", "psychiatre", "dossier medical",
    "compte rendu medical", "analyse de sang", "releve bancaire", "avis d'imposition",
    "salaire net", "dette", "credit immobilier", "numero de securite sociale", "confidentiel",
    "strictement confidentiel", "nda",
)
_PERSONAL = (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), re.compile(r"(?:\+33|0)[1-9](?:[ .-]?\d{2}){4}"))


def _luhn(number):
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
            digit -= 9 if digit > 9 else 0
        total += digit
    return total % 10 == 0


def _fold(text):
    import unicodedata

    text = unicodedata.normalize("NFD", str(text or ""))
    return "".join(c for c in text if unicodedata.category(c) != "Mn").lower()


def contains_secret(text):
    text = str(text or "")
    if any(p.search(text) for p in _SECRET_PATTERNS):
        return True
    return any(_luhn(m.group(0)) for m in _CARD.finditer(text))


def classify(text, *, hint=None):
    """public | internal | personal | sensitive | secret (le plus strict l'emporte)."""
    if contains_secret(text):
        return "secret"
    folded = _fold(text)
    if hint == "sensitive" or any(
            re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", folded)
            for w in _SENSITIVE_WORDS):
        return "sensitive"
    if hint == "personal" or any(p.search(str(text or "")) for p in _PERSONAL):
        return "personal"
    return hint if hint in ("public", "internal") else "internal"


# ------------------------------------------------------------------ charge du PC

def pc_load():
    """(cpu_percent, free_mb) sous Windows via l'API systeme ; (None, None) sinon."""
    if os.name != "nt":
        return None, None
    try:
        class FILETIME(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32),
                        ("total", ctypes.c_uint64), ("avail", ctypes.c_uint64),
                        ("total_pf", ctypes.c_uint64), ("avail_pf", ctypes.c_uint64),
                        ("total_v", ctypes.c_uint64), ("avail_v", ctypes.c_uint64),
                        ("ext", ctypes.c_uint64)]

        def times():
            idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                                  ctypes.byref(user))
            value = lambda f: (f.high << 32) | f.low  # noqa: E731
            return value(idle), value(kernel) + value(user)

        idle1, total1 = times()
        time.sleep(0.25)
        idle2, total2 = times()
        busy = total2 - total1
        cpu = 100.0 * (1 - (idle2 - idle1) / busy) if busy else 0.0
        memory = MEMORYSTATUSEX()
        memory.length = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))
        return round(cpu, 1), int(memory.avail / (1024 * 1024))
    except Exception:  # noqa: BLE001
        return None, None


# ------------------------------------------------------------------ fournisseurs

def _extract_json(text):
    text = str(text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except ValueError:
                return None
    return None


class OllamaProvider:
    name = "local"
    cloud = False

    def __init__(self, model=None, url=None, load_probe=pc_load):
        self.model = model or cfg("OLLAMA_MODEL")
        self.url = (url or os.environ.get("VAULT_OLLAMA_URL") or cfg("OLLAMA_URL")).rstrip("/")
        self.load_probe = load_probe

    def configured(self):
        return flag("LLM_LOCAL_ENABLED")

    def reachable(self):
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=2) as response:
                return self.model in [m.get("name") for m in json.load(response).get("models", [])]
        except Exception:  # noqa: BLE001
            return False

    def loaded(self):
        """Le modele est-il deja en memoire dans Ollama (aucun chargement a payer) ?"""
        try:
            with urllib.request.urlopen(self.url + "/api/ps", timeout=2) as response:
                return self.model in [m.get("name") for m in json.load(response).get("models", [])]
        except Exception:  # noqa: BLE001
            return False

    def pc_ok(self):
        cpu, free_mb = self.load_probe() if self.load_probe else (None, None)
        if cpu is not None and cpu > float(cfg("LLM_LOCAL_MAX_CPU")):
            return False, f"CPU {cpu}%"
        # RAM : seulement si Qwen doit etre charge (deja en memoire = aucun cout de plus).
        if free_mb is not None and free_mb < int(cfg("LLM_LOCAL_MIN_FREE_MB")) \
                and not self.loaded():
            return False, f"RAM libre {free_mb} Mo"
        return True, None

    def keep_alive(self):
        return f"{int(cfg('OLLAMA_IDLE_UNLOAD_MINUTES'))}m"

    def call(self, system, user, *, schema=None, max_tokens=300, timeout=None, **_routing):
        payload = {
            "model": self.model, "stream": False, "think": False,
            "keep_alive": self.keep_alive(),
            "options": {"temperature": 0, "num_predict": max_tokens, "num_ctx": 4096},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        if schema:
            payload["format"] = schema
        request = urllib.request.Request(self.url + "/api/chat",
                                         data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        # Sortie du `with` = socket fermee : Ollama cesse de generer.
        with urllib.request.urlopen(request, timeout=timeout or TIMEOUTS["local"]) as response:
            raw = json.load(response)
        return {"text": raw["message"]["content"], "input_tokens": raw.get("prompt_eval_count"),
                "output_tokens": raw.get("eval_count"), "model": self.model}


class MistralProvider:
    name = "mistral"
    cloud = True
    URL = "https://api.mistral.ai/v1/chat/completions"

    def __init__(self, model=None):
        self.model = model or cfg("MISTRAL_MODEL")

    def key(self):
        return os.environ.get("MISTRAL_API_KEY", "")

    def configured(self):
        return flag("LLM_CLOUD_ENABLED") and bool(self.key())

    def call(self, system, user, *, schema=None, max_tokens=300, timeout=None, **_routing):
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        if schema:
            body["response_format"] = {"type": "json_object"}
            body["messages"][0]["content"] += (
                "\nReponds UNIQUEMENT avec un objet JSON conforme a ce schema : "
                + json.dumps(schema, ensure_ascii=False))
        request = urllib.request.Request(
            self.URL, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "Authorization": f"Bearer {self.key()}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout or TIMEOUTS["mistral"]) as resp:
                raw = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise LLMError(redact(f"Mistral HTTP {exc.code}", self.key())) from None
        usage = raw.get("usage") or {}
        return {"text": raw["choices"][0]["message"]["content"],
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"), "model": raw.get("model")
                or self.model}


class AnthropicProvider:
    """Claude API (optionnelle) via l'adaptateur existant vault_cloud.ClaudeProvider."""

    name = "anthropic"
    cloud = True

    def __init__(self, model=None):
        import vault_cloud

        self.inner = vault_cloud.ClaudeProvider(model=model or cfg("ANTHROPIC_MODEL") or None)
        self.model = self.inner.model

    def configured(self):
        return flag("LLM_CLOUD_ENABLED") and self.inner.available()[0]

    def call(self, system, user, *, schema=None, max_tokens=600, timeout=None, **_routing):
        if schema:
            system += ("\nReponds UNIQUEMENT avec un objet JSON conforme a ce schema : "
                       + json.dumps(schema, ensure_ascii=False))
        response = self.inner.complete(system, user, max_tokens=max_tokens,
                                       timeout=timeout or TIMEOUTS["anthropic"])
        return {"text": response.text, "input_tokens": None, "output_tokens": None,
                "model": response.model}


def _http_json(request, timeout, key=""):
    """Appel HTTP JSON : erreurs reseau -> NetworkDown, HTTP -> LLMError (cle expurgee)."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = redact(exc.read().decode("utf-8", "replace")[:300], key)
        if exc.code in (401, 403) or "API_KEY_INVALID" in body or "invalid api key" in \
                body.lower():
            raise InvalidKey(f"HTTP {exc.code} cle refusee") from None
        if exc.code == 429:
            raise LLMError("HTTP 429 quota") from None
        if exc.code == 402:
            raise LLMError("HTTP 402 credit insuffisant") from None
        raise LLMError(f"HTTP {exc.code}") from None
    except (urllib.error.URLError, ConnectionError) as exc:
        if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
            raise
        raise NetworkDown(type(exc).__name__) from None


def _schema_hint(schema):
    return ("\nReponds UNIQUEMENT avec un objet JSON conforme a ce schema : "
            + json.dumps(schema, ensure_ascii=False)) if schema else ""


class GeminiProvider:
    """Gemini Developer API (generativelanguage.googleapis.com), cle en en-tete."""

    name = "gemini"
    cloud = True
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    KEYS = {"light": "GEMINI_LIGHT_MODEL", "standard": "GEMINI_STANDARD_MODEL",
            "complex": "GEMINI_COMPLEX_MODEL"}

    def __init__(self):
        self.invalid = False

    @property
    def model(self):
        return cfg("GEMINI_STANDARD_MODEL")

    def key(self):
        return os.environ.get("GEMINI_API_KEY", "")

    def configured(self):
        return flag("LLM_CLOUD_ENABLED") and flag("GEMINI_ENABLED") and bool(self.key()) \
            and not self.invalid

    def model_for(self, task):
        return cfg(self.KEYS.get(task, "GEMINI_STANDARD_MODEL"))

    def call(self, system, user, *, schema=None, max_tokens=300, timeout=None, task="standard"):
        model = self.model_for(task)
        config = {"temperature": 0, "maxOutputTokens": max_tokens}
        if task == "complex":
            budget = int(cfg("GEMINI_COMPLEX_THINKING_BUDGET"))
            config["thinkingConfig"] = {"thinkingBudget": budget}
            config["maxOutputTokens"] = max_tokens + budget
        else:
            config["thinkingConfig"] = {"thinkingBudget": 0}  # rapide, sans raisonnement cache
        if schema:
            config["responseMimeType"] = "application/json"
        body = {"systemInstruction": {"parts": [{"text": system + _schema_hint(schema)}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": config}
        request = urllib.request.Request(
            self.URL.format(model=model), data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.key()})
        try:
            raw = _http_json(request, timeout or TIMEOUTS["gemini"], self.key())
        except InvalidKey:
            self.invalid = True
            raise
        candidates = raw.get("candidates") or []
        text = "".join(p.get("text", "") for c in candidates[:1]
                       for p in (c.get("content") or {}).get("parts", []))
        if not text:
            raise LLMError("reponse Gemini vide ou bloquee")
        usage = raw.get("usageMetadata") or {}
        output = (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0)
        return {"text": text, "input_tokens": usage.get("promptTokenCount"),
                "output_tokens": output or None, "model": raw.get("modelVersion") or model}


class OpenRouterProvider:
    """OpenRouter : reserve de secours, budget local et plancher de credit."""

    name = "openrouter"
    cloud = True
    URL = "https://openrouter.ai/api/v1"

    def __init__(self, clock=time.monotonic):
        self.invalid = False
        self.clock = clock
        self._credits = None
        self._credits_at = -1e9

    @property
    def model(self):
        return cfg("OPENROUTER_STANDARD_MODEL")

    def key(self):
        return os.environ.get("OPENROUTER_API_KEY", "")

    def configured(self):
        return flag("LLM_CLOUD_ENABLED") and flag("OPENROUTER_ENABLED") and bool(self.key()) \
            and not self.invalid

    def model_for(self, task):
        return cfg("OPENROUTER_COMPLEX_MODEL" if task == "complex" else
                   "OPENROUTER_STANDARD_MODEL")

    def allowed_models(self, task):
        """Modele principal + petite liste de secours, sans modele couteux ni Claude pour du simple."""
        models = [self.model_for(task)] + [m.strip() for m in
                                           cfg("OPENROUTER_FALLBACK_MODELS").split(",")
                                           if m.strip()][:2]
        kept = []
        for model in models:
            if EXPENSIVE_MODEL.search(model):
                continue
            if model.lower().startswith("anthropic/") and task != "complex":
                continue
            if model not in kept:
                kept.append(model)
        return kept

    def credits_remaining(self):
        """Credit restant (API /credits, mis en cache 10 min), ou None si inconnu."""
        if self.clock() - self._credits_at < 600:
            return self._credits
        request = urllib.request.Request(self.URL + "/credits",
                                         headers={"Authorization": f"Bearer {self.key()}"})
        try:
            data = _http_json(request, 10, self.key()).get("data") or {}
            self._credits = float(data["total_credits"]) - float(data["total_usage"])
        except Exception:  # noqa: BLE001 - inconnu : estimation locale par le routeur
            self._credits = None
        self._credits_at = self.clock()
        return self._credits

    def call(self, system, user, *, schema=None, max_tokens=300, timeout=None, task="standard"):
        models = self.allowed_models(task)
        if not models:
            raise LLMError("aucun modele OpenRouter autorise pour cette tache")
        body = {"model": models[0], "temperature": 0, "max_tokens": max_tokens,
                "usage": {"include": True},
                "messages": [{"role": "system", "content": system + _schema_hint(schema)},
                             {"role": "user", "content": user}]}
        if len(models) > 1:
            body["models"] = models  # repli interne OpenRouter, borne a notre liste
        request = urllib.request.Request(
            self.URL + "/chat/completions", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key()}",
                     "X-Title": "Life Intelligence", "HTTP-Referer": "http://localhost"})
        try:
            raw = _http_json(request, timeout or TIMEOUTS["openrouter"], self.key())
        except InvalidKey:
            self.invalid = True
            raise
        usage = raw.get("usage") or {}
        self._credits_at = -1e9  # credit a relire apres consommation
        return {"text": raw["choices"][0]["message"]["content"],
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "cost_usd": usage.get("cost"), "model": raw.get("model") or models[0]}


# ------------------------------------------------------------------ routeur

class Breaker:
    def __init__(self, clock=time.monotonic):
        self.failures = {}
        self.open_until = {}
        self.clock = clock

    def closed(self, name):
        return self.clock() >= self.open_until.get(name, 0.0)

    def success(self, name):
        self.failures[name] = 0
        self.open_until.pop(name, None)

    def failure(self, name):
        self.failures[name] = self.failures.get(name, 0) + 1
        if self.failures[name] >= BREAKER_FAILURES:
            self.open_until[name] = self.clock() + BREAKER_COOLDOWN
            self.failures[name] = 0


BREAKER = Breaker()  # partage par processus


def default_providers():
    return {"local": OllamaProvider(), "gemini": GeminiProvider(),
            "openrouter": OpenRouterProvider(), "mistral": MistralProvider(),
            "anthropic": AnthropicProvider()}


class LLMRouter:
    """Interface unique : chat_json / complete_text. None = mise en file (reessai plus tard)."""

    name = "router"

    def __init__(self, conn=None, providers=None, breaker=None, sleep=time.sleep,
                 usage_db=None):
        # `conn` (vault.db) n'est pas utilise pour le journal : base dediee llm_usage.db.
        self.providers = providers or default_providers()
        self.breaker = breaker or BREAKER
        self.sleep = sleep
        self.last = None
        self.logging = False
        self.conn = None
        self.offline_until = -1e9
        path = Path(usage_db or os.environ.get("LLM_USAGE_DB") or DEFAULT_USAGE_DB)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5)
            self.conn.executescript(LOG_SCHEMA)
            columns = {r[1] for r in self.conn.execute("PRAGMA table_info(llm_calls)")}
            if "cost_usd" not in columns:
                self.conn.execute("ALTER TABLE llm_calls ADD COLUMN cost_usd REAL NOT NULL "
                                  "DEFAULT 0")
            self.conn.commit()
            self.logging = True
        except Exception:  # noqa: BLE001 - journal impossible : le cloud reste ferme
            self.conn = None

    # -------------------------------------------------------------- politique
    def clock(self):
        return self.breaker.clock()

    def offline(self):
        return self.clock() < self.offline_until

    def chain(self, task, sensitivity):
        if task == "deterministic":
            raise LLMError("tache deterministe : aucun LLM")
        if task not in TASKS:
            raise LLMError(f"tache inconnue : {task}")
        if sensitivity == "secret":
            return []
        order = [n.strip() for n in cfg(f"LLM_CHAIN_{task.upper()}").split(",") if n.strip()]
        seen = [n for i, n in enumerate(order) if n in self.providers and n not in order[:i]]
        cloud_ok = sensitivity in ("public", "internal") or \
            (sensitivity == "personal" and flag("LLM_PERSONAL_CLOUD_ALLOWED")) or \
            (sensitivity == "sensitive" and flag("LLM_SENSITIVE_CLOUD_ALLOWED"))
        return [n for n in seen if cloud_ok or not self.providers[n].cloud]

    def usable(self, name):
        provider = self.providers[name]
        if getattr(provider, "invalid", False):
            return False, "cle invalide"
        if not provider.configured():
            return False, "non configure"
        if not self.breaker.closed(name):
            return False, "coupe-circuit ouvert"
        if provider.cloud:
            if self.offline():
                return False, "hors ligne"
            over = self.over_budget(name)
            if over:
                return False, over
            return True, None
        if not provider.reachable():
            return False, "injoignable"
        ok, why = provider.pc_ok()
        return (ok, why)

    def available(self, task="light", sensitivity="internal"):
        return any(self.usable(n)[0] for n in self.chain(task, sensitivity))

    def status(self):
        out = {}
        for name, provider in self.providers.items():
            ok, why = self.usable(name)
            out[name] = {"model": provider.model, "usable": ok, "reason": why,
                         "cloud": provider.cloud}
        return out

    # -------------------------------------------------------------- budget
    def _since(self, column, since, provider=None):
        sql = f"SELECT COALESCE(SUM({column}), 0) FROM llm_calls WHERE created_at >= ?"
        params = [since]
        if provider:
            sql += " AND provider = ?"
            params.append(provider)
        return float(self.conn.execute(sql, params).fetchone()[0])

    def spent(self, since, provider=None):
        """Depense en EUR (couts USD convertis)."""
        if self.conn is None or not self.logging:
            return float("inf")  # budget non verifiable : pas de cloud
        return self._since("cost_eur", since, provider) + \
            self._since("cost_usd", since, provider) * float(cfg("LLM_USD_TO_EUR"))

    def spent_usd(self, since, provider):
        if self.conn is None or not self.logging:
            return float("inf")
        return self._since("cost_usd", since, provider)

    def over_budget(self, name):
        if self.conn is None or not self.logging:
            return "budget non verifiable (pas de journal)"
        now = datetime.now(timezone.utc)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        if self.spent(day) >= float(cfg("LLM_DAILY_BUDGET_EUR")):
            return "budget journalier atteint"
        if self.spent(month) >= float(cfg("LLM_MONTHLY_BUDGET_EUR")):
            return "budget mensuel atteint"
        if name == "anthropic" and self.spent(day, "anthropic") >= \
                float(cfg("LLM_CLAUDE_DAILY_BUDGET_EUR")):
            return "budget Claude journalier atteint"
        if name == "gemini" and money("GEMINI_DAILY_BUDGET_USD") is not None and \
                self.spent_usd(day, "gemini") >= money("GEMINI_DAILY_BUDGET_USD"):
            return "budget Gemini journalier atteint"
        if name == "openrouter":
            if self.spent_usd(day, "openrouter") >= money("OPENROUTER_DAILY_BUDGET_USD"):
                return "budget OpenRouter journalier atteint"
            monthly = money("OPENROUTER_MONTHLY_BUDGET_USD")
            if monthly is not None and self.spent_usd(month, "openrouter") >= monthly:
                return "budget OpenRouter mensuel atteint"
            remaining = self.openrouter_remaining()
            if remaining is not None and remaining <= money("OPENROUTER_RESERVE_USD"):
                return "reserve OpenRouter atteinte"
        return None

    def openrouter_remaining(self):
        """Credit restant : API OpenRouter si lisible, sinon credit initial - depenses journalisees."""
        provider = self.providers.get("openrouter")
        live = provider.credits_remaining() if provider and hasattr(
            provider, "credits_remaining") else None
        if live is not None:
            return live
        return money("OPENROUTER_INITIAL_CREDIT_USD") - self.spent_usd("0000", "openrouter")

    def _costs(self, name, raw):
        """(cout EUR, cout USD) d'un appel reussi."""
        tokens_in, tokens_out = raw.get("input_tokens") or 0, raw.get("output_tokens") or 0
        if name == "local":
            return 0.0, 0.0
        if name == "openrouter":
            usd = raw.get("cost_usd")
            if usd is None:
                usd = tokens_in / 1e6 * float(cfg("OPENROUTER_PRICE_IN_USD")) + \
                    tokens_out / 1e6 * float(cfg("OPENROUTER_PRICE_OUT_USD"))
            return 0.0, round(float(usd), 6)
        if name == "gemini":
            return 0.0, round(tokens_in / 1e6 * float(cfg("GEMINI_PRICE_IN_USD"))
                              + tokens_out / 1e6 * float(cfg("GEMINI_PRICE_OUT_USD")), 6)
        key = "MISTRAL" if name == "mistral" else "ANTHROPIC"
        return round(tokens_in / 1e6 * float(cfg(f"LLM_PRICE_{key}_IN"))
                     + tokens_out / 1e6 * float(cfg(f"LLM_PRICE_{key}_OUT")), 6), 0.0

    def _log(self, **row):
        if self.conn is None or not self.logging:
            return
        try:
            self.conn.execute(
                "INSERT INTO llm_calls (provider, model, task, purpose, sensitivity, "
                "input_tokens, output_tokens, cost_eur, cost_usd, duration_ms, success, "
                "fallback_from, error, created_at) VALUES (:provider, :model, :task, :purpose, "
                ":sensitivity, :input_tokens, :output_tokens, :cost_eur, :cost_usd, "
                ":duration_ms, :success, :fallback_from, :error, :created_at)",
                {"purpose": None, "input_tokens": None, "output_tokens": None, "cost_eur": 0.0,
                 "cost_usd": 0.0, "duration_ms": None, "fallback_from": None, "error": None,
                 "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **row})
            self.conn.commit()
        except Exception:  # noqa: BLE001 - le journal ne casse jamais un appel
            pass

    # -------------------------------------------------------------- appels
    def _run(self, task, system, user, *, schema, max_tokens, sensitivity, purpose):
        sensitivity = sensitivity or classify(f"{system}\n{user}")
        if sensitivity == "secret" or contains_secret(user):
            self.last = {"provider": None, "reason": "secret detecte : aucun LLM",
                         "queued": False}
            return None
        previous = None
        reasons = {}
        for name in self.chain(task, sensitivity):
            ok, why = self.usable(name)
            if not ok:
                reasons[name] = why
                continue
            provider = self.providers[name]
            # Cloud : une seule tentative (aucune double facturation) ; local : 1 relance.
            attempts = 1 if provider.cloud else 2
            for attempt in range(attempts):
                started = time.perf_counter()
                try:
                    raw = provider.call(system, user, schema=schema, max_tokens=max_tokens,
                                        task=task)
                    result = _extract_json(raw["text"]) if schema else str(raw["text"]).strip()
                    if schema and not isinstance(result, dict):
                        raise LLMError("sortie JSON invalide")
                    self.breaker.success(name)
                    cost_eur, cost_usd = self._costs(name, raw)
                    self._log(provider=name, model=raw.get("model"), task=task, purpose=purpose,
                              sensitivity=sensitivity, input_tokens=raw.get("input_tokens"),
                              output_tokens=raw.get("output_tokens"), cost_eur=cost_eur,
                              cost_usd=cost_usd,
                              duration_ms=round((time.perf_counter() - started) * 1000),
                              success=1, fallback_from=previous)
                    self.last = {"provider": name, "model": raw.get("model"),
                                 "fallback_from": previous, "skipped": reasons}
                    return result
                except Exception as exc:  # noqa: BLE001 - toute panne -> fournisseur suivant
                    if isinstance(exc, NetworkDown) and provider.cloud:
                        self.offline_until = self.clock() + OFFLINE_COOLDOWN
                    elif not isinstance(exc, InvalidKey):
                        self.breaker.failure(name)
                    self._log(provider=name, model=getattr(provider, "model", None), task=task,
                              purpose=purpose, sensitivity=sensitivity, success=0,
                              duration_ms=round((time.perf_counter() - started) * 1000),
                              fallback_from=previous,
                              error=redact(str(exc)[:120] or type(exc).__name__,
                                           getattr(provider, "key", lambda: "")()))
                    reasons[name] = type(exc).__name__
                    if attempt + 1 < attempts and self.breaker.closed(name) and \
                            not isinstance(exc, (socket.timeout, TimeoutError)):
                        self.sleep(1.0)
                        continue
                    break
            previous = name
        self.last = {"provider": None, "reason": "aucun fournisseur disponible : mis en file",
                     "skipped": reasons, "queued": True}
        return None

    def chat_json(self, system, user, schema, *, num_predict=300, task="light",
                  sensitivity=None, purpose=None):
        return self._run(task, system, user, schema=schema, max_tokens=num_predict,
                         sensitivity=sensitivity, purpose=purpose)

    def complete_text(self, system, user, *, max_tokens=300, task="light", sensitivity=None,
                      purpose=None):
        return self._run(task, system, user, schema=None, max_tokens=max_tokens,
                         sensitivity=sensitivity, purpose=purpose)

    # -------------------------------------------------------------- local au demarrage
    def ensure_ready(self, *, retries=10, delay=3.0, start=True, sleep=time.sleep):
        """Qwen pret si possible (relances, demarrage local) ; le reste du routage continue."""
        local = self.providers.get("local")
        if local is None or not local.configured():
            return self.available()
        import mentor_council

        backend = mentor_council.OllamaBackend(model=local.model, url=local.url)
        backend.keep_alive = local.keep_alive()
        if backend.ensure_ready(retries=retries, delay=delay, start=start, sleep=sleep):
            return True
        return self.available()


_SHARED = {}


def bind(conn):
    """Routeur partage du processus, adosse au journal (budget)."""
    _SHARED["router"] = LLMRouter(conn)
    return _SHARED["router"]


def shared():
    if "router" not in _SHARED:
        _SHARED["router"] = LLMRouter(None)
    return _SHARED["router"]


def close_shared():
    """Ferme le journal du routeur partage (fin de processus, fin de module de test)."""
    router = _SHARED.pop("router", None)
    if router is not None and router.conn is not None:
        router.conn.close()
        router.conn = None
        router.logging = False


def default_router(conn=None):
    return LLMRouter(conn)


if __name__ == "__main__":
    print(json.dumps(LLMRouter().status(), ensure_ascii=False, indent=2))
