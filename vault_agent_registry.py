#!/usr/bin/env python3
"""
vault_agent_registry.py - Charge les profils Agency Agents comme couche de raisonnement.

Le depot msitarzewski/agency-agents n'est pas copie dans le repo principal.
Il est clone localement dans external/agency-agents par setup_upstreams.ps1.

Ce module :
- decouvre les profils agents directement depuis le checkout upstream ;
- parse le frontmatter minimal sans dependance externe ;
- permet de chercher un agent par nom, description ou chemin ;
- retourne le Markdown complet pour qu'un orchestrateur puisse l'utiliser
  comme instruction specialisee.

Aucun agent n'est appele automatiquement ici : ce registre ne fait que fournir
les profils. Le routage et les permissions restent controles par le Vault.
"""

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_AGENCY_DIR = ROOT / "external" / "agency-agents"

IGNORED_TOP_LEVEL = {
    ".git",
    ".github",
    "assets",
    "docs",
    "integrations",
    "scripts",
    "translations",
}


class AgentRegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentProfile:
    slug: str
    name: str
    description: str
    path: str
    markdown: str
    source_repo: str = "msitarzewski/agency-agents"


def agency_dir():
    return Path(
        os.environ.get("AGENCY_AGENTS_DIR") or DEFAULT_AGENCY_DIR
    ).resolve()


def _parse_frontmatter(text):
    if not text.startswith("---"):
        return {}
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, flags=re.S)
    if not match:
        return {}

    result = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in {"name", "description", "vibe", "emoji"}:
            result[key] = value
    return result


def _is_agent_file(path, root):
    if path.name.lower() in {"readme.md", "contributing.md"}:
        return False
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if relative.parts and relative.parts[0] in IGNORED_TOP_LEVEL:
        return False
    return True


def discover_agents(root=None):
    root = Path(root or agency_dir())
    if not root.exists():
        raise AgentRegistryError(
            f"Agency Agents absent: {root}. Lance setup_upstreams.ps1."
        )

    agents = []
    for path in sorted(root.rglob("*.md")):
        if not _is_agent_file(path, root):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = _parse_frontmatter(text)
        name = (meta.get("name") or "").strip()
        description = (meta.get("description") or "").strip()
        if not name or not description:
            continue

        relative = path.relative_to(root).as_posix()
        slug = path.stem
        agents.append(
            AgentProfile(
                slug=slug,
                name=name,
                description=description,
                path=relative,
                markdown=text,
            )
        )
    return agents


def _tokens(value):
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (value or "").lower())
        if len(token) >= 2
    }


def search_agents(query, top=8, root=None):
    wanted = _tokens(query)
    if not wanted:
        return []

    scored = []
    for agent in discover_agents(root=root):
        name_tokens = _tokens(agent.name)
        desc_tokens = _tokens(agent.description)
        path_tokens = _tokens(agent.path.replace("/", " "))

        score = 0
        score += 6 * len(wanted & name_tokens)
        score += 3 * len(wanted & desc_tokens)
        score += 1 * len(wanted & path_tokens)

        query_folded = " ".join(sorted(wanted))
        if query.lower() in agent.name.lower():
            score += 12
        if query.lower() in agent.description.lower():
            score += 6
        if query_folded and query_folded in agent.description.lower():
            score += 2

        if score:
            scored.append((score, agent))

    scored.sort(key=lambda pair: (-pair[0], pair[1].name.lower()))
    return [agent for _, agent in scored[: max(1, int(top))]]


def get_agent(identifier, root=None):
    needle = (identifier or "").strip().lower()
    if not needle:
        raise AgentRegistryError("Agent identifier is empty")

    agents = discover_agents(root=root)
    exact = [
        agent
        for agent in agents
        if agent.slug.lower() == needle or agent.name.lower() == needle
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AgentRegistryError(f"Ambiguous agent identifier: {identifier!r}")

    matches = search_agents(identifier, top=5, root=root)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise AgentRegistryError(f"Agent not found: {identifier!r}")
    raise AgentRegistryError(
        "Agent identifier is ambiguous: "
        + ", ".join(agent.name for agent in matches)
    )


def main():
    parser = argparse.ArgumentParser(
        description="Registre local des profils Agency Agents"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true")
    group.add_argument("--search")
    group.add_argument("--show")
    parser.add_argument("--top", type=int, default=8)
    args = parser.parse_args()

    if args.list:
        agents = discover_agents()
        print(f"{len(agents)} agents disponibles")
        for agent in agents:
            print(f"- {agent.name} [{agent.path}]")
        return

    if args.search:
        for agent in search_agents(args.search, top=args.top):
            print(f"{agent.name}\t{agent.path}\t{agent.description}")
        return

    agent = get_agent(args.show)
    print(agent.markdown)


if __name__ == "__main__":
    main()
