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
from vault_security_gate import UNTRUSTED_RULE, wrap_untrusted

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
MAX_RETRIEVAL_QUERY_CHARS = 500

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
    # Question complete (pas la requete de retrieval compactee) : aucun mot sensible perdu.
    if set(context_layer._fold(question).split()) & _HEALTH_WORDS:
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
                  assessments=None, term_question=None):
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
    for item in context_layer.query_terms(term_question or question):
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
            f"[{index}] " + wrap_untrusted(
                f"{_cut(_clean_title(source.get('title')), 90)} — "
                f"{_cut(_best_excerpt(source), 240)}", ref=index)
        )
    lens = f"Angles : {', '.join(agents)}.\n" if agents else ""
    system = (
        "Tu es Life Intelligence. Tutoie l'utilisateur. Réponds en français, "
        "en 2 ou 3 phrases "
        "maximum, uniquement à partir des SOURCES. N'invente aucun fait, "
        "chiffre ou nom absent des sources. Mentionne les sources par leur "
        "numéro [n]. Si les sources ne répondent pas, dis-le. " + UNTRUSTED_RULE
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
    # « … 2. Faire X. 3. » : un numero de liste orphelin n'est pas une fin de phrase.
    text = re.sub(r"(?:^|(?<=[.!?:;]))\s*\d{1,2}[.)]$", "", text).rstrip()
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


def synthesize_router(question, sources, agents, timeout):
    """
    Synthese via le routeur LLM central (llm_router) : il choisit Qwen local,
    Mistral ou rien selon la politique, la charge du PC, le budget et la
    confidentialite. Seuls des extraits publics des sources sont transmis.
    """
    import llm_router

    router = llm_router.shared()
    system, user = _synthesis_prompt(question, public_only(sources), agents)
    started = time.perf_counter()
    text = router.complete_text(system, user, max_tokens=180, task="light",
                                purpose="ask_synthesis")
    last = router.last or {}
    info = {"status": "ok" if text else "unavailable", "provider": last.get("provider"),
            "model": last.get("model"), "elapsed_s": round(time.perf_counter() - started, 1)}
    if text:
        # Le moteur reel reste dans le journal technique (llm_usage.db), pas dans la reponse.
        info["plain"] = True
        return _cut(_complete_sentences(" ".join(text.split())), 700), info
    info["reason"] = last.get("reason")
    return None, info


# Point d'extension : chaque synthetiseur a la meme signature
# (question, sources, agents, timeout) -> (texte | None, info).
# "local" = Qwen via Ollama ; "cloud" = vault_cloud (Claude aujourd'hui).
SYNTHESIZERS = {
    "router": synthesize_router,
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
    choice = os.environ.get("VAULT_ASK_SYNTHESIZER", "router").strip().lower()
    if choice == "router":
        return ["router"]
    if choice == "cloud":
        return ["cloud"]
    if choice == "auto":
        return ["local", "cloud"]
    return ["local"]


_GREETING_RE = re.compile(
    r"^\s*(bonjour|bonsoir|salut|hello|hey|coucou|yo)\b[\s,!.:;-]*", re.IGNORECASE)
_POLITE_RE = re.compile(
    r"\b(s'il te pla[iî]t|s'il vous pla[iî]t|stp|svp|merci)\b[\s,!.]*", re.IGNORECASE)
_DESCRIBE_RE = re.compile(
    r"^(que sais[- ]tu|qu'est[- ]ce que tu sais|qu est ce que tu sais|que connais[- ]tu|"
    r"parle[- ]moi|dis[- ]moi (tout )?(ce que tu sais )?|fais[- ]moi le point|fais le point|"
    r"o[uù] en est|o[uù] en sont|c'est quoi|qu'est[- ]ce que|infos?|r[ée]sum[ée]|"
    r"point|rappelle[- ]moi)\b[\s:,]*(de |du |des |d'|sur |le |la |les |l')*",
    re.IGNORECASE)


def clean_question(question):
    """Retire salutations et formules de politesse : on ne cherche jamais la phrase brute."""
    text = " ".join(str(question or "").replace("\u2019", "'").split())
    previous = None
    while previous != text:
        previous = text
        text = _GREETING_RE.sub("", text).strip()
    text = _POLITE_RE.sub("", text).strip(" ,")
    return text or " ".join(str(question or "").split())


# Mots de consigne d'un long prompt : ils decrivent la forme de la reponse
# attendue (« analyse », « classe par priorite »...), pas le sujet a retrouver.
# Utilises uniquement pour compacter les longs messages (les questions courtes
# gardent le comportement historique). Aucun mot de sante ici : la sensibilite
# est de toute facon evaluee sur la question complete.
_PROMPT_WORDS = {
    "analyse", "analyser", "analyses", "audit", "auditer", "actuel", "actuelle",
    "actuels", "actuelles", "concret", "concrete", "concretes", "concrets",
    "maximum", "minimum", "degrader", "qualite", "notamment", "regardes",
    "utiliser", "utilise", "utilises", "non", "oui", "peuvent", "pouvoir", "etre",
    "faits", "faite", "faites", "classique", "classiques", "envoye", "envoyes",
    "envoyer", "eviter", "inutile", "inutilement", "meme", "memes", "selon", "peu",
    "autre", "autres", "potentiel", "potentielle", "priorite", "priorites",
    "prioriser", "priorise", "prends", "prendre", "compte", "privilegie",
    "privilegier", "simple", "simples", "avant", "apres", "nouveau", "nouveaux",
    "nouvelle", "nouvelles", "ajout", "brique", "briques", "recommandation",
    "recommandations", "recommande", "classe", "classer", "classement", "gain",
    "gains", "difficulte", "difficultes", "implementation", "implementer",
    "detail", "detaille", "detaillee", "detailles", "propose", "proposer",
    "proposition", "propositions", "montre", "moi", "quand", "lesquels",
    "system", "systeme", "systemes", "lis", "pertinent", "pertinente",
    "pertinents", "pertinentes", "possible", "possibles", "option", "options",
    "alternative", "alternatives", "contrainte", "contraintes", "fin",
    "concentre", "concentrer", "puissant", "puissants", "puissante", "couteux", "cher",
    "tache", "taches", "taille", "renvoyer", "facile", "faciles", "rapide",
    "besoin", "souhaite", "ensuite", "enfin", "egalement", "ainsi", "afin",
    "donc", "car", "mais", "etape", "etapes", "ordre", "point", "points",
    "exemple", "exemples", "cible", "ciblee", "maniere", "facon", "vraie",
}
MAX_RETRIEVAL_TERMS = 16


def compact_retrieval_query(question, max_chars=MAX_RETRIEVAL_QUERY_CHARS,
                            max_terms=MAX_RETRIEVAL_TERMS):
    """
    Reduit un long message utilisateur en requete de retrieval bornee.

    Le message original reste intact pour le raisonnement. Seule la recherche
    dans le Vault recoit cette version compacte : mots-sujets uniques (sans mots
    de rappel ni de consigne), plafonnes en nombre ; les trois quarts viennent du
    debut, le reste de la fin pour ne pas perdre un sujet cite tard. Trop de
    termes diluent la couverture et font remonter des sources hors sujet.
    """
    cleaned = clean_question(question)
    if len(cleaned) <= max_chars:
        return cleaned

    stopwords = context_layer._STOPWORDS | context_layer._RECALL_STOPWORDS | _PROMPT_WORDS
    stems = set()
    terms = []
    for token in context_layer._fold(cleaned).split():
        if len(token) < 2 or token in stopwords or token in context_layer._GENERIC_TOPIC_WORDS:
            continue
        if token.isdigit() and len(token) < 3:
            continue
        stem = context_layer._stem(token)
        if stem in stems:
            continue
        stems.add(stem)
        terms.append(token)
    if not terms:
        return cleaned[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:.") or cleaned[:max_chars]

    if len(terms) > max_terms:
        tail = max_terms // 4
        terms = terms[:max_terms - tail] + terms[-tail:]
    while len(" ".join(terms)) > max_chars and len(terms) > 1:
        terms.pop(len(terms) // 2)  # retire au milieu : garde le debut et la fin
    return " ".join(terms)[:max_chars]


def project_intent(conn, question):
    """
    Intention describe_project : « que sais-tu de Nexdoor Talent ? », « ou en est X ? »,
    ou le nom seul d'un projet connu. Retourne l'id du projet, sinon None.
    """
    cleaned = clean_question(question).strip(" ?!.")
    stripped = _DESCRIBE_RE.sub("", cleaned).strip(" ?!.")
    describing = stripped != cleaned
    try:
        import mentor_council

        projects = mentor_council.projects_in_text(conn, stripped or cleaned)
    except Exception:  # noqa: BLE001 - profils absents : pas de route projet
        return None
    if len(projects) != 1:
        return None
    if describing:
        return projects[0]
    # Nom seul (ou presque) : « Nexdoor Talent ? »
    import mentor_project_links as links

    names = links.load_projects(conn)[projects[0]]["profile"]["names"]
    from vault_db import fold

    return projects[0] if fold(stripped) in names else None


def _rows(conn, sql, params=()):
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def project_view(conn, project_id):
    """Ce que le Vault sait reellement d'un projet (aucune generation)."""
    entity = _rows(conn, "SELECT id, entity_type, name, attention_status FROM "
                         "intelligence_entities WHERE id = ?", (project_id,))
    if not entity:
        return None
    view = {"entity": entity[0]}
    binding = _rows(conn, "SELECT canonical_path, parent_entity_id, notion_url, drive_url "
                          "FROM intelligence_entity_bindings WHERE entity_id = ?", (project_id,))
    view["binding"] = binding[0] if binding else None
    try:
        import mentor_project_profiles as profiles

        view["profile"] = profiles.get_profile(conn, project_id)
    except Exception:  # noqa: BLE001
        view["profile"] = None
    view["sources"] = _rows(conn, """
        SELECT s.id AS source_id, s.title, s.source_url AS url, s.author, es.role, es.notes,
               c.id AS content_id, COALESCE(c.description_manual, c.description_original,
               c.description_ai, '') AS snippet
          FROM intelligence_entity_sources es
          JOIN intelligence_sources s ON s.id = es.source_id
          LEFT JOIN contents c ON c.id = s.content_id
         WHERE es.entity_id = ? AND es.role <> 'primary'
         ORDER BY s.id""", (project_id,))
    source_ids = [s["source_id"] for s in view["sources"]]
    marks = ",".join("?" for _ in source_ids) or "NULL"
    view["insights"] = _rows(conn, f"SELECT id, source_id, insight_type, observation, "
                                   f"relevance_score, status FROM "
                                   f"intelligence_mentor_insights_local WHERE source_id IN "
                                   f"({marks}) ORDER BY relevance_score DESC", source_ids)
    view["suggestions"] = _rows(conn, "SELECT suggestion_id, title, summary, status FROM "
                                      "mentor_suggestions WHERE project_id = ? "
                                      "ORDER BY created_at", (project_id,))
    content_ids = [s["content_id"] for s in view["sources"] if s.get("content_id")]
    cmarks = ",".join("?" for _ in content_ids) or "NULL"
    view["tasks"] = _rows(conn, f"SELECT id, title, status, due_date FROM actions WHERE "
                                f"content_id IN ({cmarks}) AND status NOT IN "
                                f"('dismissed', 'archived') ORDER BY id", content_ids)
    view["pending_links"] = _rows(conn, "SELECT proposal_id, source_id, confidence_level "
                                        "FROM mentor_project_link_proposals WHERE entity_id = ? "
                                        "AND status = 'proposed' AND confidence_level IN "
                                        "('high', 'medium')", (project_id,))
    return view


def render_project_view(view):
    entity = view["entity"]
    profile = view.get("profile") or {}
    binding = view.get("binding") or {}
    lines = [f"📁 {entity['name']} ({entity['entity_type']})"]
    if binding.get("canonical_path") and binding["canonical_path"] != entity["name"]:
        lines.append(f"Place : {binding['canonical_path']}")
    if profile.get("description"):
        lines.append(f"Description (profil) : {profile['description']}")
    topics = [t["topic"] for t in profile.get("core_topics") or []][:8]
    if topics:
        lines.append("Sujets suivis : " + ", ".join(topics))
    if profile and profile.get("profile_status") != "complete":
        lines.append("Profil incomplet.")
    lines.append("")
    if view["sources"]:
        lines.append(f"Sources rattachees ({len(view['sources'])}) :")
        for source in view["sources"][:6]:
            how = "validee par toi" if source["role"] == "relevant_to" else "mention"
            lines.append(f"• {_clean_title(source['title'])} (source {source['source_id']}, {how})")
    else:
        lines.append("Aucune source rattachee a ce projet pour l'instant.")
    for insight in view["insights"][:3]:
        lines.append(f"• Insight {insight['insight_type']} ({insight['relevance_score']:.2f}) : "
                     f"{_cut(_clean_text(insight['observation']), 160)}")
    if view["suggestions"]:
        lines.append("")
        lines.append("Suggestions du Mentor :")
        for suggestion in view["suggestions"]:
            lines.append(f"• {suggestion['suggestion_id']} — {suggestion['title']} "
                         f"({suggestion['status']})")
    if view["tasks"]:
        lines.append("")
        lines.append("Taches liees :")
        for task in view["tasks"]:
            lines.append(f"• #{task['id']} {task['title']} ({task['status']})")
    if view["pending_links"]:
        lines.append("")
        lines.append(f"{len(view['pending_links'])} rattachement(s) propose(s) en attente "
                     "de ta validation.")
    lines.append("")
    lines.append("Uniquement ce qui est enregistre dans ton Vault.")
    return "\n".join(lines)


def describe_project(conn, question, project_id):
    started = time.perf_counter()
    view = project_view(conn, project_id)
    if view is None:
        return None
    sources = [{"source_id": s["source_id"], "content_id": s.get("content_id"),
                "title": s["title"], "url": s["url"], "author": s.get("author"),
                "snippet": _cut(_clean_text(s.get("snippet")), 300)} for s in view["sources"]]
    return {
        "question": question, "mode": "project", "route": "project_view",
        "project_id": project_id, "answer": render_project_view(view),
        "sources": sources, "related_sources": [], "retrieval": {"route": "project_view"},
        "assessments": [], "linked_entities": [view["entity"]["name"]], "agents": [],
        "routes": ["project_view"], "sensitive": False, "capabilities": None,
        "llm": {"status": "not_needed", "used": False},
        "timing_ms": {"retrieval": 0,
                      "total": round((time.perf_counter() - started) * 1000)},
    }


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
    retrieval_question = compact_retrieval_query(question)

    # Question sur un projet connu : vue projet construite depuis le Vault.
    project_id = project_intent(conn, question)
    if project_id is not None:
        described = describe_project(conn, question, project_id)
        if described is not None:
            return described

    pack = context_layer.build_context_pack(
        conn, retrieval_question, direct_source_limit=max(max_sources * 2, 8),
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
        term_question=retrieval_question,
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
        if synthesis and llm_info.get("plain"):
            answer += f"\n\nSynthèse : {synthesis}"
        elif synthesis:
            label = llm_info.get("label") or f"locale ({LOCAL_MODEL})"
            answer += f"\n\nSynthèse {label}, à vérifier : {synthesis}"

    return {
        "question": question,
        "retrieval_query": retrieval_question,
        "mode": mode,
        "answer": answer,
        "sources": [
            {
                key: source.get(key)
                for key in (
                    "source_id", "content_id", "title", "url", "platform",
                    "author", "category", "snippet", "snippet_field",
                    "transcript_excerpt", "matched_tokens", "matched_fields",
                    "score", "retrieval",
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


# ---------------------------------------------------------------------------
# Consultation : demande d'analyse longue (Telegram)
#
# RETRIEVAL et RAISONNEMENT sont separes : le Vault est interroge avec une
# requete compacte (ask), le LLM recoit la question COMPLETE, les sources
# retrouvees comme simples preuves d'appui et, si la question porte sur le LIS
# lui-meme, un resume de son architecture et de sa configuration LLM reelle.
# ---------------------------------------------------------------------------

CONSULT_MIN_CHARS = 200
CONSULT_MAX_SOURCES = 4
CONSULT_MAX_TOKENS = 4000
_TRUNCATED = {"length", "max_tokens"}
_CONSULT_RE = re.compile(
    r"(?<![a-z])(analyse|analyser|audite|auditer|compare|comparer|evalue|evaluer|"
    r"recommandations?|priorise|prioriser|classe les|plan d action|quelles solutions|"
    r"optimiser|ameliorer)(?![a-z])")
_LIS_RE = re.compile(
    r"(?<![a-z0-9])(life intelligence|lis|llm router|openrouter|qwen|gemini|ollama|"
    r"mon systeme|ce systeme)(?![a-z0-9])")

# Architecture du LIS (resume statique, sans code ni secret). La configuration LLM
# effective est ajoutee a part par llm_router.describe().
LIS_COMPONENTS = {
    "vault.db": "SQLite, source de verite (contenus, sources, entites, claims, "
                "relations, actions, suggestions du Mentor).",
    "vault_ask": "retrieval-first : recherche lexicale ponderee (idf, champs, synonymes "
                 "FR/EN) + semantique locale optionnelle (embeddings e5) ; reponse "
                 "deterministe citee sans LLM ; synthese courte optionnelle (tache light, "
                 "180 tokens) ; demandes longues : requete de retrieval compactee puis "
                 "consultation (tache complex).",
    "vault_intelligence_context": "paquet de contexte borne construit sans LLM.",
    "llm_router": "point d'appel LLM unique : taches deterministic / light / standard / "
                  "complex ; sensibilite (secret jamais envoye, sensitive local uniquement, "
                  "personal local sauf option) ; budgets, coupe-circuit, journal "
                  "logs/llm_usage.db (tokens, cout). Aucun cache de reponses LLM.",
    "mentor": "mentor_proactive (candidats deterministes) -> mentor_worker (tri, tache "
              "light, 120 tokens) -> mentor_council (hypothese + avis d'agents en JSON, "
              "tache standard, 220 a 520 tokens) -> suggestions a valider.",
    "agents": "playbooks Agency Agents (markdown) choisis par regles de mots-cles "
              "(vault_orchestrator, 3 max, sans LLM) ; en Telegram, avis via une tache "
              "standard (380 tokens).",
    "ingestion": "vault_ingestion : classification des notes / URL / fichiers (tache light, "
                 "JSON, 260 tokens) ; secrets rejetes avant tout LLM.",
    "telegram": "interface (polling, whitelist, vocal transcrit localement) ; aucune "
                "action externe sans clic.",
    "notion": "cockpit : projection des suggestions et taches, jamais source de verite.",
}


def is_consultation(question):
    """Longue demande d'analyse (et non simple rappel) : raisonnement complet requis."""
    text = " ".join(str(question or "").split())
    if len(text) > MAX_RETRIEVAL_QUERY_CHARS:
        return True
    return len(text) >= CONSULT_MIN_CHARS and bool(
        _CONSULT_RE.search(context_layer._fold(text)))


def about_lis(question):
    return bool(_LIS_RE.search(context_layer._fold(question)))


def lis_context(router=None):
    """Architecture + configuration LLM reelle (chaines, modeles, fournisseurs configures)."""
    context = {"composants": LIS_COMPONENTS}
    if router is not None and hasattr(router, "describe"):
        try:
            context["configuration_llm"] = router.describe()
        except Exception:  # noqa: BLE001 - contexte facultatif
            pass
    return context


def _cloud_safe(sources):
    """Titre + extrait public seulement ; sans provenance connue, aucun extrait."""
    safe = []
    for source in public_only(sources):
        snippet = source.get("snippet") if source.get("snippet_field") else ""
        safe.append({"title": _cut(_clean_title(source.get("title")), 90),
                     "extrait": _cut(_clean_text(snippet), 240)})
    return safe


def _consult_prompt(question, sources, lis=None, agents=(), project=None):
    system = (
        "Tu es Life Intelligence, le conseiller personnel de Rosy. Francais, tutoiement. "
        "Reponds directement et completement a la DEMANDE, en respectant la structure "
        "qu'elle demande (classements, priorites...), sans preambule. Format COMPACT : "
        "pour chaque point, 1 a 3 lignes (quoi, pourquoi, comment) ; pas de rubriques "
        "repetees ; termine toujours ta reponse, meme si tu dois etre plus bref. "
        "SOURCES_VAULT = contenus sauvegardes par Rosy : ce sont des elements d'appui, pas "
        "la reponse ; cite-les [n] seulement s'ils servent vraiment, ignore ceux hors sujet. "
        "N'attribue aux sources aucun fait qu'elles ne contiennent pas. "
        "Aucune action externe : proposer, jamais executer. " + UNTRUSTED_RULE
    )
    if lis:
        system += (
            " ARCHITECTURE_LIS decrit le systeme reel : appuie ton analyse dessus, "
            "ne suppose aucun composant absent, et distingue ce qui existe deja de ce "
            "que tu recommandes d'ajouter.")
    parts = [f"DEMANDE :\n{question}"]
    if lis:
        parts.append("ARCHITECTURE_LIS :\n" + json.dumps(lis, ensure_ascii=False))
    if project:
        parts.append("CONTEXTE_PROJET (profil enregistre par Rosy) :\n"
                     + json.dumps(project, ensure_ascii=False, default=str))
    if agents:
        parts.append("PLAYBOOKS D'EXPERTS DEMANDES (applique chacun ; un avis par expert "
                     "puis une synthese) :\n" + "\n\n".join(
                         f"### {a['name']} — {_cut(a.get('role') or '', 200)}"
                         + (f"\n{a['playbook'][:1500]}" if a.get("playbook") else "")
                         for a in agents))
    if sources:
        parts.append("SOURCES_VAULT (UNTRUSTED SOURCE CONTENT) :\n" + "\n".join(
            f"[{i}] " + wrap_untrusted(
                s["title"] + (f" — {s['extrait']}" if s["extrait"] else ""), ref=i)
            for i, s in enumerate(sources, start=1)))
    return system, "\n\n".join(parts)


def consult(question, *, evidence, router=None, agents=(), project_context=None):
    """
    Repond a une demande d'analyse longue.

    evidence : resultat de ask() (retrieval compact deja fait, sans LLM).
    router   : llm_router (tache complex : Gemini puis OpenRouter selon la
               configuration). Sans LLM, la reponse deterministe du Vault est
               renvoyee avec la raison : jamais bloquant.
    agents   : experts explicitement demandes ({name, role, playbook} issus de
               vault_orchestrator.build_consultation_packet), appliques dans le
               MEME appel (pas d'appel supplementaire).
    project_context : profil compact des projets cites (telegram_gateway).
    """
    question = " ".join(str(question or "").split())
    sources = list(evidence.get("sources") or [])[:CONSULT_MAX_SOURCES]
    lis = lis_context(router) if about_lis(question) else None
    system, user = _consult_prompt(question, _cloud_safe(sources), lis, agents,
                                   project_context)
    text, info = None, {"status": "unavailable", "reason": "aucun routeur LLM"}
    if router is not None and hasattr(router, "complete_text"):
        try:
            text = router.complete_text(system, user, max_tokens=CONSULT_MAX_TOKENS,
                                        task="complex", purpose="consultation")
        except Exception as exc:  # noqa: BLE001 - un LLM en panne ne bloque jamais
            info = {"status": "error", "reason": type(exc).__name__}
        else:
            last = getattr(router, "last", None) or {}
            info = {"status": "ok" if text else "unavailable",
                    "provider": last.get("provider"), "model": last.get("model"),
                    "reason": None if text else last.get("reason"),
                    "finish_reason": last.get("finish_reason"),
                    "input_tokens": last.get("input_tokens"),
                    "output_tokens": last.get("output_tokens"),
                    "cost_usd": last.get("cost_usd"),
                    "usage_estimated": last.get("usage_estimated")}
    text = str(text or "").strip()
    truncated = bool(text) and str(info.get("finish_reason") or "").lower() in _TRUNCATED
    if truncated:
        # Limite de sortie du modele atteinte : on retire la ligne inachevee et on le DIT.
        text = text.rsplit("\n", 1)[0].rstrip() if "\n" in text else text
        text += ("\n\n⚠️ Réponse interrompue par la limite de longueur du modèle : "
                 "la suite n'a pas été générée ; pose une question plus ciblée sur la partie "
                 "manquante.")
    info["truncated"] = truncated
    if text:
        answer = text
        cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", text)
                        if 1 <= int(n) <= len(sources)})
        if cited:
            answer += "\n\n📚 Sources du Vault citées :"
            for n in cited:
                source = sources[n - 1]
                answer += f"\n[{n}] {_clean_title(source.get('title'))}"
                if source.get("url"):
                    answer += f" — {source['url']}"
        used = [sources[n - 1] for n in cited]
    else:
        answer = ("⚠️ Analyse complète indisponible pour l'instant (aucun moteur LLM adapté "
                  "disponible). Voici ce que ton Vault contient de proche :\n\n"
                  + evidence.get("answer", ""))
        used = sources
    return {"question": question, "mode": "consultation", "answer": answer,
            "sources": used, "evidence_sources": sources, "lis_context": bool(lis),
            "retrieval_query": evidence.get("retrieval_query"), "llm": info}


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
