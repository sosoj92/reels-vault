#!/usr/bin/env python3
"""
llm_router.py - Routeur LLM central du Life Intelligence System.

Aucun module metier ne choisit son fournisseur : il declare une TACHE et une
SENSIBILITE, le routeur decide.

Taches :
  deterministic -> aucun LLM (erreur si appele) ;
  light         -> Qwen/Ollama local si disponible ET PC assez libre, sinon Mistral ;
  standard      -> Mistral API, sinon local ;
  complex       -> Claude API si configuree, sinon Mistral, sinon local ;
Sensibilite (classify) :
  public / internal -> cloud autorise selon le routage ;
  personal          -> cloud seulement si LLM_PERSONAL_CLOUD_ALLOWED=true ;
  sensitive         -> LOCAL UNIQUEMENT (sauf LLM_SENSITIVE_CLOUD_ALLOWED=true) ;
  secret            -> JAMAIS envoye a un LLM.

Chaque fournisseur : timeout, 1 relance courte, coupe-circuit (3 echecs -> pause
de 5 min), budget verifie avant tout appel cloud, journal `llm_calls` (fournisseur,
modele, tache, tokens, cout estime, duree, succes, repli) sans aucun contenu ni secret.
Claude n'est jamais le repli automatique de tout : seulement pour `complex`.
Cles absentes -> fournisseur simplement desactive.
"""

import ctypes
import json
import os
import re
import socket
import sqlite3
import time
from pathlib import Path
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent
# Journal et budget LLM : base dediee (vault.db n'est jamais modifie par le routeur).
DEFAULT_USAGE_DB = ROOT / "logs" / "llm_usage.db"

TASKS = ("deterministic", "light", "standard", "complex")
SENSITIVITIES = ("public", "internal", "personal", "sensitive", "secret")

DEFAULTS = {
    "OLLAMA_MODEL": "qwen3:1.7b",
    "OLLAMA_URL": "http://localhost:11434",
    "OLLAMA_IDLE_UNLOAD_MINUTES": "10",
    "MISTRAL_MODEL": "mistral-small-latest",
    "ANTHROPIC_MODEL": "",
    "LLM_LOCAL_ENABLED": "true",
    "LLM_CLOUD_ENABLED": "true",
    "LLM_DEFAULT_LIGHT": "local",
    "LLM_DEFAULT_STANDARD": "mistral",
    "LLM_DEFAULT_COMPLEX": "anthropic",
    "LLM_SENSITIVE_CLOUD_ALLOWED": "false",
    "LLM_PERSONAL_CLOUD_ALLOWED": "false",
    "LLM_DAILY_BUDGET_EUR": "1.00",
    "LLM_MONTHLY_BUDGET_EUR": "10.00",
    "LLM_CLAUDE_DAILY_BUDGET_EUR": "0.50",
    "LLM_LOCAL_MAX_CPU": "75",
    "LLM_LOCAL_MIN_FREE_MB": "1800",
    # Prix estimes (EUR par million de tokens), ajustables.
    "LLM_PRICE_MISTRAL_IN": "0.10", "LLM_PRICE_MISTRAL_OUT": "0.30",
    "LLM_PRICE_ANTHROPIC_IN": "5.00", "LLM_PRICE_ANTHROPIC_OUT": "25.00",
}
TIMEOUTS = {"local": 75.0, "mistral": 30.0, "anthropic": 60.0}
BREAKER_FAILURES = 3
BREAKER_COOLDOWN = 300.0

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


def _load_llm_env(path=ROOT / ".env"):
    """Reglages LLM du .env local (uniquement LLM_/OLLAMA_/MISTRAL_/ANTHROPIC_), sans ecraser."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key.startswith(("LLM_", "OLLAMA_", "MISTRAL_", "ANTHROPIC_")):
            os.environ.setdefault(key, value)


_load_llm_env()


def cfg(name):
    value = os.environ.get(name)
    return value if value not in (None, "") else DEFAULTS.get(name, "")


def flag(name):
    return str(cfg(name)).strip().lower() in ("1", "true", "on", "oui", "yes")


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

    def call(self, system, user, *, schema=None, max_tokens=300, timeout=None):
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

    def call(self, system, user, *, schema=None, max_tokens=300, timeout=None):
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

    def call(self, system, user, *, schema=None, max_tokens=600, timeout=None):
        if schema:
            system += ("\nReponds UNIQUEMENT avec un objet JSON conforme a ce schema : "
                       + json.dumps(schema, ensure_ascii=False))
        response = self.inner.complete(system, user, max_tokens=max_tokens,
                                       timeout=timeout or TIMEOUTS["anthropic"])
        return {"text": response.text, "input_tokens": None, "output_tokens": None,
                "model": response.model}


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


class LLMRouter:
    """Interface unique : chat_json / complete_text. Retourne None si rien d'autorise."""

    name = "router"

    def __init__(self, conn=None, providers=None, breaker=None, sleep=time.sleep,
                 usage_db=None):
        # `conn` (vault.db) n'est pas utilise pour le journal : base dediee llm_usage.db.
        self.providers = providers or {"local": OllamaProvider(), "mistral": MistralProvider(),
                                       "anthropic": AnthropicProvider()}
        self.breaker = breaker or BREAKER
        self.sleep = sleep
        self.last = None
        self.logging = False
        self.conn = None
        path = Path(usage_db or os.environ.get("LLM_USAGE_DB") or DEFAULT_USAGE_DB)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5)
            self.conn.executescript(LOG_SCHEMA)
            self.conn.commit()
            self.logging = True
        except Exception:  # noqa: BLE001 - journal impossible : le cloud reste ferme
            self.conn = None

    # -------------------------------------------------------------- politique
    def chain(self, task, sensitivity):
        if task == "deterministic":
            raise LLMError("tache deterministe : aucun LLM")
        if task not in TASKS:
            raise LLMError(f"tache inconnue : {task}")
        if sensitivity == "secret":
            return []
        primary = {"light": cfg("LLM_DEFAULT_LIGHT"), "standard": cfg("LLM_DEFAULT_STANDARD"),
                   "complex": cfg("LLM_DEFAULT_COMPLEX")}[task]
        order = {"light": [primary, "mistral", "local"],
                 "standard": [primary, "local"],
                 "complex": [primary, "mistral", "local"]}[task]
        seen = []
        for name in order:
            if name in self.providers and name not in seen:
                # Claude seulement pour les vraies taches complexes, jamais en repli.
                if name == "anthropic" and task != "complex":
                    continue
                seen.append(name)
        cloud_ok = sensitivity in ("public", "internal") or \
            (sensitivity == "personal" and flag("LLM_PERSONAL_CLOUD_ALLOWED")) or \
            (sensitivity == "sensitive" and flag("LLM_SENSITIVE_CLOUD_ALLOWED"))
        return [n for n in seen if cloud_ok or not self.providers[n].cloud]

    def usable(self, name):
        provider = self.providers[name]
        if not provider.configured():
            return False, "desactive"
        if not self.breaker.closed(name):
            return False, "coupe-circuit ouvert"
        if provider.cloud:
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
    def spent(self, since, provider=None):
        if self.conn is None or not self.logging:
            return float("inf")  # budget non verifiable : pas de cloud
        sql = "SELECT COALESCE(SUM(cost_eur), 0) FROM llm_calls WHERE created_at >= ?"
        params = [since]
        if provider:
            sql += " AND provider = ?"
            params.append(provider)
        return float(self.conn.execute(sql, params).fetchone()[0])

    def over_budget(self, name):
        now = datetime.now(timezone.utc)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        if self.conn is None or not self.logging:
            return "budget non verifiable (pas de journal)"
        if self.spent(day) >= float(cfg("LLM_DAILY_BUDGET_EUR")):
            return "budget journalier atteint"
        if self.spent(month) >= float(cfg("LLM_MONTHLY_BUDGET_EUR")):
            return "budget mensuel atteint"
        if name == "anthropic" and self.spent(day, "anthropic") >= \
                float(cfg("LLM_CLAUDE_DAILY_BUDGET_EUR")):
            return "budget Claude journalier atteint"
        return None

    def _cost(self, name, input_tokens, output_tokens):
        if name == "local":
            return 0.0
        key = "MISTRAL" if name == "mistral" else "ANTHROPIC"
        tokens_in = input_tokens or 0
        tokens_out = output_tokens or 0
        return round(tokens_in / 1e6 * float(cfg(f"LLM_PRICE_{key}_IN"))
                     + tokens_out / 1e6 * float(cfg(f"LLM_PRICE_{key}_OUT")), 6)

    def _log(self, **row):
        if self.conn is None or not self.logging:
            return
        try:
            self._insert(row)
        except Exception:  # noqa: BLE001 - le journal ne casse jamais un appel
            pass

    def _insert(self, row):
        self.conn.execute(
            "INSERT INTO llm_calls (provider, model, task, purpose, sensitivity, input_tokens, "
            "output_tokens, cost_eur, duration_ms, success, fallback_from, error, created_at) "
            "VALUES (:provider, :model, :task, :purpose, :sensitivity, :input_tokens, "
            ":output_tokens, :cost_eur, :duration_ms, :success, :fallback_from, :error, "
            ":created_at)",
            {"purpose": None, "input_tokens": None, "output_tokens": None, "cost_eur": 0.0,
             "duration_ms": None, "fallback_from": None, "error": None,
             "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **row})
        self.conn.commit()


    # -------------------------------------------------------------- appels
    def _run(self, task, system, user, *, schema, max_tokens, sensitivity, purpose):
        sensitivity = sensitivity or classify(f"{system}\n{user}")
        if sensitivity == "secret" or contains_secret(user):
            self.last = {"provider": None, "reason": "secret detecte : aucun LLM"}
            return None
        previous = None
        reasons = {}
        for name in self.chain(task, sensitivity):
            ok, why = self.usable(name)
            if not ok:
                reasons[name] = why
                continue
            provider = self.providers[name]
            for attempt in range(2):
                started = time.perf_counter()
                try:
                    raw = provider.call(system, user, schema=schema, max_tokens=max_tokens)
                    result = _extract_json(raw["text"]) if schema else str(raw["text"]).strip()
                    if schema and not isinstance(result, dict):
                        raise LLMError("sortie JSON invalide")
                    self.breaker.success(name)
                    self._log(provider=name, model=raw.get("model"), task=task, purpose=purpose,
                              sensitivity=sensitivity, input_tokens=raw.get("input_tokens"),
                              output_tokens=raw.get("output_tokens"),
                              cost_eur=self._cost(name, raw.get("input_tokens"),
                                                  raw.get("output_tokens")),
                              duration_ms=round((time.perf_counter() - started) * 1000),
                              success=1, fallback_from=previous)
                    self.last = {"provider": name, "model": raw.get("model"),
                                 "fallback_from": previous, "skipped": reasons}
                    return result
                except (LLMError, OSError, socket.timeout, TimeoutError, ValueError, KeyError,
                        urllib.error.URLError, Exception) as exc:  # noqa: BLE001
                    self.breaker.failure(name)
                    self._log(provider=name, model=getattr(provider, "model", None), task=task,
                              purpose=purpose, sensitivity=sensitivity, success=0,
                              duration_ms=round((time.perf_counter() - started) * 1000),
                              fallback_from=previous,
                              error=redact(type(exc).__name__, getattr(provider, "key",
                                                                       lambda: "")()))
                    reasons[name] = type(exc).__name__
                    if attempt == 0 and self.breaker.closed(name) and \
                            not isinstance(exc, (socket.timeout, TimeoutError)):
                        self.sleep(1.0)
                        continue
                    break
            previous = name
        self.last = {"provider": None, "reason": "aucun fournisseur disponible",
                     "skipped": reasons}
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
    """Routeur partage du processus, adosse a la base (budget + journal)."""
    _SHARED["router"] = LLMRouter(conn)
    return _SHARED["router"]


def shared():
    """Routeur partage ; sans base liee, le cloud reste ferme (budget non verifiable)."""
    if "router" not in _SHARED:
        _SHARED["router"] = LLMRouter(None)
    return _SHARED["router"]

def default_router(conn=None):
    return LLMRouter(conn)


if __name__ == "__main__":
    print(json.dumps(LLMRouter().status(), ensure_ascii=False, indent=2))
