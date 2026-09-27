#!/usr/bin/env python3
"""
vault_intelligence_context.py - Paquet de contexte fiable pour l'Assistant IA.

Cette couche est strictement en lecture. Elle transforme la memoire V3 en un
contexte borne et auditable, sans appel LLM ni embeddings.

Principes :
- rechercher dans les entites, alias, summaries, claims, sources et relations ;
- privilegier la pertinence sans masquer le statut epistemique ;
- ne jamais promouvoir un claim asserted en verified ;
- ne jamais exposer raw_result comme connaissance active ;
- renvoyer un paquet compact pour limiter le cout des assistants en aval.
"""

import re
import unicodedata

import vault_intelligence as intelligence
import vault_intelligence_query as query_layer


class IntelligenceContextError(ValueError):
    pass


_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "dans", "avec",
    "pour", "les", "des", "une", "sur", "qui", "que", "est", "sont", "aux",
    "par", "plus", "comme", "comment", "quoi", "quel", "quelle", "quels",
    "quelles", "mon", "mes", "ton", "tes", "son", "ses",
}


def _fold(value):
    value = unicodedata.normalize("NFD", str(value or ""))
    value = "".join(
        char for char in value
        if unicodedata.category(char) != "Mn"
    ).lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _query_parts(value):
    text = (value or "").strip()
    if not text:
        raise IntelligenceContextError("q is required")
    if len(text) > 500:
        raise IntelligenceContextError("q must be 500 characters or fewer")

    folded = _fold(text)
    tokens = [
        token for token in folded.split()
        if len(token) >= 2 and token not in _STOPWORDS
    ]
    # Conserver l'ordre, retirer les doublons.
    seen = set()
    tokens = [
        token for token in tokens
        if not (token in seen or seen.add(token))
    ]
    return folded, tokens


def _contains(text, needle):
    return bool(needle and needle in _fold(text))


def _token_hits(text, tokens):
    folded = _fold(text)
    return sum(1 for token in tokens if token in folded)


def _entity_search_data(conn):
    entities = {
        row["id"]: {
            "row": dict(row),
            "aliases": [],
            "claims": [],
            "sources": [],
            "relations": [],
        }
        for row in conn.execute(
            "SELECT * FROM intelligence_entities ORDER BY id"
        ).fetchall()
    }

    for row in conn.execute(
        "SELECT entity_id, alias FROM intelligence_entity_aliases"
    ).fetchall():
        if row["entity_id"] in entities:
            entities[row["entity_id"]]["aliases"].append(row["alias"])

    for row in conn.execute(
        """
        SELECT
            c.entity_id,
            c.claim_text,
            c.verification_status,
            c.source_id,
            s.source_url,
            s.title AS source_title
          FROM intelligence_claims c
          JOIN intelligence_sources s ON s.id = c.source_id
         WHERE c.entity_id IS NOT NULL
        """
    ).fetchall():
        if row["entity_id"] in entities:
            entities[row["entity_id"]]["claims"].append(dict(row))

    for row in conn.execute(
        """
        SELECT
            es.entity_id,
            s.id,
            s.source_url,
            s.title,
            s.author,
            s.platform,
            es.role
          FROM intelligence_entity_sources es
          JOIN intelligence_sources s ON s.id = es.source_id
        """
    ).fetchall():
        if row["entity_id"] in entities:
            entities[row["entity_id"]]["sources"].append(dict(row))

    for row in conn.execute(
        """
        SELECT
            r.subject_entity_id,
            r.object_entity_id,
            r.predicate,
            r.source_id,
            s.source_url,
            subject.name AS subject_name,
            object.name AS object_name
          FROM intelligence_relations r
          JOIN intelligence_entities subject
            ON subject.id = r.subject_entity_id
          JOIN intelligence_entities object
            ON object.id = r.object_entity_id
          LEFT JOIN intelligence_sources s
            ON s.id = r.source_id
        """
    ).fetchall():
        item = dict(row)
        if row["subject_entity_id"] in entities:
            entities[row["subject_entity_id"]]["relations"].append(item)
        if row["object_entity_id"] in entities:
            entities[row["object_entity_id"]]["relations"].append(item)

    return entities


def _score_entity(data, phrase, tokens):
    row = data["row"]
    score = 0
    matched_on = set()

    name = _fold(row.get("name"))
    canonical = _fold(row.get("canonical_key"))
    summary = _fold(row.get("summary"))

    if phrase and name == phrase:
        score += 140
        matched_on.add("name")
    elif phrase and phrase in name:
        score += 95
        matched_on.add("name")

    for alias in data["aliases"]:
        folded = _fold(alias)
        if phrase and folded == phrase:
            score += 125
            matched_on.add("alias")
        elif phrase and phrase in folded:
            score += 85
            matched_on.add("alias")

    if phrase and phrase in canonical:
        score += 55
        matched_on.add("canonical_key")
    if phrase and phrase in summary:
        score += 50
        matched_on.add("summary")

    token_hits = _token_hits(row.get("name"), tokens)
    if token_hits:
        score += token_hits * 22
        matched_on.add("name")

    alias_hits = max(
        (_token_hits(alias, tokens) for alias in data["aliases"]),
        default=0,
    )
    if alias_hits:
        score += alias_hits * 18
        matched_on.add("alias")

    summary_hits = _token_hits(row.get("summary"), tokens)
    if summary_hits:
        score += summary_hits * 10
        matched_on.add("summary")

    canonical_hits = _token_hits(row.get("canonical_key"), tokens)
    if canonical_hits:
        score += canonical_hits * 8
        matched_on.add("canonical_key")

    for claim in data["claims"]:
        hits = _token_hits(claim.get("claim_text"), tokens)
        if phrase and _contains(claim.get("claim_text"), phrase):
            score += 45
            matched_on.add("claim")
        if hits:
            score += min(hits, 3) * 12
            matched_on.add("claim")

    for source in data["sources"]:
        searchable = " ".join(
            str(source.get(key) or "")
            for key in ("title", "author", "platform", "source_url")
        )
        hits = _token_hits(searchable, tokens)
        if phrase and _contains(searchable, phrase):
            score += 25
            matched_on.add("source")
        if hits:
            score += min(hits, 3) * 6
            matched_on.add("source")

    for relation in data["relations"]:
        searchable = " ".join(
            str(relation.get(key) or "")
            for key in ("subject_name", "object_name", "predicate")
        )
        hits = _token_hits(searchable, tokens)
        if phrase and _contains(searchable, phrase):
            score += 25
            matched_on.add("relation")
        if hits:
            score += min(hits, 3) * 7
            matched_on.add("relation")

    return score, sorted(matched_on)


def _relevance(item, fields, phrase, tokens):
    searchable = " ".join(str(item.get(field) or "") for field in fields)
    score = 0
    if phrase and _contains(searchable, phrase):
        score += 50
    score += _token_hits(searchable, tokens) * 10
    return score


def _claim_priority(claim):
    # Le statut n'est pas un score de verite. Ce tri sert seulement a ne pas
    # perdre une contestation ou une verification quand le contexte est borne.
    order = {
        "contested": 0,
        "verified": 1,
        "asserted": 2,
        "unverified": 3,
    }
    return order.get(claim.get("verification_status"), 4)


def _select_claims(claims, phrase, tokens, limit):
    ranked = sorted(
        claims,
        key=lambda item: (
            -_relevance(item, ("claim_text", "source_title"), phrase, tokens),
            _claim_priority(item),
            -int(item.get("id") or 0),
        ),
    )
    return ranked[:limit]


def _select_sources(sources, phrase, tokens, limit):
    ranked = sorted(
        sources,
        key=lambda item: (
            -_relevance(
                item,
                ("title", "author", "platform", "source_url"),
                phrase,
                tokens,
            ),
            0 if item.get("role") == "primary" else 1,
            -int(item.get("id") or 0),
        ),
    )
    return ranked[:limit]


def _select_relations(relations, phrase, tokens, limit):
    ranked = sorted(
        relations,
        key=lambda item: (
            -_relevance(
                item,
                ("related_entity_name", "predicate", "source_url"),
                phrase,
                tokens,
            ),
            -int(item.get("id") or 0),
        ),
    )
    return ranked[:limit]


def build_context_pack(
    conn,
    q,
    *,
    entity_limit=6,
    claims_per_entity=6,
    sources_per_entity=4,
    relations_per_entity=6,
):
    """
    Construit un paquet compact pour une IA en aval.

    Aucun resume n'est genere ici. Le summary renvoye est uniquement le summary
    actif deja stocke sur l'entite ou l'analyse active des sources.
    """
    intelligence.ensure_schema(conn)
    phrase, tokens = _query_parts(q)

    entity_limit = max(1, min(int(entity_limit), 20))
    claims_per_entity = max(1, min(int(claims_per_entity), 20))
    sources_per_entity = max(1, min(int(sources_per_entity), 20))
    relations_per_entity = max(1, min(int(relations_per_entity), 20))

    search_data = _entity_search_data(conn)
    ranked = []
    for entity_id, data in search_data.items():
        score, matched_on = _score_entity(data, phrase, tokens)
        if score > 0:
            ranked.append((score, entity_id, matched_on))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    ranked = ranked[:entity_limit]

    results = []
    for score, entity_id, matched_on in ranked:
        full = query_layer.get_entity(conn, entity_id)
        if full is None:
            continue

        claims = _select_claims(
            full.get("claims", []),
            phrase,
            tokens,
            claims_per_entity,
        )
        sources = _select_sources(
            full.get("sources", []),
            phrase,
            tokens,
            sources_per_entity,
        )
        relations = _select_relations(
            full.get("relations", []),
            phrase,
            tokens,
            relations_per_entity,
        )

        results.append(
            {
                "entity": {
                    "id": full["id"],
                    "entity_type": full["entity_type"],
                    "canonical_key": full["canonical_key"],
                    "name": full["name"],
                    "url": full.get("url"),
                    "summary": full.get("summary"),
                    "attention_status": full["attention_status"],
                    "aliases": full.get("aliases", []),
                },
                "match": {
                    "score": score,
                    "matched_on": matched_on,
                },
                "epistemic": full.get("epistemic", {}),
                "claims": claims,
                "sources": sources,
                "relations": relations,
            }
        )

    return {
        "query": q.strip(),
        "result_count": len(results),
        "entities": results,
        "epistemic_rules": {
            "asserted": (
                "The saved source asserts this claim; it is not independently verified."
            ),
            "unverified": (
                "The claim is explicitly tracked as not verified."
            ),
            "verified": (
                "The claim has a separate verification source recorded in the Vault."
            ),
            "contested": (
                "The claim has conflicting or contesting evidence and must not be "
                "presented as settled."
            ),
        },
        "generation": {
            "llm_used": False,
            "embeddings_used": False,
            "raw_model_results_included": False,
        },
    }
