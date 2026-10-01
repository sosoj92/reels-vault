#!/usr/bin/env python3
"""
mentor_council.py - Agency Agents consultes par le Mentor, avec Qwen local.

Reutilise le registre (vault_agent_registry), le routage (vault_orchestrator)
et les playbooks compacts (vault_voice_bridge._compact_playbook). Aucun agent
n'est reconstruit. Le LLM est Qwen via Ollama (local) ; aucun cloud.

Modes (definition de Rosy) :
  A = agent explicitement nomme par Rosy ;
  B = 1 a 3 agents choisis automatiquement pour une question de Rosy ;
  C = 1 a 3 agents choisis par le Mentor pour evaluer un candidat avant suggestion.

Chaine proactive (mode C) :
  candidat shortlist -> Qwen (hypothese) -> agents C (verdicts) -> filtre final
  -> suggestion SUG-xxxx (ou rejet journalise).

Selection des agents deterministe : conseillers du profil du projet, type de
candidat, registre. Qwen ne choisit jamais les agents.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import mentor_proactive_store as store
from vault_security_gate import UNTRUSTED_OPEN

# Marque des extraits de sources envoyes aux LLM (regle : llm_router + vault_security_gate).
UNTRUSTED_LABEL = "UNTRUSTED SOURCE CONTENT " + UNTRUSTED_OPEN.strip("<>")

MAX_AGENTS = 3
import llm_router  # noqa: E402 - le routeur choisit le moteur ; aucun modele code ici

MODEL = os.environ.get("VAULT_MENTOR_MODEL") or llm_router.cfg("OLLAMA_MODEL")
OLLAMA_URL = (os.environ.get("VAULT_OLLAMA_URL") or llm_router.cfg("OLLAMA_URL")).rstrip("/")

# Regle initiale de Rosy : au moins un agent >= 4/5 et aucune objection bloquante.
MIN_BEST_VALUE = 4
FATIGUE_REJECT = 2.0
FATIGUE_WINDOW_DAYS = 30
FATIGUE_WEIGHTS = {"off_topic": 1.0, "bad_idea": 1.0, "already_known": 0.5,
                   "not_now": 0.25, "no_thanks": 0.5}
MAX_REVIEW_ATTEMPTS = 3

# Alias d'agents (configuration d'interface, pas de connaissance metier).
AGENT_ALIASES = {
    "cfo": "Chief Financial Officer",
    "directeur financier": "Chief Financial Officer",
    "daf": "Chief Financial Officer",
    "growth": "Growth Hacker",
    "growth hacker": "Growth Hacker",
    "recruitment agent": "Recruitment Specialist",
    "agent recrutement": "Recruitment Specialist",
    "recruteur": "Recruitment Specialist",
    "chief of staff": "Chief of Staff",
    "directeur de cabinet": "Chief of Staff",
    "workflow": "Workflow Architect",
    "product manager": "Product Manager",
    "pm": "Product Manager",
    "commercial": "Outbound Strategist",
    "sales": "Outbound Strategist",
    "operations": "Operations Manager",
    "ops": "Operations Manager",
}

# Regard par defaut selon le type de candidat (complete les conseillers du projet).
TYPE_ADVISORS = {
    "opportunity": ["Growth Hacker"],
    "convergence": ["Trend Researcher"],
    "reactivation": ["Chief of Staff"],
    "tension": ["Chief of Staff"],
    "forgotten": ["Chief of Staff"],
    "next_action": ["Chief of Staff"],
}
GENERALIST = "Chief of Staff"

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["support", "neutral", "oppose"]},
        "value_score": {"type": "integer", "minimum": 0, "maximum": 5},
        "proposed_action": {"type": "string"},
        "main_objection": {"type": "string"},
        "blocking_objection": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "value_score", "proposed_action", "main_objection",
                 "blocking_objection", "confidence", "evidence_refs"],
}

HYPOTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "hypothesis": {"type": "string"},
        "actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "hypothesis", "actions"],
}

VERDICTS_PANEL_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"agent": {"type": "string"}, **VERDICT_SCHEMA["properties"]},
                "required": ["agent", *VERDICT_SCHEMA["required"]],
            },
        },
    },
    "required": ["verdicts"],
}

PANEL_SCHEMA = {
    "type": "object",
    "properties": {
        "opinions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"agent": {"type": "string"}, "opinion": {"type": "string"}},
                "required": ["agent", "opinion"],
            },
        },
        "synthesis": {"type": "string"},
    },
    "required": ["opinions", "synthesis"],
}


class CouncilError(RuntimeError):
    pass


# ------------------------------------------------------------------ LLM local

class OllamaBackend:
    """Qwen via Ollama, JSON contraint par schema. Local uniquement."""

    name = "ollama"

    def __init__(self, model=MODEL, url=OLLAMA_URL, timeout=90):
        self.model = model
        self.url = url
        self.timeout = timeout
        self.keep_alive = f"{int(llm_router.cfg('OLLAMA_IDLE_UNLOAD_MINUTES'))}m"
        self.last = None

    def available(self):
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=2) as response:
                models = [m.get("name") for m in json.load(response).get("models", [])]
            return self.model in models
        except Exception:  # noqa: BLE001
            return False

    def _reachable(self):
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=2):
                return True
        except Exception:  # noqa: BLE001
            return False

    def _start_local_server(self):
        """Lance le serveur Ollama LOCAL s'il est installe mais arrete (jamais le cloud)."""
        exe = shutil.which("ollama") or os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
        if not exe or not os.path.exists(exe):
            return False
        flags = 0x08000000 | 0x00000008 if os.name == "nt" else 0  # sans fenetre, detache
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=flags)
        return True

    def warm_up(self):
        """Charge le modele en memoire (evite l'attente du premier appel)."""
        request = urllib.request.Request(
            self.url + "/api/generate",
            data=json.dumps({"model": self.model, "keep_alive": self.keep_alive}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                return True
        except Exception:  # noqa: BLE001
            return False

    def ensure_ready(self, *, retries=10, delay=3.0, start=True, sleep=time.sleep):
        """Test de disponibilite avec relances ; demarre Ollama localement si besoin."""
        started = False
        for attempt in range(max(1, retries)):
            if self.available():
                self.warm_up()
                return True
            if start and not started and not self._reachable():
                started = self._start_local_server()
            if attempt < retries - 1:
                sleep(delay)
        return False

    def chat_json(self, system, user, schema, *, num_predict=260, **_routing):
        payload = {
            "model": self.model, "stream": False, "think": False, "format": schema,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0, "num_predict": num_predict, "num_ctx": 4096},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        request = urllib.request.Request(
            self.url + "/api/chat", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            # La sortie du `with` ferme la socket : Ollama arrete de generer.
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = json.load(response)
            return json.loads(raw["message"]["content"])
        except (socket.timeout, TimeoutError, OSError, ValueError, KeyError):
            return None


def _model_label(backend):
    last = getattr(backend, "last", None) or {}
    if last.get("provider"):
        return f"{last['provider']}:{last.get('model') or '?'}"
    return f"{getattr(backend, 'name', 'llm')}:{getattr(backend, 'model', MODEL)}"


# ------------------------------------------------------------------ agents

_REGISTRY = None


def registry():
    global _REGISTRY
    if _REGISTRY is None:
        from vault_agent_registry import discover_agents

        _REGISTRY = {agent.name.casefold(): agent for agent in discover_agents()}
    return _REGISTRY


def _fold(text):
    from vault_db import fold

    return " ".join(fold(str(text or "")).split())


def resolve_agent(name):
    """Nom, slug ou alias -> profil Agency existant (ou None)."""
    wanted = _fold(name)
    if not wanted:
        return None
    alias = AGENT_ALIASES.get(wanted)
    agents = registry()
    if alias:
        return agents.get(alias.casefold())
    if wanted in {k for k in agents}:
        return agents[wanted]
    for agent in agents.values():
        if agent.slug.replace("-", " ").casefold() == wanted:
            return agent
    return None


def agents_in_text(text):
    """Agents cites dans un texte (noms complets et alias), dans l'ordre d'apparition."""
    folded = " " + _fold(text) + " "
    found = []
    candidates = [(a.name, a.name) for a in registry().values()]
    candidates += [(alias, target) for alias, target in AGENT_ALIASES.items()]
    for label, target in sorted(candidates, key=lambda item: -len(item[0])):
        pattern = r"(?<![a-z0-9])" + re.escape(_fold(label)) + r"(?![a-z0-9])"
        match = re.search(pattern, folded)
        if not match:
            continue
        agent = registry().get(target.casefold())
        if agent and all(agent.name != a.name for _, a in found):
            found.append((match.start(), agent))
            folded = folded[:match.start()] + " " * (match.end() - match.start()) \
                + folded[match.end():]
    return [agent for _, agent in sorted(found, key=lambda item: item[0])]


def _profile(conn, project_id):
    if project_id is None:
        return None
    import mentor_project_profiles as profiles

    return profiles.get_profile(conn, project_id)


def select_for_candidate(conn, candidate):
    """Mode C : conseillers du projet, puis regard du type de candidat. Max 3."""
    chosen = []

    def add(name, reason):
        agent = registry().get(name.casefold())
        if agent and len(chosen) < MAX_AGENTS and all(agent.name != a["name"] for a in chosen):
            chosen.append({"name": agent.name, "slug": agent.slug, "reason": reason,
                           "agent": agent})

    profile = _profile(conn, candidate.get("project_id"))
    for name in (profile or {}).get("advisors", []):
        add(name, f"conseiller du projet {profile['project_name']}")
    for name in TYPE_ADVISORS.get(candidate["candidate_type"], []):
        add(name, f"regard adapte a un candidat '{candidate['candidate_type']}'")
    if not chosen:
        add(GENERALIST, "generaliste par defaut")
    return chosen


def projects_in_text(conn, text):
    """Projets nommes dans un texte (noms du profil ou de l'entite)."""
    import mentor_project_links as links

    folded = _fold(text)
    found = []
    try:
        projects = links.load_projects(conn)
    except Exception:  # noqa: BLE001 - schema absent
        return []
    for pid, project in projects.items():
        for name in project["profile"]["names"]:
            if re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", folded):
                found.append(pid)
                break
    return found


def select_for_question(conn, question, *, include=(), exclude=(), replace=None,
                        project_ids=()):
    """
    Mode A si des agents sont nommes, sinon mode B : conseillers du projet cite,
    puis routage Agency existant, puis recherche dans le registre. Max 3.
    `include` / `exclude` / `replace` : ajustements demandes par Rosy.
    """
    chosen = []
    excluded = {a.casefold() for a in exclude}

    def add(agent, reason):
        if agent and agent.name.casefold() not in excluded and len(chosen) < MAX_AGENTS \
                and all(agent.name != c["name"] for c in chosen):
            chosen.append({"name": agent.name, "slug": agent.slug, "reason": reason,
                           "agent": agent})

    if replace:
        for name in replace:
            add(registry().get(name.casefold()), "demande explicite de Rosy")
        return "A", chosen

    named = agents_in_text(question)
    for agent in named:
        add(agent, "nomme explicitement")
    for name in include:
        add(registry().get(name.casefold()), "ajoute a la demande de Rosy")
    if named and not include:
        return "A", chosen

    for pid in list(project_ids) + [p for p in projects_in_text(conn, question)
                                    if p not in project_ids]:
        profile = _profile(conn, pid)
        for name in (profile or {}).get("advisors", []):
            add(registry().get(name.casefold()), f"conseiller du projet {profile['project_name']}")
    if len(chosen) < 1:
        try:
            import vault_orchestrator as orchestrator

            selection = orchestrator.select_agents(question, agents=list(registry().values()),
                                                   max_agents=2)
            if any(r["route"] != "fallback" for r in selection["routes"]):
                routes = ", ".join(r["route"] for r in selection["routes"] if r["route"] != "fallback")
                for agent in selection["agents"]:
                    add(agent, f"routage Agency ({routes})")
        except Exception:  # noqa: BLE001
            pass
    if len(chosen) < 1:
        from vault_agent_registry import search_agents

        for agent in search_agents(question, top=2):
            add(agent, "correspondance avec la question dans le registre")
    if not chosen:
        add(registry().get(GENERALIST.casefold()), "generaliste par defaut")
    return ("A" if named else "B"), chosen


# ------------------------------------------------------------------ contexte

def _source_brief(conn, source_id):
    row = conn.execute(
        "SELECT s.id, s.title, s.author, s.source_url, c.description_manual, "
        "c.description_ai, c.description_original, c.category FROM intelligence_sources s "
        "LEFT JOIN contents c ON c.id = s.content_id WHERE s.id = ?",
        (source_id,),
    ).fetchone()
    if row is None:
        return None
    row = dict(row)
    text = row.get("description_manual") or row.get("description_original") \
        or row.get("description_ai") or ""
    return {"ref": f"source:{row['id']}", "title": row.get("title"), "author": row.get("author"),
            "url": row.get("source_url"), "excerpt": " ".join(text.split())[:420],
            "trust": UNTRUSTED_LABEL}


def candidate_context(conn, candidate):
    profile = _profile(conn, candidate.get("project_id"))
    sources = [b for b in (_source_brief(conn, sid) for sid in candidate["source_ids"][:4]) if b]
    refs = [s["ref"] for s in sources]
    refs += [f"insight:{i}" for i in candidate.get("insight_ids", [])]
    refs += [f"task:{t}" for t in candidate.get("task_ids", [])]
    refs += [f"claim:{c}" for c in candidate.get("claim_ids", [])]
    return {
        "candidate_id": candidate["candidate_id"],
        "type": candidate["candidate_type"],
        "title": candidate["title"],
        "score": candidate["score"],
        "project": {
            "id": candidate.get("project_id"),
            "name": (profile or {}).get("project_name"),
            "description": (profile or {}).get("description"),
        },
        "sources": sources,
        "allowed_refs": refs,
    }


def _playbook(agent, max_chars=1400):
    from vault_voice_bridge import _compact_playbook

    return _compact_playbook(agent.markdown, max_chars=max_chars)


# ------------------------------------------------------------------ Qwen : hypothese

_INTERNAL_WORDING = re.compile(
    r"\b(le|ce|du|au) candidat\b(?=\s+(a |a\u0300|présente|presente|propose|identifi|"
    r"détect|detect|met|montre|concerne|porte|indique|souligne|décrit|decrit|mentionne))",
    re.IGNORECASE)


def user_wording(text):
    """« Le candidat a identifie... » -> « Une source de ton Vault a identifie... »."""
    def swap(match):
        article = match.group(1).lower()
        replacement = {"le": "une source de ton Vault", "ce": "cette source de ton Vault",
                       "du": "d'une source de ton Vault", "au": "a une source de ton Vault"}
        value = replacement[article]
        return value[0].upper() + value[1:] if match.group(1)[0].isupper() else value

    return _INTERNAL_WORDING.sub(swap, str(text or ""))


def build_hypothesis(backend, context):
    system = ("Tu es le Mentor du Life Intelligence System de Rosy. Tu reponds en francais, "
              "uniquement avec le JSON demande, sans inventer : seules les sources fournies "
              "comptent.")
    user = (
        "Piste reperee dans le Vault de Rosy (regles deterministes) :\n"
        + json.dumps({k: context[k] for k in ("type", "title", "score", "project", "sources")},
                     ensure_ascii=False)
        + "\n\nRedige pour Rosy, en t'adressant a elle, sans employer les mots « candidat » "
          "ni « piste » : title (8 mots max), summary (1 a 2 phrases), hypothesis (ce qui "
          "pourrait lui etre utile pour ce projet), actions (1 ou 2 actions concretes et prudentes)."
    )
    result = backend.chat_json(system, user, HYPOTHESIS_SCHEMA, num_predict=240,
                               task="standard", purpose="mentor_hypothesis")
    if not isinstance(result, dict):
        return None
    actions = [str(a).strip()[:200] for a in result.get("actions") or [] if str(a).strip()][:2]
    title = str(result.get("title") or "").strip()[:90]
    summary = " ".join(str(result.get("summary") or "").split())[:320]
    if not title or not summary:
        return None
    return {"title": user_wording(title), "summary": user_wording(summary),
            "hypothesis": user_wording(" ".join(str(result.get("hypothesis") or "").split())[:500]),
            "actions": [user_wording(a) for a in actions]}


# ------------------------------------------------------------------ agents : verdicts

def _validate_verdict(raw, allowed_refs):
    if not isinstance(raw, dict):
        return None
    try:
        value = max(0, min(5, int(raw.get("value_score"))))
        confidence = max(0.0, min(1.0, float(raw.get("confidence"))))
    except (TypeError, ValueError):
        return None
    verdict = raw.get("verdict") if raw.get("verdict") in ("support", "neutral", "oppose") \
        else "neutral"
    refs = [r for r in (raw.get("evidence_refs") or []) if r in allowed_refs]
    objection = str(raw.get("main_objection") or "").strip()[:300]
    if objection.casefold() in ("blocking_objection", "none", "aucune", "n/a", "-"):
        objection = ""
    blocking = bool(raw.get("blocking_objection"))
    # Coherence (petit modele local) : une objection n'est bloquante que si l'agent
    # s'oppose ou juge la valeur faible (<= 2/5), avec une objection explicite.
    incoherent = blocking and (verdict != "oppose" and value > 2 or not objection)
    if incoherent:
        blocking = False
    return {
        "verdict": verdict,
        "value_score": value,
        "proposed_action": str(raw.get("proposed_action") or "").strip()[:300],
        "main_objection": objection,
        "blocking_objection": blocking,
        "incoherent_blocking_flag": incoherent,
        "confidence": round(confidence, 2),
        "evidence_refs": refs,
        "invalid_refs": len(raw.get("evidence_refs") or []) - len(refs),
    }


def consult_agent(backend, agent, context, hypothesis):
    system = (
        f"Tu es {agent.name} (Agency Agents). Playbook resume :\n{_playbook(agent)}\n\n"
        "Rosy dirige ce projet ; le Mentor lui propose une idee tiree de son Vault. "
        "Evalue-la pour elle. Reponds en francais, uniquement en JSON :\n"
        "- verdict : support, neutral ou oppose ;\n"
        "- value_score : 0 a 5 ; 4 ou 5 seulement si c'est utile ET actionnable maintenant ;\n"
        "- proposed_action : une action concrete et prudente (jamais executee automatiquement) ;\n"
        "- main_objection : la principale reserve, en une phrase (vide si aucune) ;\n"
        "- blocking_objection : true UNIQUEMENT si l'idee est fausse, risquee, hors sujet ou "
        "deja connue ; un manque de details n'est PAS bloquant ;\n"
        "- confidence : 0 a 1 ;\n"
        "- evidence_refs : uniquement parmi les references fournies."
    )
    user = json.dumps({
        "projet": context["project"],
        "candidat": {k: context[k] for k in ("type", "title", "score")},
        "hypothese": hypothesis,
        "sources": context["sources"],
        "references_autorisees": context["allowed_refs"],
    }, ensure_ascii=False)
    return _validate_verdict(backend.chat_json(system, user, VERDICT_SCHEMA, num_predict=220,
                                               task="standard",
                                               purpose="agent_independent_review"),
                             context["allowed_refs"])


def consult_panel(backend, agents, context, hypothesis):
    """
    Mode C courant : UN appel structure produit le verdict de chacun des 1 a 3 agents.
    (independent_review=True fait des contre-expertises separees.)
    """
    briefs = [{"agent": a["name"], "role": a["agent"].description[:300],
               "playbook": _playbook(a["agent"], max_chars=600)} for a in agents]
    system = (
        "Tu fais evaluer une idee pour Rosy par plusieurs specialistes Agency Agents, chacun "
        "selon son playbook. Rosy dirige ce projet. Reponds en francais, uniquement en JSON : "
        "un verdict par specialiste (agent = son nom exact) avec verdict (support/neutral/"
        "oppose), value_score 0 a 5 (4-5 seulement si utile ET actionnable maintenant), "
        "proposed_action, main_objection, blocking_objection (true UNIQUEMENT si l'idee est "
        "fausse, risquee, hors sujet ou deja connue), confidence 0 a 1, evidence_refs "
        "(uniquement parmi les references fournies). Chaque specialiste garde son angle.")
    user = json.dumps({
        "specialistes": briefs,
        "projet": context["project"],
        "idee": {k: context[k] for k in ("type", "title", "score")},
        "hypothese": hypothesis,
        "sources": context["sources"],
        "references_autorisees": context["allowed_refs"],
    }, ensure_ascii=False)
    raw = backend.chat_json(system, user, VERDICTS_PANEL_SCHEMA, num_predict=520,
                            task="standard", purpose="agent_panel_mode_c")
    verdicts = {}
    for item in (raw or {}).get("verdicts") or []:
        if isinstance(item, dict) and item.get("agent"):
            verdicts[item["agent"]] = _validate_verdict(item, context["allowed_refs"])
    return {a["name"]: verdicts.get(a["name"]) for a in agents}


# ------------------------------------------------------------------ filtre final

def fatigue(conn, project_id, suggestion_type, now):
    since = store.now_iso(now - timedelta(days=FATIGUE_WINDOW_DAYS))
    total = 0.0
    for item in store.feedback_since(conn, since):
        if item["project_id"] == project_id and item["suggestion_type"] == suggestion_type:
            total += FATIGUE_WEIGHTS.get(item["reason"], 0.5)
    return round(total, 2)


def project_is_active(conn, project_id):
    if project_id is None:
        return False, "aucun projet"
    profile = _profile(conn, project_id)
    if profile is None or profile["profile_status"] != "complete":
        return False, "profil de projet incomplet"
    if not profile.get("active", 1):
        return False, "projet marque inactif"
    row = conn.execute("SELECT attention_status FROM intelligence_entities WHERE id = ?",
                       (project_id,)).fetchone()
    if row is None or row[0] == "dismissed":
        return False, "projet ecarte"
    return True, None


def decide(verdicts, hypothesis):
    """Regle initiale : >= 1 agent a 4/5 et aucune objection bloquante ; actionnable."""
    valid = [v for v in verdicts if v["verdict_data"]]
    if not valid:
        return False, "no_agent_verdict"
    blocking = [v for v in valid if v["verdict_data"]["blocking_objection"]]
    if blocking:
        return False, "blocking_objection:" + ",".join(v["name"] for v in blocking)
    best = max(v["verdict_data"]["value_score"] for v in valid)
    if best < MIN_BEST_VALUE:
        return False, f"agents_not_convinced:max={best}"
    actions = [v["verdict_data"]["proposed_action"] for v in valid
               if v["verdict_data"]["proposed_action"]]
    if not actions and not (hypothesis or {}).get("actions"):
        return False, "not_actionable"
    return True, None


def disagreement(verdicts):
    scores = [v["verdict_data"]["value_score"] for v in verdicts if v["verdict_data"]]
    stances = {v["verdict_data"]["verdict"] for v in verdicts if v["verdict_data"]}
    if len(scores) >= 2 and (max(scores) - min(scores) >= 3 or {"support", "oppose"} <= stances):
        return {"scores": {v["name"]: v["verdict_data"]["value_score"]
                           for v in verdicts if v["verdict_data"]},
                "stances": {v["name"]: v["verdict_data"]["verdict"]
                            for v in verdicts if v["verdict_data"]}}
    return None


def review_candidate(conn, candidate, backend, *, now=None, independent_review=False):
    """
    shortlist -> Qwen -> agents C -> filtre -> suggestion. Retourne un resume.
    LLM indisponible : le candidat reste shortlist (nouvel essai plus tard).
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    at = store.now_iso(now)
    cid = candidate["candidate_id"]
    outcome = {"candidate_id": cid, "title": candidate["title"]}

    if candidate["status"] != "shortlisted":
        return {**outcome, "result": "skipped", "reason": f"status:{candidate['status']}"}
    active, why = project_is_active(conn, candidate.get("project_id"))
    if not active:
        store.set_candidate_status(conn, cid, "rejected", reason=f"project_inactive:{why}", at=at)
        conn.commit()
        return {**outcome, "result": "rejected", "reason": f"project_inactive:{why}"}
    tiredness = fatigue(conn, candidate.get("project_id"), candidate["candidate_type"], now)
    if tiredness >= FATIGUE_REJECT:
        store.set_candidate_status(conn, cid, "rejected", reason=f"fatigue:{tiredness}", at=at)
        conn.commit()
        return {**outcome, "result": "rejected", "reason": f"fatigue:{tiredness}"}

    attempts = int(candidate["evidence"].get("review_attempts", 0))
    if backend is None or not backend.available():
        return {**outcome, "result": "waiting", "reason": "local_llm_unavailable"}

    context = candidate_context(conn, candidate)
    hypothesis = build_hypothesis(backend, context)
    agents = select_for_candidate(conn, candidate)
    session_id = f"C:{cid}:{at}"
    verdicts = []
    if hypothesis is not None:
        panel = None if independent_review else consult_panel(backend, agents, context,
                                                               hypothesis)
        for item in agents:
            data = consult_agent(backend, item["agent"], context, hypothesis) \
                if independent_review else panel.get(item["name"])
            verdicts.append({"name": item["name"], "reason": item["reason"], "verdict_data": data})
            if data:
                store.record_consultation(
                    conn, agent_id=item["slug"], agent_name=item["name"], mode="C",
                    session_id=session_id, candidate_id=cid,
                    inputs={"candidate": cid, "hypothesis": hypothesis,
                            "selection_reason": item["reason"]},
                    verdict=data["verdict"], value_score=data["value_score"],
                    proposed_action=data["proposed_action"],
                    main_objection=data["main_objection"], confidence=data["confidence"],
                    evidence_refs=data["evidence_refs"], model=_model_label(backend),
                    at=at, commit=False,
                )
        conn.commit()

    if hypothesis is None or not any(v["verdict_data"] for v in verdicts):
        evidence = dict(candidate["evidence"], review_attempts=attempts + 1)
        if attempts + 1 >= MAX_REVIEW_ATTEMPTS:
            store.update_candidate(conn, cid, {"evidence": evidence, "status": "expired",
                                               "rejection_reason": "llm_output_invalid",
                                               "closed_at": at}, at=at)
            conn.commit()
            return {**outcome, "result": "expired", "reason": "llm_output_invalid"}
        store.update_candidate(conn, cid, {"evidence": evidence}, at=at)
        conn.commit()
        return {**outcome, "result": "waiting", "reason": "llm_output_invalid"}

    accepted, reason = decide(verdicts, hypothesis)
    conflict = disagreement(verdicts)
    panel = [{"agent": v["name"], "why_selected": v["reason"], **(v["verdict_data"] or {})}
             for v in verdicts]
    if not accepted:
        store.set_candidate_status(conn, cid, "rejected", reason=reason, at=at)
        store.update_candidate(conn, cid, {"evidence": dict(
            candidate["evidence"], review={"hypothesis": hypothesis, "agents": panel,
                                           "disagreement": conflict})}, at=at)
        conn.commit()
        return {**outcome, "result": "rejected", "reason": reason, "agents": panel}

    actions = [v["verdict_data"]["proposed_action"] for v in verdicts
               if v["verdict_data"] and v["verdict_data"]["proposed_action"]]
    evidence = {
        "candidate": {k: candidate[k] for k in ("candidate_type", "score", "source_ids",
                                                "insight_ids", "task_ids", "claim_ids")},
        "project": context["project"],
        "sources": context["sources"],
        "hypothesis": hypothesis["hypothesis"],
        "actions": (hypothesis["actions"] or actions)[:2],
        "agents": panel,
        "disagreement": conflict,
        "confidence": round(max(v["verdict_data"]["confidence"] for v in verdicts
                                if v["verdict_data"]), 2),
        "llm": _model_label(backend),
        "review": "independent" if independent_review else "panel",
        "session_id": session_id,
    }
    explanation = "Hypothese (synthese locale Qwen, a verifier) : " + hypothesis["hypothesis"]
    if conflict:
        explanation += " | Desaccord entre agents : " + json.dumps(conflict["scores"],
                                                                   ensure_ascii=False)
    suggestion = store.promote_candidate(
        conn, cid, title=hypothesis["title"], summary=hypothesis["summary"],
        explanation=explanation, evidence=evidence, at=at, commit=False,
    )
    store.attach_consultations(conn, session_id, suggestion["suggestion_id"], commit=False)
    conn.commit()
    return {**outcome, "result": "suggestion", "suggestion_id": suggestion["suggestion_id"],
            "agents": panel, "disagreement": conflict}


def review_shortlist(conn, backend, *, now=None, limit=2, independent_review=False):
    """Revue des meilleurs candidats shortlist (operation lourde : PC au repos)."""
    results = []
    for candidate in store.list_candidates(conn, "shortlisted")[:max(0, int(limit))]:
        results.append(review_candidate(conn, candidate, backend, now=now,
                                        independent_review=independent_review))
    return results


# ------------------------------------------------------------------ modes A / B (reactif)

def answer_with_agents(conn, backend, question, agents, *, vault_answer, sources, mode,
                       session_id=None, context=None, task="standard"):
    """
    Avis des agents choisis (A : un agent nomme ; B : 1 a 3 agents) sur la question,
    fondes sur les sources retrouvees par le Vault. Un seul appel Qwen.
    """
    if not agents or backend is None or not backend.available():
        return None
    briefs = [{"agent": a["name"], "role": a["agent"].description[:300],
               "playbook": _playbook(a["agent"], max_chars=700)} for a in agents]
    system = ("Tu fais parler des specialistes Agency Agents pour Rosy. Francais, JSON "
              "uniquement. Chaque avis : 2 a 4 phrases. Reponds directement au probleme "
              "fourni. N'explique pas ton role generique. N'introduis pas d'entreprise, "
              "marche, outil, loi ou fait specifique absent des preuves fournies. Si une "
              "information n'est pas supportee, ne l'invente pas. Aucune action externe : "
              "proposer, jamais executer.")
    payload = {
        "question": question,
        "specialistes": briefs,
        "reponse_du_vault": vault_answer[:1500],
        "sources": [{"ref": f"source:{s.get('source_id')}", "title": s.get("title"),
                     "extrait": (s.get("snippet") or "")[:300], "trust": UNTRUSTED_LABEL}
                    for s in sources[:4]],
    }
    if context:
        # Contexte actif explicite (suggestion ouverte, message repondu) : il prime.
        payload["contexte_actif"] = context
        system += (" Un CONTEXTE ACTIF est fourni : la question porte sur lui ; "
                   "ne parle pas d'autres sujets.")
    user = json.dumps(payload, ensure_ascii=False)
    result = backend.chat_json(system, user, PANEL_SCHEMA, num_predict=520,
                               task=task, purpose=f"agents_mode_{mode}")
    # Preuves admises : tout le payload sauf les playbooks generiques des agents.
    allowed = allowed_text(conn, json.dumps(
        {**payload, "specialistes": [a["name"] for a in agents]}, ensure_ascii=False))
    if not isinstance(result, dict):
        return None
    opinions = []
    known = {a["name"] for a in agents}
    removed = []
    for item in result.get("opinions") or []:
        if isinstance(item, dict) and item.get("agent") in known and item.get("opinion"):
            text, dropped = filter_unsupported(" ".join(str(item["opinion"]).split()),
                                               allowed)
            removed += dropped
            opinions.append({"agent": item["agent"], "opinion": _trim(text, 700) or
                             "(avis ecarte : affirmations non etayees par les preuves du Vault)"})
    if store.writes_allowed(conn) and store.schema_present(conn):
        for item in opinions:
            store.record_consultation(
                conn, agent_id=next(a["slug"] for a in agents if a["name"] == item["agent"]),
                agent_name=item["agent"], mode=mode, session_id=session_id,
                inputs={"question": question[:500]}, verdict="opinion",
                evidence_refs=[f"source:{s.get('source_id')}" for s in sources[:4]],
                model=_model_label(backend), commit=False,
            )
        conn.commit()
    synthesis, dropped = filter_unsupported(
        " ".join(str(result.get("synthesis") or "").split()), allowed)
    return {"opinions": opinions, "synthesis": _trim(synthesis, 700),
            "removed_claims": removed + dropped}


# ------------------------------------------------------------------ affirmations non etayees

_ROLE_TALK = re.compile(r"^(en tant que|as an? |je suis (un|une|le|la) |mon role|mon rôle|"
                        r"en ma qualite|en ma qualité|dans mon role|dans mon rôle)", re.I)
_SPECIFIC = re.compile(r"(chine|chinois|chinoise|china|chinese|droit du travail|code du travail|"
                       r"\brgpd\b|\bgdpr\b|\bloi\b|reglementation|réglementation|"
                       r"etats-unis|états-unis|\busa\b)", re.I)
_PROPER = re.compile(r"(?<![\w'’-])([A-Z][a-zA-Z0-9À-ÿ'’&.-]+(?:\s+[A-Z][a-zA-Z0-9À-ÿ'’&.-]+)*)")
_FIGURE = re.compile(r"\d[\d\s.,]*\s?(%|€|\$|k€|m€|euros?|dollars?)", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_LIST_MARKER = re.compile(r"\d{1,2}[.)]")


def _trim(text, max_chars):
    """Coupe a la derniere fin de phrase avant max_chars, sinon au mot, avec « … »."""
    text = str(text or "").strip()
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    end = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if end > max_chars // 2:
        return window[:end + 1]
    return window.rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def allowed_text(conn, payload_json):
    """Tout ce que le Vault a fourni (question, preuves, contexte, profils de projet)."""
    parts = [payload_json]
    try:
        import mentor_project_profiles as profiles

        for profile in profiles.list_profiles(conn):
            if _fold(profile["project_name"]) in _fold(payload_json):
                parts.append(json.dumps(profile, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001 - profils absents : preuves seules
        pass
    return _fold(" ".join(parts))


# Sigles et termes generiques qui ne constituent pas un fait specifique.
_GENERIC = {"ia", "ai", "cv", "ats", "rh", "hr", "llm", "llms", "pdf", "saas", "b2b", "b2c", "kpi",
            "roi", "mvp", "api", "crm", "seo", "faq", "poc", "vault", "rosy", "ok"}


def filter_unsupported(text, allowed):
    """
    Retire les phrases qui introduisent un nom propre, un pays, une loi ou un chiffre
    absents des preuves fournies, et le discours de role generique. Retourne
    (texte filtre, phrases retirees).
    """
    kept, removed = [], []
    # « 2. Faire X. 3. Faire Y. » : le numero reste attache a sa phrase (sinon une
    # phrase retiree laisse un « 3. » orphelin en fin de reponse).
    sentences = []
    for piece in _SENTENCE.split(str(text or "").strip()):
        if sentences and _LIST_MARKER.fullmatch(sentences[-1]):
            sentences[-1] += " " + piece
        else:
            sentences.append(piece)
    if sentences and _LIST_MARKER.fullmatch(sentences[-1]):
        sentences.pop()
    for sentence in sentences:
        if not sentence:
            continue
        reason = None
        if _ROLE_TALK.search(sentence.strip()):
            reason = "role generique"
        for match in _SPECIFIC.finditer(sentence):
            if _fold(match.group(0)) not in allowed:
                reason = f"non etaye : {match.group(0)}"
        for index, match in enumerate(_PROPER.finditer(sentence)):
            phrase = match.group(1).strip(".-")
            words = phrase.split()
            if match.start() == 0 and len(words) == 1:
                continue  # majuscule de debut de phrase
            if len(phrase) > 1 and _fold(phrase) not in allowed and not all(
                    _fold(w) in allowed or _fold(w) in _GENERIC for w in words):
                reason = f"non etaye : {phrase}"
        for match in _FIGURE.finditer(sentence):
            if _fold(match.group(0).strip()) not in allowed:
                reason = f"chiffre non etaye : {match.group(0).strip()}"
        if reason:
            removed.append({"sentence": sentence[:200], "reason": reason})
        else:
            kept.append(sentence)
    return " ".join(kept).strip(), removed
