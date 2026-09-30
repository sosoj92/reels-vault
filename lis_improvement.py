#!/usr/bin/env python3
"""
lis_improvement.py - Boucle d'AUTO-AMELIORATION SUPERVISEE du LIS (detection seulement).

    CAPTURE -> VAULT -> « est-ce que cela peut ameliorer le LIS ? » (ICI, deterministe)
    -> comparaison avec le LIS actuel -> SECURITY GATE -> amelioration candidate
    -> VALIDATION HUMAINE -> test isole -> implementation eventuelle -> mesure -> Vault

Ce module ne modifie rien : il reconnait qu'une source parle d'un domaine ou le
LIS pourrait progresser (tokens, routage, RAG, agents, securite...) et produit un
`lis_improvement_candidate` toujours `review_required=True`. Le Mentor Worker le
range dans l'insight existant (evidence_json) : aucune nouvelle table.

Le contenu de la source est une DONNEE non fiable : une instruction qu'elle contient
est signalee (scan d'injection), jamais suivie. Un repo, skill, MCP ou script cite
passe obligatoirement par vault_security_gate avant toute recommandation.

Usage (lecture seule) :
    python lis_improvement.py --scan [--limit 20]
"""

import json
import re
import sys
from pathlib import Path

import vault_security_gate as gate

ROOT = Path(__file__).resolve().parent

# Le contenu doit parler d'IA / LLM : « pipeline commercial » ou « securite
# du batiment » ne concernent pas l'architecture du LIS.
_AI_CONTEXT = re.compile(
    r"(?<![a-z0-9])(ia|ai|llm|llms|gpt|chatgpt|claude|gemini|qwen|ollama|openrouter|"
    r"mistral|agent|agents|agentique|agentic|prompt|prompts|token|tokens|modele|modeles|"
    r"model|models|embedding|embeddings|rag|mcp|codex|copilot|cursor)(?![a-z0-9])")

# domaine -> (mots-cles, termes forts suffisants seuls, composant LIS, etat actuel, gain vise)
AREAS = {
    "token_economy": (
        {"tokens", "token", "consommation", "economiser", "economise", "caching", "cache",
         "cheaper", "context"},
        {"rtk", "prompt caching", "token usage"},
        "llm_router",
        "llm_router journalise tokens et couts (logs/llm_usage.db) avec budgets par "
        "fournisseur ; aucun cache de reponses LLM ; la reponse du Vault reste sans LLM.",
        "moins de tokens / de cout a qualite egale"),
    "model_routing": (
        {"routing", "router", "routeur", "routage", "fallback", "openrouter", "ollama",
         "local", "orchestrator", "orchestrateur", "flash", "subagents"},
        {"model routing", "llm router", "modeles moins chers", "cheaper models"},
        "llm_router",
        "chaines light / standard / complex (Qwen local, Gemini, OpenRouter), "
        "coupe-circuit et budgets ; Gemini non configure actuellement.",
        "meilleur choix du moteur selon la difficulte"),
    "context_compression": (
        {"contexte", "context", "compression", "compresser", "resume", "resumer",
         "summarization", "summary", "window", "rot"},
        {"context rot", "context window", "context compression", "context engineering"},
        "vault_ask / vault_orchestrator",
        "requete de retrieval compactee, playbooks compactes (_compact_playbook) ; pas de "
        "resume systematique des contextes envoyes.",
        "contextes plus courts et plus pertinents"),
    "retrieval_rag": (
        {"retrieval", "embeddings", "embedding", "vector", "vectorielle", "rerank",
         "reranker", "chunking", "chunks", "semantic", "semantique", "index"},
        {"rag", "vector database", "base vectorielle", "hybrid search"},
        "vault_ask / vault_embeddings",
        "recherche lexicale ponderee + embeddings e5 locaux optionnels ; pas de reranker.",
        "sources retrouvees plus pertinentes"),
    "agents_skills": (
        {"agent", "agents", "skill", "skills", "subagent", "playbook", "playbooks",
         "orchestration", "agentique"},
        {"agentic os", "claude os", "skill md", "agents md", "mcp", "sub agents",
         "sous agents", "multi agent"},
        "vault_agent_registry / vault_orchestrator",
        "232 playbooks Agency Agents (registre unique), routage deterministe, 3 agents max, "
        "aucun agent autonome.",
        "meilleures methodes de raisonnement ou d'outillage"),
    "memory": (
        {"memory", "knowledge", "graph", "graphe", "brain", "recall", "embeddings"},
        {"second brain", "knowledge graph", "long term memory", "memoire long terme"},
        "vault.db",
        "vault.db canonique (sources, entites, claims, relations), projection Notion.",
        "memoire plus exploitable"),
    "observability": (
        {"monitoring", "observability", "observabilite", "logs", "tracing", "traces",
         "metrics", "metriques", "evals", "eval", "dashboard"},
        {"llm observability", "langfuse", "langsmith", "opentelemetry"},
        "llm_router / logs",
        "journaux logs/*.log et llm_usage.db ; pas de vue consolidee ni d'evals.",
        "mesure de la qualite et des couts"),
    "security": (
        {"injection", "securite", "security", "sandbox", "permissions", "secrets",
         "jailbreak", "malware", "exfiltration", "hidden", "cachees", "cachee"},
        {"prompt injection", "prompt injections", "indirect prompt injection",
         "supply chain"},
        "vault_security_gate / llm_router",
        "Security Gate statique en lecture seule, contenu source marque UNTRUSTED, secrets "
        "jamais envoyes a un LLM.",
        "resistance aux injections et aux ressources malveillantes"),
    "automation": (
        {"automation", "automatisation", "automatiser", "cron", "scheduler", "routine",
         "routines", "trigger", "triggers", "n8n", "zapier", "webhook"},
        {"persistent agent", "background agent"},
        "mentor_worker / telegram_gateway",
        "taches planifiees Windows, Mentor Worker, Telegram ; aucune action externe sans clic.",
        "routines utiles sans perte de controle"),
}

# Ressource installable / executable citee : Security Gate obligatoire.
_EXTERNAL_RESOURCE = re.compile(
    r"(github\.com|gitlab\.com|\brepo\b|\brepository\b|\bnpm\b|\bpip install\b|\bskill\b|"
    r"\bskills\b|\bmcp\b|\bplugin\b|\bextension\b|\bscript\b|\binstall\b|\binstaller\b)", re.I)
MIN_HITS = 3  # un terme fort compte pour 3


def _fold(text):
    import unicodedata

    text = unicodedata.normalize("NFD", str(text or ""))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn").lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def assess(source_id, title, text, *, url=None):
    """
    Candidat d'amelioration du LIS (dict) ou None. Deterministe, sans LLM, sans ecriture.
    Rien n'est invente : l'ecart et le gain sont formules comme « a evaluer ».
    """
    content = f"{title or ''}\n{text or ''}"
    folded = f" {_fold(content)} "
    if not _AI_CONTEXT.search(folded):
        return None
    words = set(folded.split())
    scored = []
    for area, (keywords, strong, component, state, gain) in AREAS.items():
        hits = sorted(words & keywords)
        strong_hits = sorted(t for t in strong if f" {t} " in folded)
        # singulier / pluriel comptent une fois (skill / skills, agent / agents)
        score = len({h.rstrip('s') for h in hits}) + 3 * len(strong_hits)
        if score >= MIN_HITS:
            scored.append((score, area, hits + strong_hits, component, state, gain))
    injections = gate.scan_injection(content)
    if injections and not any(item[1] == "security" for item in scored):
        # Une injection observee dans une source IA est une donnee utile a la securite du LIS.
        _, _, component, state, gain = AREAS["security"]
        scored.append((MIN_HITS, "security", ["prompt injection observee"], component, state,
                       gain))
    if not scored:
        return None
    scored.sort(key=lambda item: -item[0])
    score, area, matched, component, state, gain = scored[0]
    external = bool(_EXTERNAL_RESOURCE.search(content)) or bool(url and "github.com" in url)
    risks = []
    if injections:
        risks.append("injection indirecte detectee dans la source : contenu traite comme "
                     "donnee, jamais comme instruction")
    if external:
        risks.append("ressource externe (repo / skill / MCP / script) non fiable par defaut")
    label = (title or f"source {source_id}").strip()
    return {
        "lis_improvement_candidate": True,
        "source_id": source_id,
        "capability_area": area,
        "secondary_areas": [item[1] for item in scored[1:3]],
        "lis_component": component,
        "current_lis_state": state,
        "gap_detected": f"a evaluer : la source traite de {area.replace('_', ' ')} "
                        f"({', '.join(matched[:5])}) ; comparer avec l'etat actuel.",
        "proposed_improvement": f"examiner si « {label[:90]} » apporte une amelioration a "
                                f"{component} (idee a qualifier, rien n'est engage).",
        "expected_gain": gain,
        "implementation_complexity": "inconnue : a estimer apres revue",
        "risks": risks,
        "evidence": {"source_id": source_id, "title": label[:160], "url": url,
                     "matched_terms": matched[:8], "prompt_injection": injections},
        "suspicious": bool(injections),
        "security_gate_required": external,
        "next_safe_action": ("Security Gate en lecture seule (python vault_security_gate.py "
                             "CHEMIN) puis revue humaine ; aucune installation"
                             if external else "revue humaine de l'idee ; aucune modification "
                                              "automatique du LIS"),
        "review_required": True,
    }


def scan(conn, limit=20):
    """Lecture seule : sources du Vault candidates a l'amelioration du LIS."""
    rows = conn.execute("""
        SELECT s.id AS source_id, COALESCE(c.title, s.title) AS title,
               s.source_url AS url, c.description_original, c.description_ai,
               c.transcript
          FROM intelligence_sources s LEFT JOIN contents c ON c.id = s.content_id
         ORDER BY s.id DESC""").fetchall()
    found = []
    for row in rows:
        row = dict(row)
        # Notes manuelles exclues : la detection porte sur le contenu de la source.
        text = "\n".join(str(row.get(k) or "") for k in
                         ("description_original", "description_ai", "transcript"))
        item = assess(row["source_id"], row["title"], text, url=row["url"])
        if item:
            found.append(item)
            if len(found) >= limit:
                break
    return found


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Candidats d'amelioration du LIS (lecture)")
    parser.add_argument("--scan", action="store_true", required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--db", default=str(ROOT / "vault.db"))
    args = parser.parse_args(argv)
    import vault_ask
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    conn = vault_ask.connect_readonly(args.db)
    try:
        items = scan(conn, args.limit)
    finally:
        conn.close()
    print(json.dumps(items, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
