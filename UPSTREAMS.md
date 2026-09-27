# Upstream integrations

This project deliberately keeps three public repositories as upstream inputs rather
than copying them wholesale into the private Vault repository.

## 1. sosoj92/reels-vault

Role: ingestion foundation.

The original repository provides the base pattern for:
- extracting TikTok / Instagram links;
- downloading media with yt-dlp / gallery-dl;
- local Whisper transcription;
- raw Markdown records;
- resumable ingestion through a journal;
- scheduled inbox processing.

The private Vault extends this foundation with SQLite V2/V3, entities, claims,
relations, verification states, signals, actions, Notion projection, mobile
Syncthing/MacroDroid ingestion and controlled automation.

Rule: upstream changes are reviewed and selectively ported. They never overwrite
the private V2/V3 data model automatically.

Local checkout: external/reels-vault-upstream

## 2. msitarzewski/agency-agents

Role: reasoning playbooks ("how to think"), not source-of-truth data.

Profiles are loaded from the local upstream checkout by
vault_agent_registry.py. The Vault/orchestrator can select a specialist profile
for a task while keeping:
- Vault permissions authoritative;
- source provenance authoritative;
- sensitive-action validation authoritative;
- the underlying LLM/provider replaceable.

The full upstream roster remains available; we do not inject every agent into
every prompt. The orchestrator should select only the smallest relevant profile
set for the current task.

Local checkout: external/agency-agents

Examples especially relevant to this project include:
- AI Engineer
- Multi-Agent Systems Architect
- Knowledge Graph Engineer
- Autonomous Optimization Architect
- Codebase Onboarding Engineer
- Data Engineer
- DevOps Automator
- Privacy Engineer
- Prompt Engineer
- Minimal Change Engineer
- Code Reviewer

## 3. sosoj92/jarvis-assistant-vocal

Role: orchestration, permissions and execution-pattern reference.

The Jarvis repository contributes architectural patterns that are directly useful
to the Life Intelligence System:
- separate fast execution from slower background reasoning;
- keep the execution layer ("keys and body") distinct from the reasoning layer;
- expose tools by explicit opt-in rather than by default;
- use confirmation tiers for sensitive or irreversible actions;
- isolate credentials from delegated reasoning workers;
- run long tasks asynchronously and track their state;
- keep local-first / loopback boundaries for sensitive capabilities;
- use budgets, retry caps, cooldowns and circuit-breaker style guardrails;
- treat cockpit/UI surfaces as projections rather than the authoritative memory.

The private Vault does NOT copy Jarvis wholesale and does not inherit its cloud
providers or credentials. We selectively port patterns that fit the existing
Vault doctrine. Vault/SQLite remains the source of truth, Notion remains a cockpit
projection, and approvals remain authoritative.

Local checkout: external/jarvis-assistant-vocal

Relevant upstream references include:
- docs/hermes.md
- docs/mcp.md
- docs/hub_contenu.md
- docs/costs.md
- docs/cockpit.md

## Setup / update

Run: .\setup_upstreams.ps1

The script shallow-clones or refreshes all three upstream repositories.

## Provider rule

The Vault runtime does not use a Mistral API provider. Local Qwen remains the
default analysis path. Other cloud fallbacks, if ever enabled, must be explicitly
configured and must not become a silent dependency of ingestion.
