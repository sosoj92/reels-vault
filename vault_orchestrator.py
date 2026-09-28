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
    args = parser.parse_args()

    task = " ".join(args.task).strip()
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
