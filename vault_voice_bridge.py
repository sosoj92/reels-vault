#!/usr/bin/env python3
"""
vault_voice_bridge.py - Pont deterministe entre la voix (Jarvis) et Agency Agents.

Jarvis reste la couche vocale :
    micro -> STT local -> ce bridge -> agent(s) + contexte Vault -> LLM -> TTS

Ce module ne lance aucun micro, aucun TTS et aucune action externe.
Il prepare seulement un paquet de raisonnement borne et traçable.

Trois usages :
- adressage explicite : "demande au Knowledge Graph Engineer..."
- routage naturel : la demande est envoyee au plus petit ensemble d'agents utile.
- --ask : Ask Life Intelligence RETRIEVAL-FIRST (vault_ask.py). Reponse
  deterministe citee immediate, synthese Qwen locale optionnelle sous
  echeance. C'est le chemin utilise par l'outil Jarvis.

Les permissions et la provenance du Vault restent autoritatives.
"""

import re

import vault_intelligence_context as intelligence_context
import vault_orchestrator as orchestrator
from vault_agent_registry import (
    AgentRegistryError,
    discover_agents,
    get_agent,
)


class VoiceBridgeError(ValueError):
    pass


def _compact_playbook(markdown, max_chars=5000):
    """
    Extrait les sections les plus utiles a une conversation vocale.
    On garde la mission, les regles et le workflow sans envoyer tout le profil.
    """
    text = str(markdown or "")
    wanted = (
        "core mission",
        "critical rules",
        "workflow process",
        "communication style",
    )
    sections = []
    current = None
    buffer = []

    def flush():
        nonlocal current, buffer
        if current and buffer:
            body = "\n".join(buffer).strip()
            if body:
                sections.append(f"## {current}\n{body}")
        current = None
        buffer = []

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            title = line[3:].strip()
            folded = title.casefold()
            if any(key in folded for key in wanted):
                current = title
            continue
        if current:
            buffer.append(line)
    flush()

    result = "\n\n".join(sections).strip()
    if not result:
        result = text.strip()
    return result[:max_chars]


def _public_agent(agent, include_playbook=False):
    data = {
        "name": agent.name,
        "description": agent.description,
        "path": agent.path,
    }
    if include_playbook:
        data["playbook"] = _compact_playbook(agent.markdown)
    return data


def _explicit_agent_from_text(text, agents):
    folded = (text or "").casefold()
    matches = []
    for agent in agents:
        name = agent.name.casefold()
        slug = agent.slug.replace("-", " ").casefold()
        if name in folded or (slug and slug in folded):
            matches.append(agent)

    # Le nom le plus long est le plus specifique si plusieurs profils se recouvrent.
    matches.sort(key=lambda item: (-len(item.name), item.name.casefold()))
    return matches[0] if matches else None


def route_voice_request(
    conn,
    text,
    *,
    requested_agent=None,
    max_agents=2,
    include_vault_context=True,
):
    text = (text or "").strip()
    if not text:
        raise VoiceBridgeError("voice request is empty")

    max_agents = max(1, min(int(max_agents), 3))
    agents = discover_agents()

    explicit = None
    if requested_agent:
        try:
            explicit = get_agent(requested_agent)
        except AgentRegistryError as exc:
            raise VoiceBridgeError(str(exc)) from exc
    else:
        explicit = _explicit_agent_from_text(text, agents)

    if explicit is not None:
        selected = [explicit]
        routes = [
            {
                "route": "explicit_agent",
                "score": 100,
                "matched_keywords": [explicit.name],
            }
        ]
        mode = "explicit"
    else:
        selection = orchestrator.select_agents(
            text,
            agents=agents,
            max_agents=max_agents,
        )
        selected = selection["agents"]
        routes = selection["routes"]
        mode = "auto"

    vault_context = None
    if include_vault_context:
        try:
            vault_context = intelligence_context.build_context_pack(
                conn,
                text,
                entity_limit=4,
                claims_per_entity=4,
                sources_per_entity=3,
                relations_per_entity=4,
            )
        except intelligence_context.IntelligenceContextError:
            vault_context = {
                "query": text,
                "entities": [],
                "note": "No grounded Vault context matched this request.",
            }

    return {
        "text": text,
        "mode": mode,
        "agents": [_public_agent(agent, include_playbook=True) for agent in selected],
        "routes": routes,
        "vault_context": vault_context,
        "voice_contract": {
            "input": "Jarvis STT transcript",
            "output": "Jarvis TTS after confidentiality filtering",
            "external_actions": "never from this bridge",
            "sensitive_actions": "must follow Vault approval rules",
            "max_agents": max_agents,
        },
    }


def build_voice_reasoning_prompt(
    conn,
    text,
    *,
    requested_agent=None,
    max_agents=2,
):
    packet = route_voice_request(
        conn,
        text,
        requested_agent=requested_agent,
        max_agents=max_agents,
        include_vault_context=True,
    )

    # Reconstruit le contexte complet uniquement pour les agents selectionnes.
    selected_names = {item["name"] for item in packet["agents"]}
    all_agents = discover_agents()
    selected = [
        agent for agent in all_agents
        if agent.name in selected_names
    ]

    # Playbooks compacts : le markdown integral (dizaines de milliers de
    # caracteres) faisait expirer les LLM locaux.
    playbooks = []
    for index, agent in enumerate(selected, start=1):
        playbooks.append(
            f"--- PLAYBOOK {index}: {agent.name} ---\n"
            f"{_compact_playbook(agent.markdown, max_chars=2500)}"
        )

    return "\n\n".join(
        [
            orchestrator.VAULT_GUARDRAILS,
            "VOICE RESPONSE RULES:\n"
            "- Answer in a concise spoken style.\n"
            "- Never read secrets, tokens, credentials or raw sensitive identifiers aloud.\n"
            "- If an action would modify or send something externally, propose it instead of executing it.\n"
            "- Ground Vault-specific claims only in VAULT CONTEXT.",
            f"USER SPOKEN REQUEST:\n{text}",
            "VAULT CONTEXT:\n" + repr(packet["vault_context"]),
            "SELECTED REASONING PLAYBOOKS:\n" + "\n\n".join(playbooks),
        ]
    )


def main():
    import argparse
    import json

    import vault_intelligence as intelligence

    parser = argparse.ArgumentParser(
        description="Route une demande vocale vers Agency Agents + contexte Vault"
    )
    parser.add_argument("text", nargs="+")
    parser.add_argument("--db", default="vault.db")
    parser.add_argument("--agent")
    parser.add_argument("--max-agents", type=int, default=2)
    parser.add_argument("--prompt", action="store_true")
    parser.add_argument(
        "--ask",
        action="store_true",
        help="Ask Life Intelligence retrieval-first (reponse texte citee)",
    )
    parser.add_argument("--mode", choices=("ask", "consult"), default="ask")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--llm-timeout", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.ask:
        import vault_ask

        conn = vault_ask.connect_readonly(args.db)
        try:
            result = vault_ask.ask(
                conn,
                " ".join(args.text),
                mode=args.mode,
                agent=args.agent,
                llm="off" if args.no_llm else "auto",
                llm_timeout=(
                    args.llm_timeout
                    if args.llm_timeout is not None
                    else vault_ask.DEFAULT_LLM_TIMEOUT
                ),
            )
        finally:
            conn.close()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(result["answer"])
        return

    conn = intelligence.connect(args.db)
    try:
        spoken = " ".join(args.text)
        if args.prompt:
            print(
                build_voice_reasoning_prompt(
                    conn,
                    spoken,
                    requested_agent=args.agent,
                    max_agents=args.max_agents,
                )
            )
        else:
            print(
                json.dumps(
                    route_voice_request(
                        conn,
                        spoken,
                        requested_agent=args.agent,
                        max_agents=args.max_agents,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
