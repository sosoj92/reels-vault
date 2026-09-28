#!/usr/bin/env python3
"""
vault_ask.py - Ask Life Intelligence, RETRIEVAL-FIRST (V1).

    question -> recherche directe dans les sources sauvegardees
             -> reponse deterministe citee (titres + URLs), immediate
             -> enrichissement Qwen local OPTIONNEL, sous echeance stricte

Garanties V1 :
- une reponse utile ne depend jamais d'un appel LLM : si Ollama est lent,
  absent ou en erreur, la reponse deterministe est renvoyee telle quelle ;
- rien n'est invente : chaque element cite vient d'une ligne du Vault ;
- aucun provider cloud n'est requis (voir ``SYNTHESIZERS`` pour en brancher
  un plus tard sans toucher au pipeline) ;
- lecture seule : aucune ecriture en base, aucune action externe.

Utilise par vault_voice_bridge.py --ask (appele par l'outil Jarvis
life_intelligence.py).
"""

import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

import vault_intelligence_context as context_layer

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "vault.db"

OLLAMA_BASE = os.environ.get("VAULT_OLLAMA_URL", "http://localhost:11434").rstrip("/")
# qwen3:4b est trop lent sur cette machine : jamais dans un appel interactif.
LOCAL_MODEL = os.environ.get("VAULT_ASK_MODEL", "qwen3:1.7b")
# Meme num_ctx que mentor_worker / mentor_agency (4096 = defaut Ollama) :
# changer num_ctx force Ollama a recharger le modele (5 a 9 s ici, CPU).
# Le prompt Ask fait ~400 tokens, 4096 suffit largement.
LOCAL_NUM_CTX = 4096
DEFAULT_LLM_TIMEOUT = float(os.environ.get("VAULT_ASK_LLM_TIMEOUT", "25"))

MAX_SOURCES = 5
MIN_RELATIVE_SCORE = 0.35

# Domaines ou la prudence epistemique est obligatoire.
_SENSITIVE_ROUTES = {"research_evidence", "health", "finance", "legal"}
_HEALTH_WORDS = {
    "tdah", "adhd", "sante", "health", "medicament", "traitement", "maladie",
    "therapie", "psychiatre", "medecin", "symptome", "symptomes", "etude",
    "guanfacine", "clonidine", "sommeil", "anxiete", "depression",
}
_HEALTH_CATEGORIES = {"sante bien etre"}

_OUTCOME_LABELS = {
    "opportunity": "opportunité",
    "opportunity_convergence": "opportunité convergente",
    "convergence": "convergence",
    "novelty": "nouveauté",
    "low_signal": "signal faible",
}


# ---------------------------------------------------------------------------
# Connexion
# ---------------------------------------------------------------------------

def connect_readonly(path=None):
    """Connexion SQLite en lecture seule, lignes sqlite3.Row."""
    db = Path(path or DEFAULT_DB).resolve()
    if not db.exists():
        raise FileNotFoundError(f"Base Vault introuvable : {db}")
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Mise en forme deterministe
# ---------------------------------------------------------------------------

def _clean_title(title):
    text = re.sub(r"#\S+", "", str(title or ""))
    text = " ".join(text.split()).strip(" .-–—")
    return text or "Source sans titre"


def _clean_text(text):
    text = re.sub(r"#\S+", "", str(text or ""))
    return " ".join(text.split()).strip()


def _cut(text, max_chars):
    text = " ".join(str(text or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:.") + "…"


def _best_excerpt(source):
    """Description utile, sinon extrait du transcript autour des termes."""
    title = _clean_title(source.get("title"))
    snippet = _clean_text(source.get("snippet"))
    excerpt = _clean_text(source.get("transcript_excerpt"))
    folded_snippet = context_layer._fold(snippet)
    folded_title = context_layer._fold(title)
    snippet_is_title = (
        not snippet
        or folded_snippet == folded_title
        or folded_snippet in folded_title
        or folded_title in folded_snippet and len(snippet) < len(title) + 40
    )
    if (snippet_is_title or len(snippet) < 80) and excerpt:
        return _cut(excerpt.lstrip("."), 260)
    return _cut(snippet, 260)


def _platform_label(platform):
    value = str(platform or "").strip().lower()
    return {
        "tiktok": "TikTok",
        "instagram": "Instagram",
        "youtube": "YouTube",
    }.get(value, platform or "")


def _mentor_status(conn, source_ids):
    """Classification Mentor deja calculee (lecture seule)."""
    if not source_ids:
        return {}
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='intelligence_mentor_processing'"
    ).fetchone()
    if not exists:
        return {}
    placeholders = ",".join("?" for _ in source_ids)
    rows = conn.execute(
        f"SELECT source_id, outcome, relevance_score "
        f"FROM intelligence_mentor_processing WHERE source_id IN ({placeholders})",
        tuple(source_ids),
    ).fetchall()
    return {
        row[0]: {"outcome": row[1], "relevance": row[2]}
        for row in rows
    }


def _topic(question):
    text = " ".join(str(question or "").split()).strip(" ?!.")
    match = re.search(
        r"\b(?:sur|à propos de|a propos de|concernant|au sujet de)\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    topic = match.group(1) if match else text
    topic = re.sub(r"^(les|le|la|l'|l’|des|du|de la|de l'|mes|mon|ma)\s*", "",
                   topic, flags=re.IGNORECASE)
    return topic.strip() or text


def _is_sensitive(question, sources, routes):
    if {r.get("route") for r in routes} & _SENSITIVE_ROUTES:
        return True
    terms = {item["term"] for item in context_layer.query_terms(question)}
    if terms & _HEALTH_WORDS:
        return True
    return any(
        context_layer._fold(s.get("category")) in _HEALTH_CATEGORIES
        for s in sources
    )


def _source_lines(source):
    title = _clean_title(source.get("title"))
    meta = []
    if source.get("author"):
        meta.append("@" + str(source["author"]).lstrip("@"))
    platform = _platform_label(source.get("platform"))
    if platform:
        meta.append(platform)
    head = f"- « {title} »"
    if meta:
        head += f" ({', '.join(meta)})"
    excerpt = _best_excerpt(source)
    if excerpt and context_layer._fold(excerpt) != context_layer._fold(title):
        head += f" : {excerpt}"
    lines = [head]
    if source.get("url"):
        lines.append(f"  {source['url']}")
    return lines


_ASSESSMENT_LABELS = {
    "convergence": "Convergence",
    "contradiction": "Contradiction à vérifier",
    "possible_tension": "Tension possible (non confirmée)",
    "possible_supersession": "Information possiblement remplacée",
    "reactivation": "Ancienne information redevenue pertinente",
}


def render_answer(question, sources, *, mentor=None, sensitive=False,
                  capabilities=None, agents=None, mode="ask", related=None,
                  assessments=None):
    """
    Reponse deterministe : uniquement des faits presents dans le Vault.

    related : sources trouvees uniquement par proximite de sens (V2), toujours
    presentees a part et "a verifier".
    assessments : convergences / contradictions / remplacements traces (V2).
    """
    mentor = mentor or {}
    capabilities = capabilities or {}
    related = related or []
    assessments = assessments or []
    topic = _topic(question)

    if not sources:
        lines = [
            f"Je n'ai trouvé aucune source suffisamment pertinente dans ton "
            f"Vault sur « {topic} ».",
        ]
        if related:
            lines.append(
                "Rapprochement de sens uniquement (aucun mot de ta question n'y "
                "figure, pertinence à vérifier) :"
            )
            for source in related:
                lines.extend(_source_lines(source))
        else:
            lines.append(
                "Je préfère ne rien inventer : essaie un autre mot-clé "
                "(nom d'outil, auteur, thème)."
            )
        if mode == "consult" and agents:
            lines.append(
                "Agents qui auraient été sollicités : " + ", ".join(agents) + "."
            )
        return "\n".join(lines)

    lines = ["Tu avais notamment sauvegardé :"]
    for source in sources:
        lines.extend(_source_lines(source))
    if related:
        lines.append("Peut-être lié (rapprochement de sens, à vérifier) :")
        for source in related:
            lines.extend(_source_lines(source))

    # "Pourquoi c'est utile" : uniquement des signaux deja calcules.
    reasons = []
    origins = []
    for item in context_layer.query_terms(question):
        if item["term"] == item["origin"] and item["origin"] not in origins:
            origins.append(item["origin"])
    matched_origins = [
        term for term in origins
        if any(term in s.get("matched_tokens", []) for s in sources)
    ]
    if matched_origins:
        # Affiche le mot tel que prononce (avec accents), pas sa forme pliee.
        spoken = {}
        for word in re.findall(r"[\w'’-]+", question):
            for part in re.split(r"['’-]", word):
                spoken.setdefault(context_layer._fold(part), part.lower())
        reasons.append(
            "elles correspondent à ta question sur "
            + ", ".join(f"« {spoken.get(term, term)} »" for term in matched_origins)
        )
    flagged = [
        (_clean_title(s.get("title")), mentor[s["source_id"]])
        for s in sources
        if s["source_id"] in mentor
        and mentor[s["source_id"]].get("outcome") in (
            "opportunity", "opportunity_convergence", "novelty", "convergence"
        )
    ]
    if flagged:
        described = []
        for title, status in flagged[:3]:
            label = _OUTCOME_LABELS.get(status["outcome"], status["outcome"])
            described.append(f"« {_cut(title, 50)} » ({label})")
        reasons.append(
            "le Mentor les avait classées : " + ", ".join(described)
        )
    if reasons:
        lines.append("")
        lines.append("Pourquoi elles peuvent t'être utiles : " + " ; ".join(reasons) + ".")

    if assessments:
        lines.append("")
        lines.append("Ce que les affirmations extraites montrent :")
        for item in assessments[:4]:
            label = _ASSESSMENT_LABELS.get(item["kind"], item["kind"])
            refs = ", ".join(f"source {sid}" for sid in item["source_ids"])
            lines.append(f"- {label} : {item['summary']} ({refs})")

    if sensitive:
        lines.append("")
        lines.append(
            "Prudence : ce sont des contenus de réseaux sociaux sauvegardés, "
            "pas des preuves vérifiées. Aucune de ces affirmations n'a été "
            "vérifiée indépendamment dans le Vault ; à confronter à des sources "
            "fiables ou à un professionnel avant toute décision."
        )
    if (sensitive or mode == "consult") and capabilities.get(
        "contradiction_detection"
    ) != "available":
        lines.append(
            "Note : la détection de contradictions n'est pas encore disponible "
            "(aucune affirmation structurée extraite dans le Vault)."
        )
    if mode == "consult" and agents:
        lines.append("Angles d'analyse retenus : " + ", ".join(agents) + ".")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Enrichissement LLM optionnel (jamais bloquant)
# ---------------------------------------------------------------------------

def _ollama_models(endpoint, timeout=1.5):
    try:
        with urllib.request.urlopen(f"{OLLAMA_BASE}{endpoint}", timeout=timeout) as resp:
            models = json.load(resp).get("models", [])
    except Exception:
        return None
    return {m.get("name") for m in models} | {m.get("model") for m in models}


def _ollama_available(timeout=1.5):
    """Modele PRET : Ollama repond et qwen est deja charge en memoire."""
    loaded = _ollama_models("/api/ps", timeout)
    return bool(loaded) and LOCAL_MODEL in loaded


def _ollama_installed(timeout=1.5):
    installed = _ollama_models("/api/tags", timeout)
    return bool(installed) and LOCAL_MODEL in installed


def _warm_up_async():
    """
    Precharge qwen dans un processus detache (meme num_ctx que tous les
    appelants, donc pas de rechargement ensuite). Ask n'attend jamais ce
    chargement : c'est la question SUIVANTE qui en profite.
    """
    if os.environ.get("VAULT_ASK_WARMUP", "1").strip().lower() in ("0", "off", "false", "non"):
        return False
    import subprocess
    import sys

    payload = json.dumps({
        "model": LOCAL_MODEL,
        "keep_alive": "30m",
        "options": {"num_ctx": LOCAL_NUM_CTX},
    })
    code = (
        "import urllib.request as u\n"
        f"u.urlopen(u.Request({OLLAMA_BASE + '/api/generate'!r}, data={payload!r}.encode(),"
        " headers={'Content-Type': 'application/json'}), timeout=180).read()\n"
    )
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags,
        )
    except OSError:
        return False
    return True


def _run_with_deadline(func, timeout):
    """Execute func dans un thread ; abandonne apres timeout secondes."""
    box = {}

    def target():
        try:
            box["value"] = func()
        except Exception as exc:  # noqa: BLE001 - on remonte le type seulement
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None, "timeout"
    if "error" in box:
        return None, f"error:{type(box['error']).__name__}"
    return box.get("value"), "ok"


def _synthesis_prompt(question, sources, agents):
    blocks = []
    for index, source in enumerate(sources[:4], start=1):
        blocks.append(
            f"[{index}] {_cut(_clean_title(source.get('title')), 90)} — "
            f"{_cut(_best_excerpt(source), 240)}"
        )
    lens = f"Angles : {', '.join(agents)}.\n" if agents else ""
    system = (
        "Tu es Life Intelligence. Tutoie l'utilisateur. Réponds en français, "
        "en 2 ou 3 phrases "
        "maximum, uniquement à partir des SOURCES. N'invente aucun fait, "
        "chiffre ou nom absent des sources. Mentionne les sources par leur "
        "numéro [n]. Si les sources ne répondent pas, dis-le."
    )
    user = f"Question : {question}\n{lens}SOURCES :\n" + "\n".join(blocks)
    return system, user


def _ollama_chat(system, user, num_predict=130, timeout=120):
    payload = {
        "model": LOCAL_MODEL,
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {
            "temperature": 0.1,
            "num_predict": num_predict,
            "num_ctx": LOCAL_NUM_CTX,
        },
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        f"{OLLAMA_BASE}/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    # Le socket se ferme a l'echeance : Ollama voit la deconnexion et
    # abandonne la generation au lieu de saturer le CPU pour rien.
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = json.load(resp)
    text = str(raw.get("message", {}).get("content") or "")
    # Par securite si un modele "thinking" renvoie ses balises.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return " ".join(text.split()).strip()


def _complete_sentences(text):
    """Retire une fin de phrase coupee par la limite de tokens."""
    text = text.strip()
    if not text or text[-1] in ".!?»)]":
        return text
    cut = max(text.rfind(mark) for mark in (". ", "! ", "? ", "] "))
    return text[: cut + 1].strip() if cut > 0 else text


def synthesize_local(question, sources, agents, timeout):
    if not _ollama_available():
        if _ollama_installed():
            # Modele froid : chargement + generation depasseraient l'echeance
            # (mesure : 31 s pour aucune synthese). On repond sans attendre et
            # on prechauffe pour la prochaine question.
            return None, {"status": "skipped_model_cold", "model": LOCAL_MODEL,
                          "warming_up": _warm_up_async()}
        return None, {"status": "unavailable", "model": LOCAL_MODEL}
    system, user = _synthesis_prompt(question, sources, agents)
    started = time.perf_counter()
    text, status = _run_with_deadline(
        lambda: _ollama_chat(system, user, timeout=max(1.0, timeout)), timeout + 1.0)
    info = {
        "status": status,
        "model": LOCAL_MODEL,
        "elapsed_s": round(time.perf_counter() - started, 1),
    }
    if status != "ok" or not text:
        if status == "ok":
            info["status"] = "empty"
        return None, info
    return _cut(_complete_sentences(text), 700), info


def public_only(sources):
    """
    Copies des sources sans note personnelle : si l'extrait vient de la
    description manuelle de l'utilisateur, il est remplace par l'extrait de
    transcript (public) ou retire. Utilise avant tout envoi cloud.
    """
    cleaned = []
    for source in sources:
        item = dict(source)
        if item.get("snippet_field") == "description_manual":
            item["snippet"] = item.get("transcript_excerpt") or ""
            item["snippet_field"] = "transcript" if item["snippet"] else None
        cleaned.append(item)
    return cleaned


def synthesize_cloud(question, sources, agents, timeout):
    """
    Synthese via l'adapter cloud (vault_cloud). Sans cle configuree : renvoie
    immediatement "unavailable", la reponse deterministe reste intacte.
    Seuls des extraits PUBLICS des sources (titre, extrait) sont envoyes.
    """
    import vault_cloud

    provider = vault_cloud.get_provider()
    if provider is None:
        return None, {"status": "unavailable", "provider": None}
    system, user = _synthesis_prompt(question, public_only(sources), agents)
    started = time.perf_counter()
    try:
        response = provider.complete(system, user, max_tokens=1024, timeout=timeout)
    except vault_cloud.CloudError as exc:
        return None, {
            "status": "error",
            "provider": provider.name,
            "reason": vault_cloud.redact(str(exc))[:200],
            "elapsed_s": round(time.perf_counter() - started, 1),
        }
    text = " ".join(str(response.text or "").split())
    info = {
        "status": "ok" if text else "empty",
        "provider": provider.name,
        "model": response.model,
        "label": f"cloud ({provider.name}:{response.model})",
        "elapsed_s": round(time.perf_counter() - started, 1),
    }
    return (_cut(_complete_sentences(text), 700) if text else None), info


# Point d'extension : chaque synthetiseur a la meme signature
# (question, sources, agents, timeout) -> (texte | None, info).
# "local" = Qwen via Ollama ; "cloud" = vault_cloud (Claude aujourd'hui).
SYNTHESIZERS = {
    "local": synthesize_local,
    "cloud": synthesize_cloud,
}


# ---------------------------------------------------------------------------
# API principale
# ---------------------------------------------------------------------------

def _agent_lenses(question, requested_agent=None, max_agents=3):
    try:
        import vault_orchestrator as orchestrator
        from vault_agent_registry import discover_agents, get_agent

        if requested_agent:
            agent = get_agent(requested_agent)
            return [agent.name], [{"route": "explicit_agent"}]
        selection = orchestrator.select_agents(
            question, agents=discover_agents(), max_agents=max_agents,
        )
        return [a.name for a in selection["agents"]], selection["routes"]
    except Exception:
        # Le routage Agency est un enrichissement : son absence ne bloque pas.
        return [], []


def _claim_assessments(conn, source_ids):
    """Convergences / contradictions tracees touchant ces sources (V2)."""
    if not source_ids:
        return []
    try:
        import vault_claim_reasoning as reasoning

        return reasoning.assessments_for_sources(conn, source_ids)
    except Exception:
        # Enrichissement : son absence ne bloque jamais Ask.
        return []


def _synthesizer_order():
    """
    VAULT_ASK_SYNTHESIZER :
    - "local" (defaut) : Qwen local uniquement ;
    - "cloud" : provider cloud uniquement (si une cle est configuree) ;
    - "auto"  : local, puis cloud si le local est absent ou trop lent.
    La reponse deterministe citee ne depend d'aucun de ces choix.
    """
    choice = os.environ.get("VAULT_ASK_SYNTHESIZER", "local").strip().lower()
    if choice == "cloud":
        return ["cloud"]
    if choice == "auto":
        return ["local", "cloud"]
    return ["local"]


def ask(conn, question, *, mode="ask", agent=None, llm="auto",
        llm_timeout=DEFAULT_LLM_TIMEOUT, max_sources=MAX_SOURCES,
        semantic="auto", semantic_backend=None):
    """
    Repond a une question de rappel personnel a partir du Vault.

    mode     : "ask" (rappel) ou "consult" (consultation des agents du Vault).
    llm      : "auto" (synthese si disponible, sous echeance) ou "off".
    semantic : "auto" (recherche hybride si l'index existe) ou "off".
    """
    started = time.perf_counter()
    question = " ".join(str(question or "").split())
    if not question:
        raise ValueError("question is required")

    pack = context_layer.build_context_pack(
        conn, question, direct_source_limit=max(max_sources * 2, 8),
        semantic=semantic, semantic_backend=semantic_backend,
    )
    lexical = [
        s for s in pack["sources"]
        if (s.get("retrieval") or {}).get("match_type") != "semantic"
    ]
    top_lexical = max((s["score"] for s in lexical), default=0.0)
    sources = [
        s for s in lexical if s["score"] >= top_lexical * MIN_RELATIVE_SCORE
    ][:max_sources]
    related = [
        s for s in pack["sources"]
        if (s.get("retrieval") or {}).get("match_type") == "semantic"
    ][:2]
    retrieval_ms = round((time.perf_counter() - started) * 1000)

    agents, routes = _agent_lenses(question, agent)
    sensitive = _is_sensitive(question, sources + related, routes)
    mentor = _mentor_status(conn, [s["source_id"] for s in sources])
    assessments = _claim_assessments(conn, [s["source_id"] for s in sources])

    answer = render_answer(
        question,
        sources,
        mentor=mentor,
        sensitive=sensitive,
        capabilities=pack.get("capabilities"),
        agents=agents,
        mode=mode,
        related=related,
        assessments=assessments,
    )

    synthesis = None
    synthesizer = _synthesizer_order()
    if llm == "off":
        llm_info = {"status": "disabled"}
    elif not sources:
        llm_info = {"status": "skipped_no_sources"}
    elif sensitive:
        # Sante / finance / droit : un petit modele local invente trop
        # facilement (observe : "la guanfacine est une stimulante", faux).
        # La reponse citee + l'avertissement suffisent.
        llm_info = {"status": "skipped_sensitive"}
    else:
        llm_info = {"status": "skipped_no_synthesizer"}
        attempts = []
        for name in synthesizer:
            synthesis, llm_info = SYNTHESIZERS[name](
                question, sources, agents if mode == "consult" else [], llm_timeout,
            )
            attempts.append({"synthesizer": name, **llm_info})
            if synthesis:
                break
        llm_info = {**llm_info, "attempts": attempts}
        if synthesis:
            label = llm_info.get("label") or f"locale ({LOCAL_MODEL})"
            answer += f"\n\nSynthèse {label}, à vérifier : {synthesis}"

    return {
        "question": question,
        "mode": mode,
        "answer": answer,
        "sources": [
            {
                key: source.get(key)
                for key in (
                    "source_id", "content_id", "title", "url", "platform",
                    "author", "category", "snippet", "matched_tokens",
                    "matched_fields", "score", "retrieval",
                )
            }
            for source in sources
        ],
        "related_sources": [
            {
                key: source.get(key)
                for key in (
                    "source_id", "content_id", "title", "url", "platform",
                    "author", "category", "snippet", "retrieval",
                )
            }
            for source in related
        ],
        "retrieval": pack.get("retrieval_info"),
        "assessments": assessments,
        "linked_entities": [e["name"] for e in pack.get("linked_entities", [])],
        "agents": agents,
        "routes": [r.get("route") for r in routes],
        "sensitive": sensitive,
        "capabilities": pack.get("capabilities"),
        "llm": {**llm_info, "used": bool(synthesis)},
        "timing_ms": {
            "retrieval": retrieval_ms,
            "total": round((time.perf_counter() - started) * 1000),
        },
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Ask Life Intelligence (retrieval-first)")
    parser.add_argument("question", nargs="+")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--mode", choices=("ask", "consult"), default="ask")
    parser.add_argument("--agent")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--llm-timeout", type=float, default=DEFAULT_LLM_TIMEOUT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    conn = connect_readonly(args.db)
    try:
        result = ask(
            conn,
            " ".join(args.question),
            mode=args.mode,
            agent=args.agent,
            llm="off" if args.no_llm else "auto",
            llm_timeout=args.llm_timeout,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["answer"])


if __name__ == "__main__":
    main()
