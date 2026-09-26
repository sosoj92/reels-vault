#!/usr/bin/env python3
"""
vault_analysis.py - Analyse semantique des sources pour Life Intelligence V3.

Cette couche transforme une source deja ingeree en intelligence exploitable :
- un resume source-level ;
- des claims ATTRIBUES a la source, jamais marques "verified" ici ;
- des relations explicites entre des entites deja liees a cette source.

Regles de securite epistemique :
- aucune connaissance externe dans cette etape ;
- aucune creation d'entite ;
- aucun ID d'entite absent de la source ne peut etre ecrit ;
- chaque claim extrait reste verification_status='asserted' ;
- les relations utilisent un vocabulaire controle ;
- un fingerprint rend l'analyse idempotente tant que la source ne change pas.

Providers :
- Ollama/Qwen local = moteur par defaut, sans cout API ;
- Gemini = fallback cloud optionnel si GEMINI_API_KEY est configuree ;
- Mistral = provider explicite uniquement.

Le coeur reste provider-agnostic et aucun fallback payant n'est silencieux.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

import vault_intelligence as intelligence

ANALYSIS_SCHEMA_VERSION = 2
ANALYSIS_LOGIC_VERSION = "v3-grounded-claims-and-relations"

ALLOWED_RELATION_PREDICATES = {
    "uses",
    "integrates_with",
    "alternative_to",
    "compares_with",
    "depends_on",
    "part_of",
    "created_by",
    "recommended_with",
    "supports",
    "replaces",
    "connects_to",
}

ALLOWED_CLAIM_TYPES = {
    "fact",
    "capability",
    "recommendation",
    "comparison",
    "pricing",
    "requirement",
    "limitation",
    "other",
}

ANALYSIS_SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_source_analysis (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id          INTEGER NOT NULL REFERENCES intelligence_sources(id) ON DELETE CASCADE,
    source_fingerprint TEXT NOT NULL,
    provider           TEXT NOT NULL,
    model              TEXT,
    summary            TEXT,
    raw_result         TEXT NOT NULL DEFAULT '{}',
    rejected_items     TEXT NOT NULL DEFAULT '[]',
    superseded_at      TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE (source_id, source_fingerprint, provider, model)
);

CREATE TABLE IF NOT EXISTS intelligence_analysis_claim_links (
    analysis_id INTEGER NOT NULL REFERENCES intelligence_source_analysis(id) ON DELETE CASCADE,
    claim_id    INTEGER NOT NULL REFERENCES intelligence_claims(id) ON DELETE CASCADE,
    PRIMARY KEY (analysis_id, claim_id)
);

CREATE TABLE IF NOT EXISTS intelligence_analysis_relation_links (
    analysis_id INTEGER NOT NULL REFERENCES intelligence_source_analysis(id) ON DELETE CASCADE,
    relation_id INTEGER NOT NULL REFERENCES intelligence_relations(id) ON DELETE CASCADE,
    PRIMARY KEY (analysis_id, relation_id)
);

CREATE INDEX IF NOT EXISTS idx_source_analysis_source
    ON intelligence_source_analysis(source_id);
"""


class AnalysisError(RuntimeError):
    pass


class AnalysisRateLimitError(AnalysisError):
    pass


@dataclass
class AnalysisResult:
    summary: str | None
    claims: list[dict]
    relations: list[dict]


class AnalysisProvider(Protocol):
    name: str
    model: str | None

    def analyze(self, payload: dict) -> AnalysisResult:
        ...


def ensure_analysis_schema(conn):
    intelligence.ensure_schema(conn)
    conn.executescript(ANALYSIS_SCHEMA)

    columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(intelligence_source_analysis)"
        ).fetchall()
    }
    if "superseded_at" not in columns:
        conn.execute(
            "ALTER TABLE intelligence_source_analysis "
            "ADD COLUMN superseded_at TEXT"
        )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('analysis_schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(ANALYSIS_SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _effective_description(content):
    return (
        content.get("description_manual")
        or content.get("description_ai")
        or content.get("description_original")
        or ""
    ).strip()


def _clean_text(value, max_len=None):
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if max_len is not None:
        value = value[:max_len]
    return value


def source_payload(conn, source_id, max_chars=None):
    ensure_analysis_schema(conn)

    source_row = conn.execute(
        "SELECT * FROM intelligence_sources WHERE id = ?",
        (source_id,),
    ).fetchone()
    if not source_row:
        raise AnalysisError(f"source {source_id} not found")

    source = dict(source_row)
    content_id = source.get("content_id")
    if content_id is None:
        raise AnalysisError(
            f"source {source_id} has no V2 content_id; semantic analysis requires source text"
        )
    if not _table_exists(conn, "contents"):
        raise AnalysisError("V2 contents table not found")

    content_row = conn.execute(
        "SELECT * FROM contents WHERE id = ?",
        (content_id,),
    ).fetchone()
    if not content_row:
        raise AnalysisError(f"V2 content {content_id} not found")
    content = dict(content_row)

    entities = [
        {
            "id": row["id"],
            "name": row["name"],
            "entity_type": row["entity_type"],
            "canonical_key": row["canonical_key"],
        }
        for row in conn.execute(
            """
            SELECT DISTINCT e.id, e.name, e.entity_type, e.canonical_key
              FROM intelligence_entities e
              JOIN intelligence_entity_sources es ON es.entity_id = e.id
             WHERE es.source_id = ?
               AND es.role <> 'primary'
             ORDER BY e.id
            """,
            (source_id,),
        ).fetchall()
    ]

    title = _clean_text(content.get("title")) or _clean_text(source.get("title"))
    description = _effective_description(content)
    transcript = _clean_text(content.get("transcript")) or ""

    if max_chars is None:
        max_chars = int(os.environ.get("VAULT_ANALYSIS_MAX_CHARS", "30000"))

    # Le titre et la description sont petits et prioritaires.
    # Le transcript prend le budget restant, sans jamais alterer la V2.
    fixed = len(title or "") + len(description or "")
    transcript_budget = max(0, max_chars - fixed)
    transcript = transcript[:transcript_budget]

    if not (title or description or transcript):
        raise AnalysisError(f"source {source_id} has no analyzable text")

    return {
        "source_id": source_id,
        "content_id": content_id,
        "source_url": source.get("source_url"),
        "platform": source.get("platform"),
        "title": title,
        "description": description,
        "transcript": transcript,
        "entities": entities,
    }


def source_fingerprint(payload):
    stable = {
        "analysis_logic_version": ANALYSIS_LOGIC_VERSION,
        "source_id": payload["source_id"],
        "title": payload.get("title"),
        "description": payload.get("description"),
        "transcript": payload.get("transcript"),
        "entities": [
            {
                "id": entity["id"],
                "name": entity["name"],
                "entity_type": entity["entity_type"],
                "canonical_key": entity["canonical_key"],
            }
            for entity in payload.get("entities", [])
        ],
    }
    raw = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_analysis_prompt(payload):
    entity_catalog = json.dumps(
        payload.get("entities", []),
        ensure_ascii=False,
        indent=2,
    )

    return f"""
Analyze ONLY the source material below. Do not use external knowledge.

SOURCE
Title: {payload.get('title') or ''}
Description:
{payload.get('description') or ''}

Transcript:
{payload.get('transcript') or ''}

ENTITIES ALREADY LINKED TO THIS SOURCE
{entity_catalog}

Return exactly one JSON object:
{{
  "summary": "2-5 sentence concrete summary" | null,
  "claims": [
    {{
      "entity_id": integer | null,
      "claim_type": "fact" | "capability" | "recommendation" | "comparison" |
                    "pricing" | "requirement" | "limitation" | "other",
      "claim_text": "short factual paraphrase of what THIS SOURCE says",
      "evidence": "short excerpt from the supplied source material"
    }}
  ],
  "relations": [
    {{
      "subject_entity_id": integer,
      "predicate": "uses" | "integrates_with" | "alternative_to" |
                   "compares_with" | "depends_on" | "part_of" | "created_by" |
                   "recommended_with" | "supports" | "replaces" | "connects_to",
      "object_entity_id": integer,
      "evidence": "short excerpt from the supplied source material"
    }}
  ]
}}

Rules:
- Claims are source assertions, NOT verified facts.
- Extract only claims explicitly supported by the supplied text.
- Preserve modality and uncertainty exactly. Never strengthen "aims to", "may",
  "might", "likely", "probably", "approximately", "~", "appears", "suggests",
  "vise à", "peut", "pourrait", "probablement", "environ", or similar wording
  into a certain statement.
- Never convert a descriptive attribute into a requirement. Example: "the repo
  has 230 stars" does NOT mean "the skill requires a repo with 230 stars".
- Never introduce "requires", "must", "guarantees", "ensures", "always",
  "never", or equivalent certainty unless the supplied evidence explicitly
  supports that strength.
- entity_id may be null for a general claim not attributable to one listed entity.
- If entity_id is not null, it MUST be one of the listed entity IDs.
- Relations may ONLY connect two listed entity IDs.
- Never create or infer a new entity, company, person, product, URL, repository, or ID.
- Do not create a relation just because two entities appear in the same source.
- A relation means the SUBJECT itself has that relationship to the OBJECT.
- IMPORTANT: if a directory/platform X lists alternatives to product Y, that does
  NOT mean X is alternative_to Y.
- Example: "OpenAlternative lists alternatives to Notion" must NOT produce
  OpenAlternative alternative_to Notion.
- Only produce "Memos alternative_to Notion" if the supplied source explicitly
  supports that Memos itself is an alternative to Notion.
- Relation evidence MUST explicitly name BOTH the subject entity and object entity.
  If you cannot quote evidence naming both, omit the relation.
- Keep evidence short and directly grounded in the supplied text.
- If there is no supported claim or relation, return an empty array.
- Output JSON only.
""".strip()


def _parse_result_object(data):
    if not isinstance(data, dict):
        raise AnalysisError("analysis response must be a JSON object")

    claims = data.get("claims") or []
    relations = data.get("relations") or []

    if not isinstance(claims, list) or not isinstance(relations, list):
        raise AnalysisError("claims and relations must be arrays")

    return AnalysisResult(
        summary=_clean_text(data.get("summary")),
        claims=claims,
        relations=relations,
    )


def _fold_for_evidence(value):
    value = unicodedata.normalize("NFD", value or "")
    value = "".join(
        char for char in value
        if unicodedata.category(char) != "Mn"
    ).lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _evidence_names_both_entities(evidence, subject_name, object_name):
    folded_evidence = _fold_for_evidence(evidence)
    if not folded_evidence:
        return False
    subject = _fold_for_evidence(subject_name)
    object_value = _fold_for_evidence(object_name)
    return bool(
        subject
        and object_value
        and subject in folded_evidence
        and object_value in folded_evidence
    )


UNCERTAINTY_CUES = (
    "~",
    "approximately",
    "approx",
    "about",
    "around",
    "aim",
    "aims",
    "target",
    "targets",
    "likely",
    "probably",
    "possibly",
    "may",
    "might",
    "could",
    "appears",
    "seems",
    "suggests",
    "environ",
    "approximativement",
    "vise",
    "probablement",
    "peut",
    "pourrait",
    "semble",
)

STRONG_CLAIM_CUE_GROUPS = {
    "requirement": (
        ("requires", "required", "must", "needs to", "need to", "necessary"),
        ("require", "required", "must", "need", "necess", "requis", "doit", "oblig", "demande", "sans", "without"),
    ),
    "guarantee": (
        ("guarantees", "guarantee", "ensures", "ensure", "always", "never"),
        ("guarantee", "ensure", "always", "never", "garant", "toujours", "jamais"),
    ),
}


def _contains_any(text, cues):
    folded = _fold_for_evidence(text)
    return any(_fold_for_evidence(cue) in folded for cue in cues)


def _claim_preserves_evidence_strength(claim_text, evidence):
    if not evidence:
        return False, "missing_evidence"

    evidence_uncertain = _contains_any(evidence, UNCERTAINTY_CUES)
    claim_uncertain = _contains_any(claim_text, UNCERTAINTY_CUES)
    if evidence_uncertain and not claim_uncertain:
        return False, "uncertainty_strengthened"

    for label, (claim_cues, evidence_cues) in STRONG_CLAIM_CUE_GROUPS.items():
        if _contains_any(claim_text, claim_cues) and not _contains_any(
            evidence,
            evidence_cues,
        ):
            return False, f"unsupported_{label}"

    return True, None


def sanitize_result(payload, result):
    """
    Filtre tout item qui depasse le perimetre de la source.
    Les items rejetes sont conserves dans le journal d'analyse, mais jamais ecrits.
    """
    entity_by_id = {
        entity["id"]: entity
        for entity in payload.get("entities", [])
    }
    allowed_ids = set(entity_by_id)
    claims = []
    relations = []
    rejected = []

    for item in result.claims:
        if not isinstance(item, dict):
            rejected.append({"kind": "claim", "reason": "not_object", "item": item})
            continue

        entity_id = item.get("entity_id")
        if entity_id is not None and entity_id not in allowed_ids:
            rejected.append(
                {
                    "kind": "claim",
                    "reason": "entity_not_linked_to_source",
                    "item": item,
                }
            )
            continue

        claim_type = _clean_text(item.get("claim_type")) or "other"
        if claim_type not in ALLOWED_CLAIM_TYPES:
            claim_type = "other"

        claim_text = _clean_text(item.get("claim_text"), max_len=1200)
        evidence = _clean_text(item.get("evidence"), max_len=500)
        if not claim_text:
            rejected.append(
                {"kind": "claim", "reason": "missing_claim_text", "item": item}
            )
            continue

        grounded, grounding_reason = _claim_preserves_evidence_strength(
            claim_text,
            evidence,
        )
        if not grounded:
            rejected.append(
                {
                    "kind": "claim",
                    "reason": grounding_reason,
                    "item": item,
                }
            )
            continue

        claims.append(
            {
                "entity_id": entity_id,
                "claim_type": claim_type,
                "claim_text": claim_text,
                "evidence": evidence,
            }
        )

    for item in result.relations:
        if not isinstance(item, dict):
            rejected.append(
                {"kind": "relation", "reason": "not_object", "item": item}
            )
            continue

        subject = item.get("subject_entity_id")
        object_id = item.get("object_entity_id")
        predicate = _clean_text(item.get("predicate"))

        if subject not in allowed_ids or object_id not in allowed_ids:
            rejected.append(
                {
                    "kind": "relation",
                    "reason": "entity_not_linked_to_source",
                    "item": item,
                }
            )
            continue
        if subject == object_id:
            rejected.append(
                {"kind": "relation", "reason": "self_relation", "item": item}
            )
            continue
        if predicate not in ALLOWED_RELATION_PREDICATES:
            rejected.append(
                {
                    "kind": "relation",
                    "reason": "predicate_not_allowed",
                    "item": item,
                }
            )
            continue

        evidence = _clean_text(item.get("evidence"), max_len=500)
        subject_name = entity_by_id[subject]["name"]
        object_name = entity_by_id[object_id]["name"]
        if not _evidence_names_both_entities(
            evidence,
            subject_name,
            object_name,
        ):
            rejected.append(
                {
                    "kind": "relation",
                    "reason": "evidence_does_not_name_both_entities",
                    "item": item,
                }
            )
            continue

        relations.append(
            {
                "subject_entity_id": subject,
                "predicate": predicate,
                "object_entity_id": object_id,
                "evidence": evidence,
            }
        )

    return AnalysisResult(
        summary=_clean_text(result.summary, max_len=3000),
        claims=claims,
        relations=relations,
    ), rejected


class OllamaAnalysisProvider:
    """
    Analyse locale via l'API Ollama.
    Aucun secret n'est necessaire pour l'API locale.
    """

    name = "ollama"

    def __init__(self, model=None, base_url=None, timeout=None):
        self.model = model or os.environ.get(
            "OLLAMA_ANALYSIS_MODEL",
            "qwen3:4b",
        )
        self.base_url = (
            base_url
            or os.environ.get("OLLAMA_BASE_URL")
            or "http://127.0.0.1:11434"
        ).rstrip("/")
        self.timeout = (
            float(timeout)
            if timeout is not None
            else float(os.environ.get("OLLAMA_ANALYSIS_TIMEOUT", "600"))
        )
        self.max_chars = int(
            os.environ.get("OLLAMA_ANALYSIS_MAX_CHARS", "8000")
        )

    def analyze(self, payload):
        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You extract structured knowledge from supplied source text. "
                        "Never use outside knowledge. Return JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": build_analysis_prompt(payload),
                },
            ],
            "format": "json",
            "stream": False,
            "think": False,
            "keep_alive": "10m",
            "options": {
                "temperature": 0,
            },
        }

        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise AnalysisError(
                f"Ollama HTTP {exc.code}: {body_text[:500]}"
            ) from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            if isinstance(exc, TimeoutError):
                raise AnalysisError(
                    "Ollama timed out while analyzing this source. "
                    f"Model={self.model!r}, timeout={self.timeout}s, "
                    f"max_chars={self.max_chars}."
                ) from exc
            raise AnalysisError(
                "Ollama local is unavailable. Start Ollama and make sure "
                f"model {self.model!r} is installed."
            ) from exc

        try:
            response = json.loads(raw)
            content = response["message"]["content"]
            data = json.loads(content)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise AnalysisError("invalid Ollama JSON response") from exc

        return _parse_result_object(data)


class GeminiAnalysisProvider:
    """
    Fallback cloud optionnel. Il n'est utilise que si GEMINI_API_KEY existe
    ou si --provider gemini est demande explicitement.
    """

    name = "gemini"

    def __init__(self, api_key=None, model=None, timeout=90):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model or os.environ.get(
            "GEMINI_ANALYSIS_MODEL",
            "gemini-2.5-flash-lite",
        )
        self.timeout = timeout
        self.max_retries = int(os.environ.get("GEMINI_ANALYSIS_MAX_RETRIES", "3"))
        self.backoff_seconds = float(
            os.environ.get("GEMINI_ANALYSIS_BACKOFF_SECONDS", "1")
        )

        if not self.api_key:
            raise AnalysisError(
                "GEMINI_API_KEY is not set. Configure it only if you want "
                "Gemini as a cloud fallback."
            )

    def analyze(self, payload):
        body = {
            "system_instruction": {
                "parts": [
                    {
                        "text": (
                            "You extract structured knowledge from supplied source text. "
                            "Never use outside knowledge. Return JSON only."
                        )
                    }
                ]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": build_analysis_prompt(payload),
                        }
                    ],
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0,
            },
        }

        model_path = urllib.parse.quote(self.model, safe="-_.")
        request = urllib.request.Request(
            (
                "https://generativelanguage.googleapis.com/v1beta/"
                f"models/{model_path}:generateContent"
            ),
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "x-goog-api-key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        raw = None
        transient_codes = {429, 500, 502, 503, 504}
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout,
                ) as response:
                    raw = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                body_text = exc.read().decode("utf-8", errors="replace")
                if exc.code in transient_codes and attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * (2 ** attempt))
                    continue
                message = f"Gemini HTTP {exc.code}: {body_text[:500]}"
                if exc.code == 429:
                    raise AnalysisRateLimitError(message) from exc
                raise AnalysisError(message) from exc
            except urllib.error.URLError as exc:
                if attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * (2 ** attempt))
                    continue
                raise AnalysisError(f"Gemini network error: {exc}") from exc

        if raw is None:
            raise AnalysisError("Gemini request ended without a response")

        try:
            response = json.loads(raw)
            parts = response["candidates"][0]["content"]["parts"]
            content = "".join(
                part.get("text", "")
                for part in parts
                if isinstance(part, dict)
            )
            data = json.loads(content)
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise AnalysisError("invalid Gemini JSON response") from exc

        return _parse_result_object(data)


class AutoAnalysisProvider:
    """
    Routeur economique :
    1) Ollama/Qwen local ;
    2) Gemini uniquement si une cle est configuree ;
    3) jamais de fallback Mistral implicite.

    Le provider/model exposes apres analyse correspondent au moteur reel,
    ce qui preserve correctement le cache et la provenance.
    """

    name = "auto"

    def __init__(self, providers=None):
        if providers is not None:
            self.providers = list(providers)
        else:
            self.providers = [OllamaAnalysisProvider()]
            if os.environ.get("GEMINI_API_KEY"):
                self.providers.append(GeminiAnalysisProvider())
        self.model = "cost-router"
        self.max_chars = min(
            getattr(provider, "max_chars", 30000)
            for provider in self.providers
        ) if self.providers else 8000
        self.last_provider = None
        self.last_model = None

    def analyze(self, payload):
        errors = []
        for provider in self.providers:
            try:
                result = provider.analyze(payload)
                self.last_provider = provider.name
                self.last_model = provider.model
                return result
            except AnalysisRateLimitError as exc:
                errors.append(f"{provider.name}: {exc}")
                continue
            except AnalysisError as exc:
                errors.append(f"{provider.name}: {exc}")
                continue

        raise AnalysisError(
            "No low-cost analysis provider is available. "
            + " | ".join(errors)
        )


class MistralChatAnalysisProvider:
    name = "mistral"

    def __init__(self, api_key=None, model=None, timeout=90):
        self.api_key = api_key or os.environ.get("MISTRAL_API_KEY")
        self.model = model or os.environ.get(
            "MISTRAL_ANALYSIS_MODEL",
            "mistral-medium-latest",
        )
        self.timeout = timeout
        self.max_retries = int(os.environ.get("MISTRAL_ANALYSIS_MAX_RETRIES", "4"))
        self.backoff_seconds = float(
            os.environ.get("MISTRAL_ANALYSIS_BACKOFF_SECONDS", "1")
        )
        self.max_backoff_seconds = float(
            os.environ.get("MISTRAL_ANALYSIS_MAX_BACKOFF_SECONDS", "20")
        )

        if not self.api_key:
            raise AnalysisError(
                "MISTRAL_API_KEY is not set. Do not paste the key into the Vault."
            )

    def _retry_after(self, exc, attempt):
        try:
            header = exc.headers.get("Retry-After")
        except Exception:
            header = None

        if header:
            try:
                return max(0.0, float(header))
            except (TypeError, ValueError):
                pass

        return min(
            self.max_backoff_seconds,
            self.backoff_seconds * (2 ** attempt),
        )

    def analyze(self, payload):
        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You extract structured knowledge from supplied source text. "
                        "Never use outside knowledge. Return JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": build_analysis_prompt(payload),
                },
            ],
            "response_format": {"type": "json_object"},
        }

        request = urllib.request.Request(
            "https://api.mistral.ai/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        transient_codes = {429, 500, 502, 503, 504}
        raw = None

        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout,
                ) as response:
                    raw = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                body_text = exc.read().decode("utf-8", errors="replace")
                if exc.code in transient_codes and attempt < self.max_retries:
                    time.sleep(self._retry_after(exc, attempt))
                    continue
                message = f"Mistral HTTP {exc.code}: {body_text[:500]}"
                if exc.code == 429:
                    raise AnalysisRateLimitError(message) from exc
                raise AnalysisError(message) from exc
            except urllib.error.URLError as exc:
                if attempt < self.max_retries:
                    time.sleep(self._retry_after(exc, attempt))
                    continue
                raise AnalysisError(f"Mistral network error: {exc}") from exc

        if raw is None:
            raise AnalysisError("Mistral request ended without a response")

        try:
            response = json.loads(raw)
            content = response["choices"][0]["message"]["content"]
            data = json.loads(content)
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise AnalysisError("invalid Mistral JSON-mode response") from exc

        return _parse_result_object(data)


def provider_from_name(name):
    name = (name or "").strip().lower()
    if name == "auto":
        return AutoAnalysisProvider()
    if name in ("ollama", "qwen", "local"):
        return OllamaAnalysisProvider()
    if name == "gemini":
        return GeminiAnalysisProvider()
    if name == "mistral":
        return MistralChatAnalysisProvider()
    raise AnalysisError(f"unsupported analysis provider: {name!r}")


def _existing_analysis(conn, source_id, fingerprint, provider):
    row = conn.execute(
        """
        SELECT *
          FROM intelligence_source_analysis
         WHERE source_id = ?
           AND source_fingerprint = ?
           AND provider = ?
           AND COALESCE(model, '') = COALESCE(?, '')
        """,
        (
            source_id,
            fingerprint,
            provider.name,
            getattr(provider, "model", None),
        ),
    ).fetchone()
    return dict(row) if row else None


def _execution_provider_name(provider):
    return getattr(provider, "last_provider", None) or provider.name


def _execution_model_name(provider):
    return getattr(provider, "last_model", None) or getattr(provider, "model", None)


def _existing_claim_id(conn, source_id, entity_id, claim_text):
    if entity_id is None:
        row = conn.execute(
            """
            SELECT id
              FROM intelligence_claims
             WHERE source_id = ?
               AND entity_id IS NULL
               AND claim_text = ?
             ORDER BY id
             LIMIT 1
            """,
            (source_id, claim_text),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id
              FROM intelligence_claims
             WHERE source_id = ?
               AND entity_id = ?
               AND claim_text = ?
             ORDER BY id
             LIMIT 1
            """,
            (source_id, entity_id, claim_text),
        ).fetchone()
    return row[0] if row else None


def _insert_claim(conn, source_id, item, analysis_id, provider):
    existing = _existing_claim_id(
        conn,
        source_id,
        item["entity_id"],
        item["claim_text"],
    )
    if existing is not None:
        return existing

    stamp = intelligence.now()
    notes = json.dumps(
        {
            "extracted_by": _execution_provider_name(provider),
            "analysis_model": _execution_model_name(provider),
            "analysis_id": analysis_id,
            "claim_type": item["claim_type"],
            "evidence": item.get("evidence"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )

    cur = conn.execute(
        """
        INSERT INTO intelligence_claims(
            entity_id, source_id, claim_text, verification_status,
            verification_source_id, notes, verified_at, created_at, updated_at
        ) VALUES (?, ?, ?, 'asserted', NULL, ?, NULL, ?, ?)
        """,
        (
            item["entity_id"],
            source_id,
            item["claim_text"],
            notes,
            stamp,
            stamp,
        ),
    )
    return cur.lastrowid


def _insert_relation(conn, source_id, item, analysis_id, provider):
    notes = json.dumps(
        {
            "extracted_by": _execution_provider_name(provider),
            "analysis_model": _execution_model_name(provider),
            "analysis_id": analysis_id,
            "evidence": item.get("evidence"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO intelligence_relations(
            subject_entity_id, predicate, object_entity_id,
            source_id, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            item["subject_entity_id"],
            item["predicate"],
            item["object_entity_id"],
            source_id,
            notes,
            intelligence.now(),
        ),
    )

    row = conn.execute(
        """
        SELECT id
          FROM intelligence_relations
         WHERE subject_entity_id = ?
           AND predicate = ?
           AND object_entity_id = ?
           AND COALESCE(source_id, 0) = COALESCE(?, 0)
        """,
        (
            item["subject_entity_id"],
            item["predicate"],
            item["object_entity_id"],
            source_id,
        ),
    ).fetchone()
    return row[0]


def _supersede_previous_analysis_artifacts(
    conn,
    *,
    source_id,
    provider_name,
    model,
    keep_analysis_id,
):
    """
    Retire du graphe actif les artefacts automatiques des anciennes analyses
    de la meme source/provider/model, tout en conservant leur raw_result.
    """
    previous_ids = [
        row[0]
        for row in conn.execute(
            """
            SELECT id
              FROM intelligence_source_analysis
             WHERE source_id = ?
               AND provider = ?
               AND COALESCE(model, '') = COALESCE(?, '')
               AND id <> ?
               AND superseded_at IS NULL
            """,
            (source_id, provider_name, model, keep_analysis_id),
        ).fetchall()
    ]
    if not previous_ids:
        return

    placeholders = ",".join("?" for _ in previous_ids)

    claim_ids = [
        row[0]
        for row in conn.execute(
            f"""
            SELECT DISTINCT claim_id
              FROM intelligence_analysis_claim_links
             WHERE analysis_id IN ({placeholders})
            """,
            previous_ids,
        ).fetchall()
    ]
    relation_ids = [
        row[0]
        for row in conn.execute(
            f"""
            SELECT DISTINCT relation_id
              FROM intelligence_analysis_relation_links
             WHERE analysis_id IN ({placeholders})
            """,
            previous_ids,
        ).fetchall()
    ]

    conn.execute(
        f"""
        UPDATE intelligence_source_analysis
           SET superseded_at = ?
         WHERE id IN ({placeholders})
        """,
        [intelligence.now(), *previous_ids],
    )
    conn.execute(
        f"""
        DELETE FROM intelligence_analysis_claim_links
         WHERE analysis_id IN ({placeholders})
        """,
        previous_ids,
    )
    conn.execute(
        f"""
        DELETE FROM intelligence_analysis_relation_links
         WHERE analysis_id IN ({placeholders})
        """,
        previous_ids,
    )

    for claim_id in claim_ids:
        linked = conn.execute(
            """
            SELECT 1
              FROM intelligence_analysis_claim_links
             WHERE claim_id = ?
             LIMIT 1
            """,
            (claim_id,),
        ).fetchone()
        row = conn.execute(
            "SELECT notes FROM intelligence_claims WHERE id = ?",
            (claim_id,),
        ).fetchone()
        if (
            not linked
            and row
            and '"analysis_id"' in (row[0] or "")
        ):
            conn.execute(
                "DELETE FROM intelligence_claims WHERE id = ?",
                (claim_id,),
            )

    for relation_id in relation_ids:
        linked = conn.execute(
            """
            SELECT 1
              FROM intelligence_analysis_relation_links
             WHERE relation_id = ?
             LIMIT 1
            """,
            (relation_id,),
        ).fetchone()
        row = conn.execute(
            "SELECT notes FROM intelligence_relations WHERE id = ?",
            (relation_id,),
        ).fetchone()
        if (
            not linked
            and row
            and '"analysis_id"' in (row[0] or "")
        ):
            conn.execute(
                "DELETE FROM intelligence_relations WHERE id = ?",
                (relation_id,),
            )


def analyze_source(conn, source_id, provider):
    ensure_analysis_schema(conn)
    payload = source_payload(
        conn,
        source_id,
        max_chars=getattr(provider, "max_chars", None),
    )
    fingerprint = source_fingerprint(payload)

    existing = _existing_analysis(conn, source_id, fingerprint, provider)
    if existing:
        return {
            "source_id": source_id,
            "analysis_id": existing["id"],
            "status": "cached",
            "claims_written": 0,
            "relations_written": 0,
            "rejected_items": len(json.loads(existing["rejected_items"] or "[]")),
        }

    raw_result = provider.analyze(payload)
    clean_result, rejected = sanitize_result(payload, raw_result)

    try:
        conn.execute("BEGIN")
        cur = conn.execute(
            """
            INSERT INTO intelligence_source_analysis(
                source_id, source_fingerprint, provider, model,
                summary, raw_result, rejected_items, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                fingerprint,
                provider.name,
                getattr(provider, "model", None),
                clean_result.summary,
                json.dumps(
                    {
                        "summary": raw_result.summary,
                        "claims": raw_result.claims,
                        "relations": raw_result.relations,
                        "execution_provider": getattr(
                            provider,
                            "last_provider",
                            provider.name,
                        ),
                        "execution_model": getattr(
                            provider,
                            "last_model",
                            getattr(provider, "model", None),
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                json.dumps(rejected, ensure_ascii=False, sort_keys=True),
                intelligence.now(),
            ),
        )
        analysis_id = cur.lastrowid

        _supersede_previous_analysis_artifacts(
            conn,
            source_id=source_id,
            provider_name=provider.name,
            model=getattr(provider, "model", None),
            keep_analysis_id=analysis_id,
        )

        claim_ids = []
        for item in clean_result.claims:
            claim_id = _insert_claim(
                conn,
                source_id,
                item,
                analysis_id,
                provider,
            )
            claim_ids.append(claim_id)
            conn.execute(
                """
                INSERT OR IGNORE INTO intelligence_analysis_claim_links(
                    analysis_id, claim_id
                ) VALUES (?, ?)
                """,
                (analysis_id, claim_id),
            )

        relation_ids = []
        for item in clean_result.relations:
            relation_id = _insert_relation(
                conn,
                source_id,
                item,
                analysis_id,
                provider,
            )
            relation_ids.append(relation_id)
            conn.execute(
                """
                INSERT OR IGNORE INTO intelligence_analysis_relation_links(
                    analysis_id, relation_id
                ) VALUES (?, ?)
                """,
                (analysis_id, relation_id),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "source_id": source_id,
        "analysis_id": analysis_id,
        "status": "completed",
        "summary": clean_result.summary,
        "claims_written": len(claim_ids),
        "relations_written": len(relation_ids),
        "rejected_items": len(rejected),
    }


def analysis_details(conn, analysis_id):
    """Retourne une vue lisible d'une analyse, avec noms d'entites et preuves."""
    ensure_analysis_schema(conn)

    row = conn.execute(
        "SELECT * FROM intelligence_source_analysis WHERE id = ?",
        (analysis_id,),
    ).fetchone()
    if not row:
        raise AnalysisError(f"analysis {analysis_id} not found")

    analysis_row = dict(row)
    raw = json.loads(analysis_row["raw_result"] or "{}")

    claims = []
    for claim_row in conn.execute(
        """
        SELECT c.*, e.name AS entity_name
          FROM intelligence_analysis_claim_links l
          JOIN intelligence_claims c ON c.id = l.claim_id
          LEFT JOIN intelligence_entities e ON e.id = c.entity_id
         WHERE l.analysis_id = ?
         ORDER BY c.id
        """,
        (analysis_id,),
    ).fetchall():
        claim = dict(claim_row)
        notes = json.loads(claim.get("notes") or "{}")
        claims.append(
            {
                "id": claim["id"],
                "entity_id": claim["entity_id"],
                "entity_name": claim.get("entity_name"),
                "claim_type": notes.get("claim_type"),
                "claim_text": claim["claim_text"],
                "evidence": notes.get("evidence"),
                "verification_status": claim["verification_status"],
            }
        )

    relations = []
    for relation_row in conn.execute(
        """
        SELECT r.*,
               s.name AS subject_name,
               o.name AS object_name
          FROM intelligence_analysis_relation_links l
          JOIN intelligence_relations r ON r.id = l.relation_id
          JOIN intelligence_entities s ON s.id = r.subject_entity_id
          JOIN intelligence_entities o ON o.id = r.object_entity_id
         WHERE l.analysis_id = ?
         ORDER BY r.id
        """,
        (analysis_id,),
    ).fetchall():
        relation = dict(relation_row)
        notes = json.loads(relation.get("notes") or "{}")
        relations.append(
            {
                "id": relation["id"],
                "subject_entity_id": relation["subject_entity_id"],
                "subject_name": relation["subject_name"],
                "predicate": relation["predicate"],
                "object_entity_id": relation["object_entity_id"],
                "object_name": relation["object_name"],
                "evidence": notes.get("evidence"),
            }
        )

    return {
        "analysis_id": analysis_id,
        "source_id": analysis_row["source_id"],
        "provider": analysis_row["provider"],
        "model": analysis_row["model"],
        "execution_provider": raw.get(
            "execution_provider",
            analysis_row["provider"],
        ),
        "execution_model": raw.get(
            "execution_model",
            analysis_row["model"],
        ),
        "summary": analysis_row["summary"],
        "claims": claims,
        "relations": relations,
        "rejected_items": json.loads(analysis_row["rejected_items"] or "[]"),
    }


def _candidate_source_ids(conn, provider, limit):
    ensure_analysis_schema(conn)

    rows = conn.execute(
        """
        SELECT s.id
          FROM intelligence_sources s
          JOIN contents c ON c.id = s.content_id
         WHERE s.content_id IS NOT NULL
           AND (
                NULLIF(TRIM(COALESCE(c.title, '')), '') IS NOT NULL
                OR NULLIF(TRIM(COALESCE(c.description_manual, '')), '') IS NOT NULL
                OR NULLIF(TRIM(COALESCE(c.description_ai, '')), '') IS NOT NULL
                OR NULLIF(TRIM(COALESCE(c.description_original, '')), '') IS NOT NULL
                OR NULLIF(TRIM(COALESCE(c.transcript, '')), '') IS NOT NULL
           )
         ORDER BY s.id
        """
    ).fetchall()

    selected = []
    for row in rows:
        source_id = row[0]
        payload = source_payload(
            conn,
            source_id,
            max_chars=getattr(provider, "max_chars", None),
        )
        fingerprint = source_fingerprint(payload)
        if _existing_analysis(conn, source_id, fingerprint, provider):
            continue
        selected.append(source_id)
        if len(selected) >= limit:
            break

    return selected


def analyze_next(conn, provider, limit=1, progress=False):
    results = []
    source_ids = _candidate_source_ids(conn, provider, limit)
    total = len(source_ids)
    for index, source_id in enumerate(source_ids, start=1):
        started = time.monotonic()
        if progress:
            print(
                f"[{index}/{total}] Source {source_id} : analyse en cours...",
                file=sys.stderr,
                flush=True,
            )
        try:
            result = analyze_source(conn, source_id, provider)
            results.append(result)
            if progress:
                elapsed = time.monotonic() - started
                print(
                    f"[{index}/{total}] Source {source_id} : "
                    f"{result.get('status')} en {elapsed:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )
        except AnalysisRateLimitError as exc:
            if progress:
                elapsed = time.monotonic() - started
                print(
                    f"[{index}/{total}] Source {source_id} : erreur en "
                    f"{elapsed:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )
            results.append(
                {
                    "source_id": source_id,
                    "status": "error",
                    "error": str(exc),
                }
            )
            break
        except Exception as exc:
            if progress:
                elapsed = time.monotonic() - started
                print(
                    f"[{index}/{total}] Source {source_id} : erreur en "
                    f"{elapsed:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )
            results.append(
                {
                    "source_id": source_id,
                    "status": "error",
                    "error": str(exc),
                }
            )
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Analyse semantique Life Intelligence V3"
    )
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--provider",
        default=os.environ.get("VAULT_ANALYSIS_PROVIDER", "auto"),
    )

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--source-id", type=int)
    action.add_argument("--next", action="store_true")
    action.add_argument("--show-analysis", type=int)

    parser.add_argument("--limit", type=int, default=1)
    args = parser.parse_args()

    conn = intelligence.connect(args.db)
    try:
        if args.show_analysis is not None:
            result = analysis_details(conn, args.show_analysis)
        else:
            provider = provider_from_name(args.provider)
            if args.source_id is not None:
                result = analyze_source(conn, args.source_id, provider)
            else:
                result = analyze_next(
                    conn,
                    provider,
                    limit=args.limit,
                    progress=True,
                )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
