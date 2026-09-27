# Upstream integrations

This project deliberately keeps two public repositories as upstream inputs rather
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

## Setup / update

Run: .\setup_upstreams.ps1

The script shallow-clones or refreshes both upstream repositories.

## Provider rule

The Vault runtime does not use a Mistral API provider. Local Qwen remains the
default analysis path. Other cloud fallbacks, if ever enabled, must be explicitly
configured and must not become a silent dependency of ingestion.
