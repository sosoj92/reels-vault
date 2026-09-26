#!/usr/bin/env python3
"""
vault_research.py - Recherche externe provider-agnostic pour Life Intelligence V3.

Architecture :
- le coeur manipule un contrat de resultat independant du fournisseur ;
- Mistral est le premier adaptateur implemente ;
- les recherches sont journalisees avant toute mutation d'identite ;
- aucune affirmation metier n'est "verifiee" parce qu'une identite est resolue.

Le worker travaille par defaut sur la file produite par vault_resolution.py.

Variables d'environnement Mistral :
    MISTRAL_API_KEY
    MISTRAL_RESEARCH_MODEL   (defaut: mistral-medium-latest)

Le connecteur utilise l'API Conversations avec web_search et store=false.
Aucune cle n'est ecrite dans vault.db ni dans les logs.
"""

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from typing import Protocol

import vault_identity as identity
import vault_intelligence as intelligence
import vault_resolution as resolution


RESEARCH_SCHEMA_VERSION = 1

RESEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_research_findings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id          INTEGER NOT NULL,
    provider           TEXT NOT NULL,
    model              TEXT,
    status             TEXT NOT NULL
                       CHECK (status IN ('resolved', 'ambiguous', 'not_found', 'error')),
    canonical_name     TEXT,
    canonical_url      TEXT,
    resolved_type      TEXT,
    primary_source_url TEXT,
    aliases            TEXT NOT NULL DEFAULT '[]',
    evidence_note      TEXT,
    raw_result         TEXT NOT NULL DEFAULT '{}',
    safe_to_apply      INTEGER NOT NULL DEFAULT 0
                       CHECK (safe_to_apply IN (0, 1)),
    applied_at         TEXT,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_findings_entity
    ON intelligence_research_findings(entity_id);
CREATE INDEX IF NOT EXISTS idx_research_findings_status
    ON intelligence_research_findings(status);
"""


class ResearchError(RuntimeError):
    pass


class ResearchRateLimitError(ResearchError):
    """Rate limit persistant apres les retries du provider."""
    pass


@dataclass
class ResearchFinding:
    status: str
    canonical_name: str | None = None
    canonical_url: str | None = None
    entity_type: str | None = None
    primary_source_url: str | None = None
    aliases: list[str] | None = None
    evidence_note: str | None = None

    def normalized(self):
        aliases = self.aliases if isinstance(self.aliases, list) else []
        return ResearchFinding(
            status=(self.status or "").strip().lower(),
            canonical_name=_clean_text(self.canonical_name),
            canonical_url=_clean_url(self.canonical_url),
            entity_type=_clean_text(self.entity_type),
            primary_source_url=_clean_url(self.primary_source_url),
            aliases=[
                x for x in (_clean_text(a) for a in aliases)
                if x
            ],
            evidence_note=_clean_text(self.evidence_note),
        )


class ResearchProvider(Protocol):
    name: str
    model: str | None

    def research_identity(self, item: dict) -> ResearchFinding:
        ...


def ensure_research_schema(conn):
    resolution.ensure_resolution_schema(conn)
    conn.executescript(RESEARCH_SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('research_schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(RESEARCH_SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def _clean_text(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _clean_url(value):
    value = _clean_text(value)
    if not value:
        return None
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            "",
        )
    )


def _fold(value):
    value = (value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _host(url):
    if not url:
        return None
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _same_host(a, b):
    return bool(_host(a) and _host(a) == _host(b))


def _name_supported(entity_name, finding):
    """
    Garde-fou volontairement simple :
    le nom original doit correspondre au nom canonique ou a un alias,
    apres normalisation legere. Pas de fuzzy matching automatique ici.
    """
    needle = _fold(entity_name)
    candidates = [finding.canonical_name] + list(finding.aliases or [])
    return needle and any(_fold(value) == needle for value in candidates if value)


def finding_is_safe(entity, finding):
    """
    Auto-application tres conservative d'une recherche externe.

    Conditions :
    - seulement pour une entite provisoire de type solution ;
    - resultat explicitement resolved ;
    - URL canonique et source primaire valides ;
    - meme host entre URL canonique et preuve primaire ;
    - nom exact present dans canonical_name ou aliases.

    Les autres types restent en revue humaine/agentique.
    """
    finding = finding.normalized()

    return bool(
        entity.get("entity_type") == "solution"
        and finding.status == "resolved"
        and finding.canonical_name
        and finding.canonical_url
        and finding.primary_source_url
        and _same_host(finding.canonical_url, finding.primary_source_url)
        and _name_supported(entity.get("name"), finding)
    )


def build_identity_prompt(item):
    """
    Prompt minimal : recherche d'identite uniquement, jamais de claims marketing.
    """
    resource_names = item.get("resource_names") or []
    return f"""
Resolve the identity of this entity from a personal knowledge vault.

Entity name: {item.get('name')}
Current provisional type: {item.get('entity_type')}
Names seen in source material: {json.dumps(resource_names, ensure_ascii=False)}
Number of independent saved sources mentioning it: {item.get('source_count', 0)}

Use web search and prefer the entity's own official website or official repository.

Your task is ONLY identity resolution. Do not verify product claims, quality,
pricing, medical claims, financial claims, or recommendations.

Return exactly one JSON object with these keys:
{{
  "status": "resolved" | "ambiguous" | "not_found",
  "canonical_name": string | null,
  "canonical_url": string | null,
  "entity_type": string | null,
  "primary_source_url": string | null,
  "aliases": [string, ...],
  "evidence_note": string | null
}}

Rules:
- status="resolved" only if the identity is unambiguous from an official source.
- canonical_url must be the official homepage or official canonical repository URL.
- primary_source_url must be an official first-party URL supporting that identity.
- If canonical_url is on an official website, primary_source_url MUST be that same
  URL or another official page on the SAME HOSTNAME whenever such a page exists.
- Do not use a GitHub repository as primary_source_url when an official website
  page on the canonical hostname already supports the identity.
- Use an official repository as canonical_url/primary_source_url when the entity
  itself is specifically a repository, or when no first-party website exists.
- Include the exact vault name in aliases if the official canonical name differs.
- If multiple plausible entities share the name, return ambiguous.
- Never invent an URL, company, person, repository, product, or alias.
- Return JSON only.
""".strip()


def _extract_text_chunks(value):
    """
    Extraction tolerante aux variantes de reponse Conversations.
    On cherche en priorite les contenus assistant, puis les champs text/content.
    """
    found = []

    def walk(node):
        if isinstance(node, dict):
            role = node.get("role")
            content = node.get("content")
            if role == "assistant":
                if isinstance(content, str):
                    found.append(content)
                elif isinstance(content, list):
                    for chunk in content:
                        if isinstance(chunk, dict):
                            text = chunk.get("text") or chunk.get("content")
                            if isinstance(text, str):
                                found.append(text)

            if node.get("type") in ("text", "message.output"):
                text = node.get("text")
                if isinstance(text, str):
                    found.append(text)

            for child in node.values():
                walk(child)

        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


def _json_object_from_text(text):
    text = (text or "").strip()
    if not text:
        raise ResearchError("empty model response")

    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    # Fallback prudent pour une reponse entouree de texte/markdown.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start:end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

    raise ResearchError("model response does not contain a valid JSON object")


def parse_provider_response(payload):
    chunks = _extract_text_chunks(payload)
    if not chunks:
        raise ResearchError("no assistant text found in provider response")

    last_error = None
    for chunk in reversed(chunks):
        try:
            data = _json_object_from_text(chunk)
            return ResearchFinding(
                status=data.get("status"),
                canonical_name=data.get("canonical_name"),
                canonical_url=data.get("canonical_url"),
                entity_type=data.get("entity_type"),
                primary_source_url=data.get("primary_source_url"),
                aliases=data.get("aliases") or [],
                evidence_note=data.get("evidence_note"),
            ).normalized()
        except ResearchError as exc:
            last_error = exc

    raise last_error or ResearchError("unable to parse provider response")


class MistralWebResearchProvider:
    name = "mistral"

    def __init__(self, api_key=None, model=None, timeout=90):
        self.api_key = api_key or os.environ.get("MISTRAL_API_KEY")
        self.model = model or os.environ.get(
            "MISTRAL_RESEARCH_MODEL",
            "mistral-medium-latest",
        )
        self.timeout = timeout
        self.max_retries = int(os.environ.get("MISTRAL_RESEARCH_MAX_RETRIES", "5"))
        self.backoff_seconds = float(os.environ.get("MISTRAL_RESEARCH_BACKOFF_SECONDS", "1"))
        self.max_backoff_seconds = float(os.environ.get("MISTRAL_RESEARCH_MAX_BACKOFF_SECONDS", "30"))
        self.min_interval_seconds = float(os.environ.get("MISTRAL_RESEARCH_MIN_INTERVAL_SECONDS", "1"))
        self._last_request_at = 0.0

        if not self.api_key:
            raise ResearchError(
                "MISTRAL_API_KEY is not set. Do not paste the key into the Vault."
            )

    def research_identity(self, item):
        payload = {
            "model": self.model,
            "inputs": build_identity_prompt(item),
            "instructions": (
                "You are an identity-resolution research worker. "
                "Use first-party sources, be conservative, and return JSON only."
            ),
            "tools": [{"type": "web_search"}],
            "store": False,
        }

        request = urllib.request.Request(
            "https://api.mistral.ai/v1/conversations",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ResearchError(
                f"Mistral HTTP {exc.code}: {body[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ResearchError(f"Mistral network error: {exc}") from exc

        try:
            response_payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ResearchError("Mistral returned non-JSON response") from exc

        return parse_provider_response(response_payload)


def provider_from_name(name):
    name = (name or "").strip().lower()
    if name == "mistral":
        return MistralWebResearchProvider()
    raise ResearchError(
        f"unsupported research provider: {name!r}. "
        "The core is provider-agnostic; configure an installed adapter."
    )


def _get_entity(conn, entity_id):
    row = conn.execute(
        "SELECT * FROM intelligence_entities WHERE id = ?",
        (entity_id,),
    ).fetchone()
    return dict(row) if row else None


def save_finding(conn, entity, provider, finding):
    ensure_research_schema(conn)
    finding = finding.normalized()

    if finding.status not in ("resolved", "ambiguous", "not_found"):
        raise ResearchError(f"invalid finding status: {finding.status!r}")

    safe = finding_is_safe(entity, finding)
    stamp = intelligence.now()

    cur = conn.execute(
        """
        INSERT INTO intelligence_research_findings(
            entity_id, provider, model, status,
            canonical_name, canonical_url, resolved_type,
            primary_source_url, aliases, evidence_note,
            raw_result, safe_to_apply, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity["id"],
            provider.name,
            getattr(provider, "model", None),
            finding.status,
            finding.canonical_name,
            finding.canonical_url,
            finding.entity_type,
            finding.primary_source_url,
            json.dumps(finding.aliases or [], ensure_ascii=False),
            finding.evidence_note,
            json.dumps(asdict(finding), ensure_ascii=False, sort_keys=True),
            1 if safe else 0,
            stamp,
        ),
    )
    conn.commit()
    return cur.lastrowid, safe


def research_queue_items(conn, provider, limit=5):
    ensure_research_schema(conn)

    queue = resolution.research_queue(conn, limit=200)
    researched_entity_ids = {
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT entity_id
              FROM intelligence_research_findings
             WHERE provider = ?
               AND status IN ('resolved', 'ambiguous', 'not_found')
            """,
            (provider.name,),
        ).fetchall()
    }

    selected = [
        item for item in queue
        if item["entity_id"] not in researched_entity_ids
    ][:limit]

    results = []
    for item in selected:
        entity = _get_entity(conn, item["entity_id"])
        if not entity:
            continue

        try:
            finding = provider.research_identity(item)
            finding_id, safe = save_finding(conn, entity, provider, finding)
            results.append(
                {
                    "entity_id": entity["id"],
                    "name": entity["name"],
                    "finding_id": finding_id,
                    "status": finding.status,
                    "canonical_name": finding.canonical_name,
                    "canonical_url": finding.canonical_url,
                    "primary_source_url": finding.primary_source_url,
                    "safe_to_apply": safe,
                }
            )
        except Exception as exc:
            stamp = intelligence.now()
            cur = conn.execute(
                """
                INSERT INTO intelligence_research_findings(
                    entity_id, provider, model, status,
                    evidence_note, raw_result, safe_to_apply, created_at
                ) VALUES (?, ?, ?, 'error', ?, '{}', 0, ?)
                """,
                (
                    entity["id"],
                    provider.name,
                    getattr(provider, "model", None),
                    str(exc)[:1000],
                    stamp,
                ),
            )
            conn.commit()
            results.append(
                {
                    "entity_id": entity["id"],
                    "name": entity["name"],
                    "finding_id": cur.lastrowid,
                    "status": "error",
                    "error": str(exc),
                    "safe_to_apply": False,
                }
            )

    return results


def apply_safe_findings(conn, provider_name=None):
    ensure_research_schema(conn)

    where = [
        "f.status = 'resolved'",
        "f.safe_to_apply = 1",
        "f.applied_at IS NULL",
    ]
    params = []

    if provider_name:
        where.append("f.provider = ?")
        params.append(provider_name)

    rows = conn.execute(
        f"""
        SELECT f.*
          FROM intelligence_research_findings f
         WHERE {' AND '.join(where)}
         ORDER BY f.id
        """,
        params,
    ).fetchall()

    applied = []
    skipped = []

    for row in rows:
        finding_row = dict(row)
        entity = _get_entity(conn, finding_row["entity_id"])
        if not entity:
            skipped.append(finding_row["id"])
            continue

        finding = ResearchFinding(
            status=finding_row["status"],
            canonical_name=finding_row["canonical_name"],
            canonical_url=finding_row["canonical_url"],
            entity_type=finding_row["resolved_type"],
            primary_source_url=finding_row["primary_source_url"],
            aliases=json.loads(finding_row["aliases"] or "[]"),
            evidence_note=finding_row["evidence_note"],
        ).normalized()

        # Revalider depuis les donnees stockees juste avant mutation.
        if not finding_is_safe(entity, finding):
            skipped.append(finding_row["id"])
            continue

        primary_source_id = intelligence.upsert_source(
            conn,
            source_url=finding.primary_source_url,
            platform="web",
            source_type="primary_identity_source",
            title=f"{finding.canonical_name} - official source",
            metadata={
                "research_provider": finding_row["provider"],
                "research_finding_id": finding_row["id"],
            },
        )

        resolved_id = identity.resolve_entity(
            conn,
            entity_id=entity["id"],
            canonical_key=f"url:{finding.canonical_url}",
            canonical_name=finding.canonical_name,
            canonical_url=finding.canonical_url,
            entity_type=finding.entity_type or "solution",
            aliases=finding.aliases or [],
            evidence_source_id=primary_source_id,
            metadata={
                "resolved_via": "external_primary_source_research",
                "research_provider": finding_row["provider"],
            },
        )

        conn.execute(
            """
            UPDATE intelligence_research_findings
               SET applied_at = ?
             WHERE id = ?
            """,
            (intelligence.now(), finding_row["id"]),
        )
        conn.commit()

        applied.append(
            {
                "finding_id": finding_row["id"],
                "from_entity_id": entity["id"],
                "resolved_entity_id": resolved_id,
                "canonical_name": finding.canonical_name,
                "canonical_url": finding.canonical_url,
            }
        )

    return {
        "applied": len(applied),
        "skipped": len(skipped),
        "items": applied,
    }


def findings_summary(conn, provider_name=None):
    ensure_research_schema(conn)

    where = ""
    params = ()
    if provider_name:
        where = "WHERE provider = ?"
        params = (provider_name,)

    counts = {
        row[0]: row[1]
        for row in conn.execute(
            f"""
            SELECT status, COUNT(*)
              FROM intelligence_research_findings
              {where}
             GROUP BY status
            """,
            params,
        ).fetchall()
    }

    safe = conn.execute(
        f"""
        SELECT COUNT(*)
          FROM intelligence_research_findings
          {where + (' AND ' if where else 'WHERE ')} safe_to_apply = 1
            AND applied_at IS NULL
        """,
        params,
    ).fetchone()[0]

    return {
        "by_status": counts,
        "safe_pending_apply": safe,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Research worker externe Life Intelligence V3"
    )
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--provider",
        default=os.environ.get("VAULT_RESEARCH_PROVIDER", "mistral"),
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--research", action="store_true")
    action.add_argument("--apply-safe", action="store_true")
    action.add_argument("--summary", action="store_true")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    conn = intelligence.connect(args.db)
    try:
        if args.research:
            provider = provider_from_name(args.provider)
            result = research_queue_items(conn, provider, limit=args.limit)
        elif args.apply_safe:
            result = apply_safe_findings(conn, provider_name=args.provider)
        else:
            result = findings_summary(conn, provider_name=args.provider)

        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
