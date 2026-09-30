#!/usr/bin/env python3
"""
vault_orchestrator.py - Routage deterministe des playbooks Agency Agents.

Objectif:
- Agency Agents definit "comment reflechir";
- le Vault reste source de verite et controle permissions / provenance;
- le routeur choisit un petit nombre de profils pertinents pour une tache;
- aucun LLM ni appel cloud n'est necessaire pour router;
- aucun agent ne peut contourner les regles du Vault.

Ce module ne lance pas encore d'action externe. Il prepare un contexte
d'orchestration reutilisable par les futurs workers / executors.
"""

import argparse
import json
import re
import unicodedata
from dataclasses import asdict

from vault_agent_registry import AgentProfile, discover_agents


MAX_SELECTED_AGENTS = 3

# Regles de routage locales. Les noms correspondent aux profils upstream.
ROUTES = (
    {
        "name": "knowledge_graph",
        "keywords": {
            "entity", "entities", "relation", "relations", "claim", "claims",
            "graph", "knowledge", "source", "sources", "provenance",
            "contradiction", "identity", "resolution", "deduplicate",
            "entite", "entites", "graphe", "preuve", "preuves",
        },
        "agents": (
            "Knowledge Graph Engineer",
            "Data Engineer",
        ),
    },
    {
        "name": "multi_agent_orchestration",
        "keywords": {
            "agent", "agents", "orchestrator", "orchestration", "delegate",
            "routing", "router", "workflow", "multi-agent", "multiagent",
            "autonomous", "coordination", "handoff",
        },
        "agents": (
            "Multi-Agent Systems Architect",
            "AI Engineer",
            "Autonomous Optimization Architect",
        ),
    },
    {
        "name": "automation_reliability",
        "keywords": {
            "cron", "schedule", "scheduled", "task", "worker", "background",
            "retry", "timeout", "pipeline", "automation", "automatisation",
            "syncthing", "macrodroid", "windows", "powershell", "batch",
            "reliability", "monitoring",
        },
        "agents": (
            "DevOps Automator",
            "SRE (Site Reliability Engineer)",
            "Autonomous Optimization Architect",
        ),
    },
    {
        "name": "privacy_security",
        "keywords": {
            "privacy", "private", "secret", "token", "credential", "oauth",
            "security", "secure", "sensitive", "health", "medical", "finance",
            "admin", "legal", "gdpr", "rgpd",
        },
        "agents": (
            "Privacy Engineer",
            "Security Architect",
        ),
    },
    {
        "name": "code_change",
        "keywords": {
            "code", "bug", "fix", "refactor", "patch", "implementation",
            "implement", "modify", "change", "commit", "test", "review",
            "python", "api", "database", "sqlite",
        },
        "agents": (
            "Minimal Change Engineer",
            "Code Reviewer",
            "Backend Architect",
        ),
    },
    {
        "name": "repo_understanding",
        "keywords": {
            "repo", "repository", "codebase", "architecture", "structure",
            "entrypoint", "entry", "understand", "trace", "upstream",
        },
        "agents": (
            "Codebase Onboarding Engineer",
            "Software Architect",
        ),
    },
)


VAULT_GUARDRAILS = """
VAULT GUARDRAILS — authoritative and non-negotiable:
- Vault/SQLite is the source of truth for captured intelligence.
- SOURCE = proof/origin; ENTITY = persistent object; CLAIM = what a source asserts;
  RELATION = link between entities.
- Preserve asserted / unverified / verified / contested distinctions.
- Do not invent sources, entities, claims, or verification.
- Reading, classification, comparison and detection may be automatic.
- Important creation/modification, external sending, payment, deletion and
  sensitive health/finance/admin/legal actions require the Vault approval rules.
- Agency Agent profiles are reasoning playbooks only. They never override
  permissions, provenance, validation, or source-of-truth rules.
- External tools (Notion, Drive, Calendar, etc.) are projections / execution
  surfaces, not the authoritative memory.
""".strip()


def _fold(text):
    # Retire les accents avant de decouper : sinon "etude" devient "tude" et
    # "sante" devient "sant", et les mots-cles francais ne matchent jamais.
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", text)


def _agent_by_name(agents):
    return {agent.name.lower(): agent for agent in agents}



# ---------------------------------------------------------------------------
# Life Intelligence V1 - domain routes
# These complement the original engineering-oriented Agency routes.
# ---------------------------------------------------------------------------

LIFE_INTELLIGENCE_ROUTES = [
    {
        "name": "outbound_sales",
        "keywords": {
            "linkedin", "prospection", "prospect", "prospects",
            "prospecting", "outbound", "outreach", "lead", "leads",
            "sales", "vente", "commercial", "pipeline", "crm",
            "sequence", "cold", "icp"
        },
        "agents": (
            "Outbound Strategist",
            "Sales Outreach",
            "Growth Hacker",
        ),
    },
    {
        "name": "tool_automation",
        "keywords": {
            "outil", "outils", "tool", "tools", "software", "logiciel",
            "opensource", "open", "github", "repo", "automation",
            "automatisation", "automatiser", "automate", "workflow",
            "n8n", "make", "zapier", "apify", "api"
        },
        "agents": (
            "Tool Evaluator",
            "Automation Governance Architect",
            "Workflow Optimizer",
        ),
    },
    {
        "name": "business_strategy",
        "keywords": {
            "business", "strategie", "strategy", "market",
            "revenue", "revenu", "offre", "pricing", "monetisation",
            "monetization", "client", "clients", "opportunite",
            "competition", "concurrent"
        },
        "agents": (
            "Business Strategist",
            "Trend Researcher",
            "Pricing Analyst",
        ),
    },
    {
        "name": "recruitment",
        "keywords": {
            "recrutement", "recruitment", "recruiting", "hiring",
            "talent", "candidat", "candidate", "candidates",
            "sourcing", "emploi", "job", "cv", "rh"
        },
        "agents": (
            "Recruitment Specialist",
            "Outbound Strategist",
            "Business Strategist",
        ),
    },
    {
        "name": "supply_chain",
        "keywords": {
            "logistique", "logistics", "supply", "chain",
            "procurement", "approvisionnement", "supplier",
            "fournisseur", "import", "export", "transport",
            "warehouse", "stock"
        },
        "agents": (
            "Supply Chain Strategist",
            "Operations Manager",
            "Business Strategist",
        ),
    },
    {
        "name": "marketing_content",
        "keywords": {
            "marketing", "contenu", "content", "social", "instagram",
            "tiktok", "linkedin", "seo", "audience", "acquisition",
            "conversion", "funnel", "viral", "campaign", "campagne"
        },
        "agents": (
            "Growth Hacker",
            "Social Media Strategist",
            "Content Creator",
        ),
    },
    {
        "name": "web_digital",
        "keywords": {
            "website", "site", "wordpress", "elementor", "cms",
            "seo", "aeo", "ux", "landing", "web", "design"
        },
        "agents": (
            "CMS Developer",
            "SEO Specialist",
            "UX Researcher",
        ),
    },
    {
        "name": "research_evidence",
        "keywords": {
            "research", "recherche", "study", "etude",
            "paper", "evidence", "preuve", "claim", "clinical",
            "clinique", "tdah", "adhd", "autism", "autisme",
            "sante", "health", "trial", "essai"
        },
        "agents": (
            "Research Synthesist",
            "Statistician",
            "Psychologist",
        ),
    },
    {
        "name": "finance",
        "keywords": {
            "finance", "financial", "budget", "cashflow", "cash",
            "investissement", "investment", "rentabilite",
            "margin", "marge", "cout",
            "pricing", "forecast"
        },
        "agents": (
            "Financial Analyst",
            "FP&A Analyst",
            "Investment Researcher",
        ),
    },
]

# Put Life Intelligence routes first.
ROUTES = tuple(LIFE_INTELLIGENCE_ROUTES) + ROUTES

# A match on one of these words alone is not sufficient to select an Agent.
GENERIC_ROUTE_KEYWORDS = {
    "source", "sources",
    "agent", "agents",
    "data",
    "system", "systems",
    "content",
    "tool", "tools",
    "workflow",
    "token", "tokens",
    "api",
    "client", "clients",
}


def _route_scores(task):
    tokens = set(_fold(task))
    scores = []

    for route in ROUTES:
        matches = tokens & route["keywords"]

        if not matches:
            continue

        meaningful = matches - GENERIC_ROUTE_KEYWORDS

        # Do not route based only on vague words such as "source" or "token".
        if not meaningful:
            continue

        # Meaningful matches carry more weight.
        score = (
            len(meaningful) * 2
            + len(matches)
        )

        scores.append(
            (
                score,
                route["name"],
                route,
                sorted(matches),
            )
        )

    scores.sort(
        key=lambda item: (
            -item[0],
            item[1],
        )
    )

    return scores


def select_agents(task, *, agents=None, max_agents=MAX_SELECTED_AGENTS):
    agents = list(agents if agents is not None else discover_agents())
    by_name = _agent_by_name(agents)
    selected = []
    seen = set()
    matched_routes = []

    for score, route_name, route, matches in _route_scores(task):
        matched_routes.append(
            {
                "route": route_name,
                "score": score,
                "matched_keywords": matches,
            }
        )
        for name in route["agents"]:
            agent = by_name.get(name.lower())
            if not agent or agent.name.lower() in seen:
                continue
            selected.append(agent)
            seen.add(agent.name.lower())
            if len(selected) >= max_agents:
                break
        if len(selected) >= max_agents:
            break

    # Fallback tres limite : si aucune route locale ne matche, un seul profil
    # generaliste suffit. On evite de charger une "agence entiere".
    if not selected:
        for fallback in ("AI Engineer", "Codebase Onboarding Engineer"):
            agent = by_name.get(fallback.lower())
            if agent:
                selected.append(agent)
                matched_routes.append(
                    {
                        "route": "fallback",
                        "score": 0,
                        "matched_keywords": [],
                    }
                )
                break

    return {
        "task": task,
        "agents": selected[:max_agents],
        "routes": matched_routes,
    }


def build_orchestration_context(task, *, agents=None, max_agents=MAX_SELECTED_AGENTS):
    selection = select_agents(
        task,
        agents=agents,
        max_agents=max_agents,
    )

    profiles = []
    for agent in selection["agents"]:
        profiles.append(
            {
                "name": agent.name,
                "description": agent.description,
                "path": agent.path,
                "playbook": agent.markdown,
            }
        )

    return {
        "task": task,
        "guardrails": VAULT_GUARDRAILS,
        "routes": selection["routes"],
        "agents": profiles,
    }


def render_prompt_context(task, *, agents=None, max_agents=MAX_SELECTED_AGENTS):
    context = build_orchestration_context(
        task,
        agents=agents,
        max_agents=max_agents,
    )
    sections = [
        VAULT_GUARDRAILS,
        "",
        f"CURRENT TASK:\n{task}",
        "",
        "SELECTED REASONING PLAYBOOKS:",
    ]

    if not context["agents"]:
        sections.append("(none)")
    else:
        for index, agent in enumerate(context["agents"], start=1):
            sections.extend(
                [
                    "",
                    f"--- PLAYBOOK {index}: {agent['name']} ---",
                    agent["playbook"].strip(),
                ]
            )

    return "\n".join(sections).strip()


# ---------------------------------------------------------------------------
# Paquet de consultation PORTABLE (independant du moteur et de l'interface)
#
# Un Agency Agent est un PLAYBOOK de raisonnement, pas un modele : le meme paquet
# peut etre applique par llm_router (Qwen / OpenRouter), par ChatGPT (colle ou lu
# depuis Notion) ou par un autre moteur. Aucun champ ne fixe un modele : le choix
# de l'agent et le choix du moteur restent deux decisions distinctes.
# ---------------------------------------------------------------------------

PACKET_VERSION = 1
PACKET_PLAYBOOK_CHARS = 2500

EXPECTED_OUTPUT = {
    "language": "francais, tutoiement",
    "structure": "reponse directe a la question ; si plusieurs agents : un avis par agent "
                 "(selon son playbook) puis une synthese",
    "citations": "citer les sources par leur ref (source:ID) ; ne rien leur attribuer "
                 "qu'elles ne contiennent pas",
    "actions": "proposer, jamais executer ; aucune action externe sans validation de Rosy",
    "destination": "analyse ponctuelle -> interface de conversation ; insight durable, "
                   "decision ou amelioration du LIS -> Vault + projection Notion ; tache -> "
                   "Vault + Taches Notion ; rapport lourd -> fichier ; alerte -> Telegram",
}


def _explicit_agents(question, requested, agents):
    """Mode A : agents demandes (noms, slugs, alias) ou nommes dans la question."""
    by_name = {agent.name.casefold(): agent for agent in agents}
    chosen = []
    for item in requested or ():
        agent = item if isinstance(item, AgentProfile) else None
        if agent is None:
            try:
                from vault_agent_registry import get_agent

                agent = get_agent(str(item))
            except Exception:  # noqa: BLE001 - nom inconnu : ignore, signale plus bas
                agent = None
        if agent is not None and all(a.name != agent.name for a in chosen):
            chosen.append(agent)
    if requested:
        return chosen
    try:
        import mentor_council  # alias existants (CFO, growth...) : pas de second resolveur

        named = mentor_council.agents_in_text(question)
    except Exception:  # noqa: BLE001 - registre Telegram indisponible : noms complets seuls
        folded = (question or "").casefold()
        named = sorted((a for a in agents if a.name.casefold() in folded),
                       key=lambda a: folded.index(a.name.casefold()))
    return [by_name.get(a.name.casefold(), a) for a in named]


def _packet_sources(sources):
    """Preuves sans note personnelle ni secret ; marquees comme contenu non fiable."""
    import llm_router
    from vault_ask import public_only

    items = []
    for source in public_only(list(sources or [])):
        excerpt = source.get("snippet") if source.get("snippet_field") else ""
        excerpt = " ".join(str(excerpt or "").split())[:400]
        if excerpt and llm_router.contains_secret(excerpt):
            excerpt = ""
        items.append({
            "ref": f"source:{source.get('source_id')}",
            "title": " ".join(str(source.get("title") or "").split())[:160],
            "url": source.get("url"),
            "excerpt": excerpt,
            "trust": "untrusted_source_content",
        })
    return items


def build_consultation_packet(question, *, agents=None, requested_agents=(),
                              max_agents=MAX_SELECTED_AGENTS, evidence_sources=(),
                              project_context=None, auto_select=True,
                              playbook_chars=PACKET_PLAYBOOK_CHARS):
    """
    Paquet autonome de consultation, construit SANS appel LLM :
    question, agents choisis (mode A explicite, sinon mode B routage deterministe),
    playbooks compacts, preuves du Vault, frontiere de securite, sortie attendue,
    provenance. auto_select=False : seulement les agents explicitement demandes.
    """
    from vault_security_gate import UNTRUSTED_RULE
    from vault_voice_bridge import _compact_playbook

    question = " ".join(str(question or "").split())
    pool = list(agents if agents is not None else discover_agents())
    max_agents = max(1, min(int(max_agents), MAX_SELECTED_AGENTS))
    explicit = _explicit_agents(question, requested_agents, pool)[:max_agents]
    if explicit:
        mode, selected = "A", explicit
        routes = [{"route": "explicit_agent", "matched_keywords": [a.name for a in explicit]}]
    elif auto_select:
        selection = select_agents(question, agents=pool, max_agents=max_agents)
        mode, selected, routes = "B", selection["agents"], selection["routes"]
    else:
        mode, selected, routes = None, [], []
    return {
        "packet_version": PACKET_VERSION,
        "question": question,
        "selection_mode": mode,
        "selected_agents": [{"name": a.name, "slug": a.slug, "description": a.description,
                             "path": a.path} for a in selected],
        "agent_playbooks": [{"name": a.name,
                             "playbook": _compact_playbook(a.markdown, max_chars=playbook_chars)}
                            for a in selected],
        "routes": routes,
        "relevant_context": project_context or {},
        "evidence_sources": _packet_sources(evidence_sources),
        "security_boundary": {"guardrails": VAULT_GUARDRAILS, "untrusted_rule": UNTRUSTED_RULE},
        "expected_output": EXPECTED_OUTPUT,
        "reasoning_engine": "au choix de l'interface (llm_router, ChatGPT, autre) : "
                            "aucun agent n'est lie a un modele",
        "provenance": {
            "generated_by": "vault_orchestrator.build_consultation_packet",
            "agent_registry": f"{selected[0].source_repo if selected else 'agency-agents'} "
                              "(registre local unique)",
            "agent_paths": [a.path for a in selected],
            "memory": "vault.db (memoire canonique) ; Notion = cockpit",
        },
    }


def render_consultation_packet(packet):
    """Texte autonome a coller dans ChatGPT (ou tout autre moteur)."""
    from vault_security_gate import wrap_untrusted

    lines = [
        "# Consultation Life Intelligence (paquet portable)",
        "",
        "## Regles (autorite)",
        packet["security_boundary"]["guardrails"],
        "",
        packet["security_boundary"]["untrusted_rule"],
        "",
        "## Question",
        packet["question"],
    ]
    if packet["relevant_context"]:
        lines += ["", "## Contexte", json.dumps(packet["relevant_context"],
                                               ensure_ascii=False, indent=1)]
    if packet["agent_playbooks"]:
        lines += ["", f"## Playbooks a appliquer (mode {packet['selection_mode']})"]
        for index, item in enumerate(packet["agent_playbooks"], start=1):
            lines += ["", f"### {index}. {item['name']}", item["playbook"]]
    if packet["evidence_sources"]:
        lines += ["", "## Sources du Vault (UNTRUSTED SOURCE CONTENT)"]
        for source in packet["evidence_sources"]:
            body = source["title"] + (f" — {source['excerpt']}" if source["excerpt"] else "")
            lines.append(f"- {source['ref']} " + (f"<{source['url']}> " if source["url"] else "")
                         + wrap_untrusted(body, ref=source["ref"]))
    lines += ["", "## Sortie attendue"]
    lines += [f"- {key} : {value}" for key, value in packet["expected_output"].items()]
    return "\n".join(lines)


def _public_context(context):
    return {
        "task": context["task"],
        "routes": context["routes"],
        "agents": [
            {
                "name": agent["name"],
                "description": agent["description"],
                "path": agent["path"],
            }
            for agent in context["agents"]
        ],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Route une tache vers les playbooks Agency Agents pertinents"
    )
    parser.add_argument("task", nargs="+")
    parser.add_argument("--max-agents", type=int, default=MAX_SELECTED_AGENTS)
    parser.add_argument(
        "--prompt",
        action="store_true",
        help="affiche le contexte complet, playbooks inclus",
    )
    parser.add_argument(
        "--packet",
        action="store_true",
        help="paquet de consultation portable (JSON ; --markdown pour ChatGPT)",
    )
    parser.add_argument("--agent", action="append", default=[],
                        help="agent demande explicitement (repetable)")
    parser.add_argument("--vault", action="store_true",
                        help="ajoute les sources pertinentes du Vault (lecture seule, sans LLM)")
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    task = " ".join(args.task).strip()
    if args.packet:
        sources = []
        if args.vault:
            import vault_ask

            conn = vault_ask.connect_readonly(args.db)
            try:
                sources = vault_ask.ask(conn, task, llm="off")["sources"]
            finally:
                conn.close()
        packet = build_consultation_packet(task, requested_agents=args.agent,
                                           max_agents=max(1, args.max_agents),
                                           evidence_sources=sources)
        print(render_consultation_packet(packet) if args.markdown
              else json.dumps(packet, ensure_ascii=False, indent=2))
        return
    if args.prompt:
        print(
            render_prompt_context(
                task,
                max_agents=max(1, args.max_agents),
            )
        )
        return

    context = build_orchestration_context(
        task,
        max_agents=max(1, args.max_agents),
    )
    print(json.dumps(_public_context(context), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
