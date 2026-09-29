#!/usr/bin/env python3
"""
mentor_project_links.py - Propositions de rattachement SOURCE -> PROJET (V3).

Phase intermediaire du Mentor proactif : comprendre a quel projet une source
existante peut etre utile, SANS creer de faux rattachements.

Reutilise :
- intelligence_entities + intelligence_entity_bindings : les projets ;
- mentor_project_profiles (vault.db) : ce qu'est chaque projet (donnees
  editables, voir mentor_project_profiles.py) ; ce module ne garde que des
  regles generiques ;
- intelligence_entity_sources : le rattachement REEL, cree uniquement apres
  validation humaine (role `relevant_to`, note = proposition d'origine) ;
- contents, tags, sous-themes, ressources : les signaux ;
- vault_db.fold, detecteur de sensibilite de vault_analysis.

Ajoute une seule table additive : mentor_project_link_proposals.
(intelligence_resolution_candidates sert a l'identite entite <-> entite ;
elle n'est pas detournee.)

Regles :
- jamais de rattachement sur un simple mot-cle, un outil generique ou un
  theme transversal ; une source peut rester sans projet ;
- HIGH / MEDIUM / LOW / AMBIGUOUS, avec signaux explicites ;
- LOW et AMBIGUOUS ne sont jamais appliques ; HIGH non plus sans validation ;
- aucun LLM, aucun reseau, aucun Notion.

Usage :
    python mentor_project_links.py               # = --dry-run (copie en memoire)
    python mentor_project_links.py --dry-run --show 20
    python mentor_project_links.py --review      # validation interactive (plus tard)
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import mentor_proactive_store as pstore

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "vault.db"
RULES_VERSION = "project-links-v3"
LINK_ROLE = "relevant_to"

LEVELS = ("high", "medium", "low", "ambiguous")
REVIEWABLE_LEVELS = ("high", "medium")
STATUSES = ("proposed", "approved", "rejected", "superseded")

# ------------------------------------------------------------------ regles generiques
#
# Les connaissances metier (sujets, noms, outils, exclusions de chaque projet)
# vivent dans vault.db : table mentor_project_profiles (mentor_project_profiles.py).
# Ce module ne contient que des regles GENERIQUES.

# Outils et termes universels : jamais un signal de projet, quel que soit le profil.
GENERIC_TERMS = frozenset({
    "ia", "ai", "chatgpt", "claude", "claude code", "codex", "llm", "gpt",
    "prompt", "prompts", "outil", "outils", "tool", "tools", "agent", "agents",
    "agent ia",
})

# Un client ou un prospect se designe par son nom : ses sujets seuls ne suffisent pas.
NAME_ONLY_TYPES = frozenset({"client", "prospect"})

WEIGHTS = {
    "name": 0.85,            # nom du projet / entite nommee du profil cite
    "shared_alias": 0.40,    # debut de nom partage par plusieurs projets (« Nexdoor »)
    "core": 0.30,            # par sujet principal distinct (max 3)
    "tool": 0.30,            # par outil specifique du profil (compte comme sujet)
    "core_ai_only": 0.15,    # sujet vu seulement dans le resume IA (seconde main)
    "context": 0.10,         # par sujet de contexte distinct (max 2)
    "classification": 0.15,  # tag / sous-theme / categorie du Vault = sujet du profil
    "proximity": 0.25,       # par source deja liee au projet et proche (max 2)
    "active": 0.05,          # projet marque « Maintenant »
}
MEDIUM_MIN = 0.55
HIGH_MIN = 0.80
AMBIGUITY_GAP = 0.15
MAX_CONCEPTS = 3
MAX_CONTEXT = 2

PRIMARY_FIELDS = ("title", "description_manual", "description_original", "transcript")
SECOND_HAND_FIELDS = ("description_ai",)
CLASSIFICATION_FIELDS = ("tags", "subthemes", "category")
CONCEPT_KINDS = ("core", "tool", "core_ai_only")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mentor_project_link_proposals (
    proposal_id      TEXT PRIMARY KEY,
    source_id        INTEGER NOT NULL,
    entity_id        INTEGER NOT NULL,
    confidence       REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    confidence_level TEXT NOT NULL CHECK (confidence_level IN
                       ('high', 'medium', 'low', 'ambiguous')),
    reason           TEXT NOT NULL,
    evidence_json    TEXT NOT NULL DEFAULT '{}',
    rules_version    TEXT NOT NULL,
    fingerprint      TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'proposed'
                     CHECK (status IN ('proposed', 'approved', 'rejected', 'superseded')),
    review_note      TEXT,
    reviewed_by      TEXT,
    link_applied_at  TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    reviewed_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_link_one_open
    ON mentor_project_link_proposals(source_id, entity_id)
    WHERE status = 'proposed';
CREATE INDEX IF NOT EXISTS idx_project_link_status
    ON mentor_project_link_proposals(status, confidence_level);
"""


class ProjectLinkError(ValueError):
    pass


# ------------------------------------------------------------------ texte

def fold(text):
    from vault_db import fold as vault_fold

    return " ".join(vault_fold(str(text or "")).replace("’", "'").split())


def _term_pattern(term):
    return re.compile(r"(?<![a-z0-9])" + re.escape(fold(term)) + r"(?![a-z0-9])")


def _excerpt(text, start, end, width=60):
    left = max(0, start - width)
    right = min(len(text), end + width)
    return ("..." if left else "") + text[left:right].strip() + ("..." if right < len(text) else "")


def _spans(term, fields):
    """Premiere occurrence du terme par champ : {champ: (debut, fin, extrait)}."""
    pattern = _term_pattern(term)
    found = {}
    for field, text in fields.items():
        match = pattern.search(text)
        if match:
            found[field] = (match.start(), match.end(), _excerpt(text, match.start(), match.end()))
    return found


def _find(term, fields):
    return {field: span[2] for field, span in _spans(term, fields).items()}


def _noisy_or(weights):
    remaining = 1.0
    for weight in weights:
        remaining *= 1.0 - weight
    return round(1.0 - remaining, 3)


# ------------------------------------------------------------------ lecture

def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _compile_profile(entity, stored):
    """Profil stocke -> structure de matching. Aucun sujet invente."""
    stored = stored or {}
    weak = {fold(e["term"]) for e in stored.get("excluded_topics", [])
            if e["mode"] == "alone_insufficient"}
    block = [e["term"] for e in stored.get("excluded_topics", []) if e["mode"] == "block"]

    def concepts(items, kind):
        result = []
        for item in items or []:
            terms = [t for t in item["terms"]
                     if fold(t) not in GENERIC_TERMS and fold(t) not in weak]
            if terms:
                result.append({"topic": item["topic"], "terms": terms, "kind": kind,
                               "provenance": item.get("provenance")})
        return result

    names = {fold(entity["name"]): "nom du projet"}
    for name in stored.get("named_entities", []):
        names.setdefault(fold(name), "entite nommee du profil")
    rules = []
    for rule in stored.get("compound_rules", []):
        rules.append({
            "name": rule["name"], "level": rule["level"], "context_min": rule["context_min"],
            "primary": concepts(rule["primary_any"], "core"),
            "context": concepts(rule["context_any"], "context"),
        })
    return {
        "rules": rules,
        "defer": [e["project"] for e in stored.get("excluded_topics", [])
                  if e["mode"] == "defer"],
        "active": bool(stored.get("active", 1)),
        "names": names,
        "core": concepts(stored.get("core_topics"), "core")
        + concepts(stored.get("specific_tools"), "tool"),
        "context": [dict(c, kind="context") for c in concepts(stored.get("context_topics"),
                                                              "context")],
        "block": block,
        "status": stored.get("profile_status", "missing"),
        "source": stored.get("profile_source"),
    }


def load_projects(conn):
    """Projets connus du Vault, avec leur profil lu dans vault.db."""
    import mentor_proactive as proactive
    import mentor_project_profiles as profiles

    view = proactive.VaultView(conn)
    projects = {}
    for entity_id in view.project_entities():
        row = conn.execute(
            "SELECT id, entity_type, canonical_key, name, attention_status "
            "FROM intelligence_entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        entity = dict(row)
        entity["profile"] = _compile_profile(entity, profiles.get_profile(conn, entity_id))
        entity["parent_entity_id"] = view.entity(entity_id)["parent_entity_id"]
        projects[entity_id] = entity
    return projects


def _shared_aliases(projects):
    """Premier mot d'un nom partage par plusieurs projets non apparentes (« nexdoor »)."""
    owners = {}
    for pid, project in projects.items():
        for name in project["profile"]["names"]:
            first = name.split(" ")[0]
            if len(first) >= 4 and first != name:
                owners.setdefault(first, set()).add(pid)
    aliases = {}
    for alias, pids in owners.items():
        if len(pids) < 2:
            continue
        unrelated = [p for p in pids if not any(
            q in _ancestors(projects, p) for q in pids if q != p)]
        if len(unrelated) >= 2:
            aliases[alias] = sorted(unrelated)
    return aliases


def _ancestors(projects, entity_id):
    seen = []
    parent = projects.get(entity_id, {}).get("parent_entity_id")
    while parent is not None and parent not in seen:
        seen.append(parent)
        parent = projects.get(parent, {}).get("parent_entity_id")
    return seen


def load_sources(conn):
    """Texte de chaque source, champ par champ (plie, sans accents)."""
    sources = []
    has_contents = _table_exists(conn, "contents")
    for row in conn.execute(
        "SELECT id, title, author, content_id FROM intelligence_sources ORDER BY id"
    ).fetchall():
        source = dict(row)
        fields = {"title": fold(source["title"])}
        raw = {"title": source["title"]}
        if has_contents and source["content_id"]:
            content = conn.execute(
                "SELECT title, description_manual, description_original, description_ai, "
                "transcript, category FROM contents WHERE id = ?",
                (source["content_id"],),
            ).fetchone()
            if content:
                content = dict(content)
                raw.update(content)
                for key in ("title", "description_manual", "description_original",
                            "description_ai", "transcript", "category"):
                    if content.get(key):
                        fields[key] = fold(content[key])
                tags = [r[0] for r in conn.execute(
                    "SELECT t.name FROM content_tags ct JOIN tags t ON t.id = ct.tag_id "
                    "WHERE ct.content_id = ? ORDER BY t.name", (source["content_id"],))]
                subthemes = [r[0] for r in conn.execute(
                    "SELECT s.name FROM content_subthemes cs JOIN subthemes s "
                    "ON s.id = cs.subtheme_id WHERE cs.content_id = ? ORDER BY s.name",
                    (source["content_id"],))]
                if tags:
                    fields["tags"] = " | ".join(fold(t) for t in tags)
                if subthemes:
                    fields["subthemes"] = " | ".join(fold(s) for s in subthemes)
        source["fields"] = {k: v for k, v in fields.items() if v}
        source["display_title"] = raw.get("title") or source["title"]
        sources.append(source)
    return sources


def _linked_sources(conn):
    """{entity_id: set(source_id)} deja relies (liens reels existants)."""
    links = {}
    if not _table_exists(conn, "intelligence_entity_sources"):
        return links
    for entity_id, source_id in conn.execute(
        "SELECT entity_id, source_id FROM intelligence_entity_sources WHERE role <> 'primary'"
    ).fetchall():
        links.setdefault(entity_id, set()).add(source_id)
    return links


def _source_entities(conn, project_ids):
    """Entites specifiques (ni generiques, ni projets) citees par chaque source."""
    result = {}
    if not _table_exists(conn, "intelligence_entity_sources"):
        return result
    for source_id, entity_id, name in conn.execute(
        "SELECT es.source_id, e.id, e.name FROM intelligence_entity_sources es "
        "JOIN intelligence_entities e ON e.id = es.entity_id WHERE es.role <> 'primary'"
    ).fetchall():
        if fold(name) in GENERIC_TERMS or entity_id in project_ids:
            continue
        result.setdefault(source_id, {})[entity_id] = name
    return result


# ------------------------------------------------------------------ analyse

def _concept_signals(profile, primary, second_hand):
    """
    Un signal par sujet. Deux sujets qui se chevauchent dans le texte
    (« recrutement » dans « recrutement tech ») ne comptent qu'une fois : le plus long.
    """
    hits = []
    for concept in profile["core"]:
        for term in sorted(concept["terms"], key=len, reverse=True):
            spans = _spans(term, primary)
            kind = concept["kind"]
            if not spans:
                spans = _spans(term, second_hand)
                kind = "core_ai_only" if spans else kind
            if spans:
                hits.append({"concept": concept, "term": term, "kind": kind, "spans": spans})
                break
    hits.sort(key=lambda h: -max(e - s for s, e, _ in h["spans"].values()))
    accepted, taken = [], []
    for hit in hits:
        overlaps = any(
            field == other_field and s < oe and os_ < e
            for field, (s, e, _) in hit["spans"].items()
            for other_field, (os_, oe) in taken
        )
        if overlaps and all(
            any(field == of and s < oe and os_ < e for of, (os_, oe) in taken)
            for field, (s, e, _) in hit["spans"].items()
        ):
            continue
        accepted.append(hit)
        taken.extend((field, (s, e)) for field, (s, e, _) in hit["spans"].items())
    signals = []
    for hit in sorted(accepted, key=lambda h: (CONCEPT_KINDS.index(h["kind"]),
                                                 hit_order(h)))[:MAX_CONCEPTS]:
        signals.append({
            "kind": hit["kind"], "concept": hit["concept"]["topic"], "term": hit["term"],
            "provenance": hit["concept"].get("provenance"),
            "fields": {f: span[2] for f, span in hit["spans"].items()},
            "weight": WEIGHTS[hit["kind"]],
        })
    return signals


def hit_order(hit):
    return hit["concept"]["topic"]


def _first_hit(concept, fields):
    for term in sorted(concept["terms"], key=len, reverse=True):
        found = _find(term, fields)
        if found:
            return term, found
    return None


def _evaluate_rule(rule, primary, second_hand, classification):
    """Regle composee (donnee du profil) : un sujet principal + N contextes distincts."""
    main = None
    for concept in rule["primary"]:
        hit = _first_hit(concept, {**primary, **second_hand})
        if hit:
            main = {"topic": concept["topic"], "term": hit[0], "fields": hit[1]}
            break
    if main is None:
        return None
    # Contextes DISTINCTS : un contexte dont toutes les occurrences sont incluses
    # dans celles d'un autre (« leads » dans « generation de leads ») ne compte pas.
    fields = {**primary, **second_hand, **classification}
    hits = []
    for concept in rule["context"]:
        for term in sorted(concept["terms"], key=len, reverse=True):
            spans = _spans(term, fields)
            if spans:
                hits.append((concept["topic"], term, spans))
                break
    hits.sort(key=lambda h: -max(e - b for b, e, _ in h[2].values()))
    contexts, taken = [], []
    for topic, term, spans in hits:
        inside = all(
            any(f == tf and tb <= b and e <= te for tf, tb, te in taken)
            for f, (b, e, _) in spans.items()
        )
        if inside:
            continue
        contexts.append({"topic": topic, "term": term,
                         "fields": {f: span[2] for f, span in spans.items()}})
        taken.extend((f, b, e) for f, (b, e, _) in spans.items())
    if len(contexts) < rule["context_min"]:
        return None
    return {"kind": "compound_rule", "rule": rule["name"], "level": rule["level"],
            "main": main, "contexts": contexts, "weight": 0.0}


def _signals_for(project, source, *, proximity):
    profile = project["profile"]
    fields = source["fields"]
    primary = {k: v for k, v in fields.items() if k in PRIMARY_FIELDS}
    second_hand = {k: v for k, v in fields.items() if k in SECOND_HAND_FIELDS}
    classification = {k: v for k, v in fields.items() if k in CLASSIFICATION_FIELDS}
    signals = []

    names = []
    for name, provenance in profile["names"].items():
        found = _find(name, {**primary, **second_hand})
        if found:
            names.append({"kind": "name", "term": name, "provenance": provenance,
                          "fields": found, "weight": WEIGHTS["name"]})
    if names:
        signals.append(max(names, key=lambda s: len(s["term"])))

    signals.extend(_concept_signals(profile, primary, second_hand))

    context = []
    for concept in profile["context"]:
        for term in sorted(concept["terms"], key=len, reverse=True):
            found = _find(term, {**primary, **second_hand, **classification})
            if found:
                context.append({"kind": "context", "concept": concept["topic"], "term": term,
                                "provenance": concept.get("provenance"), "fields": found,
                                "weight": WEIGHTS["context"]})
                break
    signals.extend(context[:MAX_CONTEXT])

    for concept in profile["core"]:
        hit = next(((t, f) for t in concept["terms"] for f in [_find(t, classification)] if f),
                   None)
        if hit:
            signals.append({"kind": "classification", "concept": concept["topic"],
                            "term": hit[0], "provenance": concept.get("provenance"),
                            "fields": hit[1], "weight": WEIGHTS["classification"]})
            break

    for item in proximity[:2]:
        signals.append({"kind": "proximity", "weight": WEIGHTS["proximity"], **item})
    if project["attention_status"] == "now" and signals:
        signals.append({"kind": "active", "weight": WEIGHTS["active"]})

    for rule in profile["rules"]:
        hit = _evaluate_rule(rule, primary, second_hand, classification)
        if hit:
            signals.append(hit)

    blocked = []
    for term in profile["block"]:
        found = _find(term, {**primary, **second_hand})
        if found:
            blocked.append({"term": term, "fields": found})
    return signals, blocked


def _level(project, signals, *, sensitive, blocked):
    if not signals:
        return None, 0.0, None
    rule_hits = [s for s in signals if s["kind"] == "compound_rule"]
    signals = [s for s in signals if s["kind"] != "compound_rule"]
    if not signals and not rule_hits:
        return None, 0.0, None
    kinds = [s["kind"] for s in signals]
    confidence = _noisy_or(s["weight"] for s in signals)
    concept_count = sum(1 for k in kinds if k in CONCEPT_KINDS)
    strong = sum(1 for k in kinds if k in ("core", "tool"))
    support = any(k in kinds for k in ("context", "classification", "proximity"))

    if "name" in kinds:
        return "high", max(confidence, WEIGHTS["name"]), "mention explicite du projet"
    if blocked:
        terms = ", ".join(sorted({b["term"] for b in blocked}))
        return "low", confidence, f"anti-signal du profil ({terms}) : jamais rattache"
    # Source sensible : plafonnee a LOW, sauf pour un projet dont le profil declare ses
    # propres anti-signaux (ex. sante personnelle) et qu'aucun n'est present.
    health_guarded = bool(project["profile"]["block"])
    if sensitive and not health_guarded:
        return "low", confidence, "source sensible (sante/finance/droit) sans mention explicite du projet"
    if project["entity_type"] in NAME_ONLY_TYPES:
        return "low", confidence, "un client/prospect se designe par son nom"
    if rule_hits:
        rule = rule_hits[0]
        contexts = ", ".join(c["topic"] for c in rule["contexts"])
        reason = (f"regle composee '{rule['rule']}' : {rule['main']['topic']} + "
                  f"contextes metier ({contexts})")
        if sensitive:
            reason += " (source sensible : verifier qu'il ne s'agit pas de sante personnelle)"
        return rule["level"], max(confidence, MEDIUM_MIN), reason
    # Un troisieme sujet distinct vaut element de contexte.
    if concept_count >= 2 and strong >= 1 and (support or concept_count >= 3):
        independent = len({k for k in kinds if k in ("core", "tool", "classification",
                                                     "proximity")})
        if independent >= 3 and confidence >= HIGH_MIN and not sensitive:
            return "high", confidence, "plusieurs signaux independants coherents"
        if confidence >= MEDIUM_MIN:
            reason = "sujets du projet presents avec des elements de contexte"
            if sensitive:
                reason += " (source sensible : verifier qu'il ne s'agit pas de sante personnelle)"
            return "medium", confidence, reason
        return "low", confidence, "sujets presents mais signaux trop faibles"
    if concept_count >= 2:
        return "low", confidence, "sujets du projet sans element de contexte"
    if concept_count == 1:
        return "low", confidence, "un seul sujet du projet : simple mot-cle"
    return "low", confidence, "seulement des elements de contexte"


def analyze(conn):
    """Calcule toutes les propositions. Lecture seule."""
    import mentor_proactive as proactive

    projects = load_projects(conn)
    sources = load_sources(conn)
    linked = _linked_sources(conn)
    source_entities = _source_entities(conn, set(projects))
    aliases = _shared_aliases(projects)
    view = proactive.VaultView(conn)

    results = []
    for source in sources:
        sid = source["id"]
        sensitive = view.source_is_sensitive(sid)
        candidates = []
        for pid, project in projects.items():
            if sid in linked.get(pid, set()):
                continue  # deja rattachee
            proximity = []
            mine = source_entities.get(sid, {})
            for other in sorted(linked.get(pid, set()) - {sid}):
                shared = sorted(set(mine) & set(source_entities.get(other, {})))
                if shared:
                    proximity.append({"source_id": other,
                                      "shared_entities": [mine[e] for e in shared]})
            signals, blocked = _signals_for(project, source, proximity=proximity)
            level, confidence, reason = _level(project, signals, sensitive=sensitive,
                                               blocked=blocked)
            if not project["profile"]["active"] and level in ("high", "medium"):
                level, reason = "low", "projet marque inactif dans son profil"
            if level is None:
                continue
            candidates.append({
                "source_id": sid, "entity_id": pid, "project": project["name"],
                "project_type": project["entity_type"], "level": level,
                "confidence": round(confidence, 3), "reason": reason,
                "signals": signals, "blocked": blocked,
            })

        text_fields = {k: v for k, v in source["fields"].items()
                       if k not in CLASSIFICATION_FIELDS}
        named = {c["entity_id"] for c in candidates if "name" in
                 [s["kind"] for s in c["signals"]]}
        for alias, ids in aliases.items():
            found = _find(alias, text_fields)
            if found and not named & set(ids):
                candidates.append({
                    "source_id": sid, "entity_id": ids[0],
                    "project": " / ".join(projects[i]["name"] for i in ids),
                    "project_type": "shared_alias", "level": "ambiguous",
                    "confidence": WEIGHTS["shared_alias"],
                    "reason": f"alias '{alias}' partage par plusieurs projets",
                    "signals": [{"kind": "shared_alias", "term": alias, "fields": found,
                                 "weight": WEIGHTS["shared_alias"]}],
                    "blocked": [], "competitors": ids,
                })

        _apply_defer(candidates, projects, sid, linked)
        _resolve_competition(candidates, projects)
        source["sensitive"] = sensitive
        results.append({"source": source, "candidates": candidates})
    return {"projects": projects, "results": results}


def _apply_defer(candidates, projects, source_id, linked):
    """
    Exclusion `defer` (donnee du profil) : si la source est deja clairement
    rattachee a un autre projet designe (lien reel, HIGH ou MEDIUM), ce projet s'efface.
    """
    by_key = {p["canonical_key"]: pid for pid, p in projects.items()}
    strong = {c["entity_id"] for c in candidates if c["level"] in ("high", "medium")}
    for cand in candidates:
        project = projects.get(cand["entity_id"])
        if not project or cand["level"] not in ("high", "medium"):
            continue
        for key in project["profile"]["defer"]:
            other = by_key.get(key)
            if other is None or other == cand["entity_id"]:
                continue
            if other in strong or source_id in linked.get(other, set()):
                cand["level"] = "low"
                cand["reason"] = (f"s'efface : source deja clairement rattachee a "
                                  f"{projects[other]['name']}")
                break


def _resolve_competition(candidates, projects):
    """Un parent s'efface devant un descendant nomme ; ex aequo -> AMBIGUOUS."""
    strong = [c for c in candidates if c["level"] in ("high", "medium")]
    for cand in strong:
        for other in strong:
            if other is cand:
                continue
            if cand["entity_id"] in _ancestors(projects, other["entity_id"]) and \
                    other["level"] == "high":
                cand["level"] = "low"
                cand["reason"] = f"couvert par le rattachement plus precis a {other['project']}"
    strong = [c for c in candidates if c["level"] in ("high", "medium")]
    if len(strong) < 2:
        return
    strong.sort(key=lambda c: -c["confidence"])
    best, second = strong[0], strong[1]
    related = best["entity_id"] in _ancestors(projects, second["entity_id"]) or \
        second["entity_id"] in _ancestors(projects, best["entity_id"])
    if not related and best["confidence"] - second["confidence"] < AMBIGUITY_GAP:
        names = [c["project"] for c in strong]
        for cand in strong:
            cand["level"] = "ambiguous"
            cand["reason"] = "plusieurs projets possibles : " + ", ".join(names)
    else:
        for cand in strong[1:]:
            if cand["level"] == "medium":
                cand["level"] = "low"
                cand["reason"] = f"moins convaincant que {best['project']}"


# ------------------------------------------------------------------ explication

SIGNAL_LABELS = {
    "name": "mention explicite",
    "shared_alias": "alias partage",
    "core": "sujet du projet",
    "core_ai_only": "sujet du projet (resume IA seulement, seconde main)",
    "tool": "outil specifique du projet",
    "context": "element de contexte",
    "classification": "classement du Vault",
    "proximity": "proche d'une source deja liee au projet",
    "active": "projet marque Maintenant",
}
PROVENANCE_LABELS = {
    "name": "nom du projet", "rosy": "profil de Rosy",
    "synonym": "synonyme", "inferred": "deduit, a valider",
}


def explain(candidate):
    lines = []
    for signal in candidate["signals"]:
        if signal["kind"] == "compound_rule":
            contexts = "; ".join(
                f"{c['topic']} (dans {', '.join(c['fields'])})" for c in signal["contexts"])
            lines.append(
                f"regle composee '{signal['rule']}' du profil : sujet principal "
                f"'{signal['main']['topic']}' (mot '{signal['main']['term']}') + contextes "
                f"metier : {contexts}")
            continue
        label = SIGNAL_LABELS[signal["kind"]]
        if signal["kind"] == "proximity":
            lines.append(f"{label} : source {signal['source_id']} "
                         f"(entites communes : {', '.join(signal['shared_entities'])})")
            continue
        if signal["kind"] == "active":
            lines.append(label)
            continue
        where = ", ".join(signal["fields"])
        excerpt = next(iter(signal["fields"].values()))
        provenance = PROVENANCE_LABELS.get(signal.get("provenance"),
                                           signal.get("provenance") or "")
        what = (f"'{signal['concept']}' (mot '{signal['term']}')"
                if signal.get("concept") and signal["concept"] != signal["term"]
                else f"'{signal['term']}'")
        lines.append(
            f"{label} {what}"
            + (f" [{provenance}]" if provenance else "")
            + f" dans {where} : \"{excerpt[:140]}\""
        )
    return lines


# ------------------------------------------------------------------ stockage

def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _fingerprint(candidate):
    signature = sorted(
        (s["kind"], s.get("term") or s.get("rule") or str(s.get("source_id") or ""))
        for s in candidate["signals"]
    )
    return pstore.sha256(json.dumps(
        [RULES_VERSION, candidate["source_id"], candidate["entity_id"],
         candidate["level"], signature], ensure_ascii=False,
    ))


def _next_id(conn):
    row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(proposal_id, 4) AS INTEGER)) FROM mentor_project_link_proposals"
    ).fetchone()
    return f"PL-{(row[0] or 0) + 1:04d}"


def record_proposals(conn, analysis, *, at=None):
    """
    Enregistre les propositions (tous niveaux, pour calibrer). Idempotent :
    une proposition identique n'est jamais dupliquee ; une proposition ouverte
    dont les preuves changent est remplacee (superseded) ; une paire deja
    decidee (approuvee/refusee) ou deja liee n'est pas re-proposee.
    """
    ensure_schema(conn)
    at = at or pstore.now_iso()
    counts = {"created": 0, "unchanged": 0, "superseded": 0, "already_decided": 0}
    for result in analysis["results"]:
        for cand in result["candidates"]:
            if cand["project_type"] == "shared_alias":
                continue  # journalise dans le rapport, pas de paire source/projet unique
            fingerprint = _fingerprint(cand)
            if conn.execute(
                "SELECT 1 FROM mentor_project_link_proposals WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone():
                counts["unchanged"] += 1
                continue
            if conn.execute(
                "SELECT 1 FROM mentor_project_link_proposals WHERE source_id = ? "
                "AND entity_id = ? AND status IN ('approved', 'rejected')",
                (cand["source_id"], cand["entity_id"]),
            ).fetchone():
                counts["already_decided"] += 1
                continue
            cur = conn.execute(
                "UPDATE mentor_project_link_proposals SET status = 'superseded', updated_at = ? "
                "WHERE source_id = ? AND entity_id = ? AND status = 'proposed'",
                (at, cand["source_id"], cand["entity_id"]),
            )
            counts["superseded"] += cur.rowcount
            conn.execute(
                """
                INSERT INTO mentor_project_link_proposals (
                    proposal_id, source_id, entity_id, confidence, confidence_level,
                    reason, evidence_json, rules_version, fingerprint, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
                """,
                (
                    _next_id(conn), cand["source_id"], cand["entity_id"],
                    cand["confidence"], cand["level"], cand["reason"],
                    pstore.dumps({"signals": cand["signals"], "explanation": explain(cand)}),
                    RULES_VERSION, fingerprint, at, at,
                ),
            )
            counts["created"] += 1
    conn.commit()
    return counts


def get_proposal(conn, proposal_id):
    row = conn.execute(
        "SELECT * FROM mentor_project_link_proposals WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["evidence"] = pstore.loads(item["evidence_json"], {})
    return item


def reviewable(conn):
    ensure_schema(conn)
    return [
        get_proposal(conn, row[0])
        for row in conn.execute(
            "SELECT proposal_id FROM mentor_project_link_proposals "
            "WHERE status = 'proposed' AND confidence_level IN ('high', 'medium') "
            "ORDER BY CASE confidence_level WHEN 'high' THEN 0 ELSE 1 END, "
            "confidence DESC, proposal_id"
        ).fetchall()
    ]


def approve(conn, proposal_id, *, reviewed_by="rosy", note=None, at=None):
    """Validation humaine : applique le rattachement reel. Jamais LOW/AMBIGUOUS."""
    import vault_intelligence as intelligence

    proposal = get_proposal(conn, proposal_id)
    if proposal is None:
        raise ProjectLinkError(f"proposition introuvable : {proposal_id}")
    if proposal["status"] != "proposed":
        raise ProjectLinkError(f"{proposal_id} est deja {proposal['status']}")
    if proposal["confidence_level"] not in REVIEWABLE_LEVELS:
        raise ProjectLinkError(
            f"{proposal_id} est {proposal['confidence_level']} : jamais applique"
        )
    at = at or pstore.now_iso()
    intelligence.link_entity_source(
        conn, entity_id=proposal["entity_id"], source_id=proposal["source_id"],
        role=LINK_ROLE, notes=f"{proposal_id} valide par {reviewed_by}",
    )
    conn.execute(
        "UPDATE mentor_project_link_proposals SET status = 'approved', reviewed_at = ?, "
        "reviewed_by = ?, review_note = ?, link_applied_at = ?, updated_at = ? "
        "WHERE proposal_id = ?",
        (at, reviewed_by, note, at, at, proposal_id),
    )
    conn.commit()
    return get_proposal(conn, proposal_id)


def reject(conn, proposal_id, *, reviewed_by="rosy", note=None, at=None):
    proposal = get_proposal(conn, proposal_id)
    if proposal is None:
        raise ProjectLinkError(f"proposition introuvable : {proposal_id}")
    if proposal["status"] != "proposed":
        raise ProjectLinkError(f"{proposal_id} est deja {proposal['status']}")
    at = at or pstore.now_iso()
    conn.execute(
        "UPDATE mentor_project_link_proposals SET status = 'rejected', reviewed_at = ?, "
        "reviewed_by = ?, review_note = ?, updated_at = ? WHERE proposal_id = ?",
        (at, reviewed_by, note, at, proposal_id),
    )
    conn.commit()
    return get_proposal(conn, proposal_id)


def record_decision(conn, *, source_id, entity_id, decision, level, confidence, reason,
                    explanation=(), rules_version=RULES_VERSION, reviewed_by="rosy",
                    note=None, at=None):
    """
    Enregistre une decision humaine prise sur un dry-run deja presente
    (sans passer par --review). decision = 'approve' | 'reject'.
    Idempotent : une paire deja decidee n'est pas modifiee.
    """
    if decision not in ("approve", "reject"):
        raise ProjectLinkError("decision = approve ou reject")
    ensure_schema(conn)
    for table, key in (("intelligence_sources", source_id), ("intelligence_entities", entity_id)):
        if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (key,)).fetchone() is None:
            raise ProjectLinkError(f"{table} {key} introuvable")
    decided = conn.execute(
        "SELECT proposal_id FROM mentor_project_link_proposals WHERE source_id = ? "
        "AND entity_id = ? AND status IN ('approved', 'rejected')",
        (source_id, entity_id),
    ).fetchone()
    if decided:
        return get_proposal(conn, decided[0])
    at = at or pstore.now_iso()
    conn.execute(
        "UPDATE mentor_project_link_proposals SET status = 'superseded', updated_at = ? "
        "WHERE source_id = ? AND entity_id = ? AND status = 'proposed'",
        (at, source_id, entity_id),
    )
    proposal_id = _next_id(conn)
    fingerprint = pstore.sha256(json.dumps(
        [rules_version, source_id, entity_id, level, "human_decision"], ensure_ascii=False))
    conn.execute(
        """
        INSERT INTO mentor_project_link_proposals (
            proposal_id, source_id, entity_id, confidence, confidence_level, reason,
            evidence_json, rules_version, fingerprint, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
        """,
        (proposal_id, source_id, entity_id, float(confidence), level, reason,
         pstore.dumps({"explanation": list(explanation), "recorded_from": "dry-run"}),
         rules_version, fingerprint, at, at),
    )
    conn.commit()
    if decision == "approve":
        return approve(conn, proposal_id, reviewed_by=reviewed_by, note=note, at=at)
    return reject(conn, proposal_id, reviewed_by=reviewed_by, note=note, at=at)


# ------------------------------------------------------------------ rapport

def stats(analysis):
    counts = {level: 0 for level in LEVELS}
    per_project = {}
    no_proposal = 0
    for result in analysis["results"]:
        best = _best(result["candidates"])
        if best is None or best["level"] in ("low", "ambiguous"):
            no_proposal += 1
        for cand in result["candidates"]:
            counts[cand["level"]] += 1
            if cand["level"] in REVIEWABLE_LEVELS:
                bucket = per_project.setdefault(cand["project"], {"high": 0, "medium": 0})
                bucket[cand["level"]] += 1
    return {
        "sources_analyzed": len(analysis["results"]),
        "proposals_by_level": counts,
        "sources_with_reviewable_proposal": len(analysis["results"]) - no_proposal,
        "sources_without_reviewable_proposal": no_proposal,
        "projects": per_project,
        "projects_known": len(analysis["projects"]),
    }


LEVEL_ORDER = {"high": 0, "medium": 1, "ambiguous": 2, "low": 3}


def _best(candidates):
    if not candidates:
        return None
    return sorted(candidates, key=lambda c: (LEVEL_ORDER[c["level"]], -c["confidence"]))[0]


def render(analysis, *, show=20, show_low=10):
    summary = stats(analysis)
    lines = [
        f"Rattachements source -> projet ({RULES_VERSION}) - DRY-RUN, aucune ecriture",
        "",
        f"Sources analysees : {summary['sources_analyzed']}  |  projets connus : "
        f"{summary['projects_known']}",
        "Propositions : " + ", ".join(
            f"{level.upper()} {count}" for level, count in summary["proposals_by_level"].items()
        ),
        f"Sources avec proposition HIGH/MEDIUM : {summary['sources_with_reviewable_proposal']}"
        f"  |  sans (LOW/AMBIGUOUS/aucune) : {summary['sources_without_reviewable_proposal']}",
        "Projets concernes (HIGH/MEDIUM) : " + (", ".join(
            f"{name} (H{c['high']}/M{c['medium']})" for name, c in summary["projects"].items()
        ) or "aucun"),
        "",
    ]
    by_source = {r["source"]["id"]: r["source"] for r in analysis["results"]}
    flat = [c for r in analysis["results"] for c in r["candidates"]]
    reviewable_items = sorted(
        [c for c in flat if c["level"] in REVIEWABLE_LEVELS],
        key=lambda c: (LEVEL_ORDER[c["level"]], -c["confidence"], c["source_id"]),
    )
    lines.append(f"=== A VALIDER (HIGH/MEDIUM) : {len(reviewable_items)} ===")
    for cand in reviewable_items[:show]:
        lines.extend(_render_candidate(cand, by_source))
    if not reviewable_items:
        lines.append("    (aucune)")
    others = sorted(
        [c for c in flat if c["level"] not in REVIEWABLE_LEVELS],
        key=lambda c: (LEVEL_ORDER[c["level"]], -c["confidence"], c["source_id"]),
    )
    lines.append("")
    lines.append(f"=== NON APPLICABLES (LOW/AMBIGUOUS) : {len(others)} - exemples ===")
    for cand in others[:show_low]:
        lines.extend(_render_candidate(cand, by_source))
    lines.append("")
    lines.append("Aucun rattachement n'est applique. Validation : --review (plus tard).")
    return "\n".join(lines)


def _render_candidate(cand, by_source):
    source = by_source[cand["source_id"]]
    title = (source.get("display_title") or "").replace("\n", " ")[:90]
    lines = [
        "",
        f"SOURCE {cand['source_id']} - {title}",
        f"  Projet propose : {cand['project']}",
        f"  Confiance : {cand['confidence']:.2f} {cand['level'].upper()} - {cand['reason']}",
        "  Signaux :",
    ]
    lines.extend(f"   - {line}" for line in explain(cand))
    return lines


# ------------------------------------------------------------------ CLI

def _backup(path):
    target_dir = ROOT / "backups" / f"project_links_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
    target_dir.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(target_dir / "vault.db")
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    return target_dir / "vault.db"


def review(conn, *, input_func=input, out=print):
    """[1] approuver  [2] refuser  [3] passer  [q] quitter."""
    decided = {"approved": 0, "rejected": 0, "skipped": 0}
    items = reviewable(conn)
    for index, proposal in enumerate(items, start=1):
        out("")
        out(f"[{index}/{len(items)}] {proposal['proposal_id']} - source {proposal['source_id']} "
            f"-> entite {proposal['entity_id']}")
        out(f"  Confiance : {proposal['confidence']:.2f} "
            f"{proposal['confidence_level'].upper()} - {proposal['reason']}")
        for line in proposal["evidence"].get("explanation", []):
            out(f"   - {line}")
        choice = input_func("[1] approuver  [2] refuser  [3] passer  [q] quitter > ").strip().lower()
        if choice == "1":
            approve(conn, proposal["proposal_id"])
            decided["approved"] += 1
        elif choice == "2":
            note = input_func("Raison (optionnelle) > ").strip() or None
            reject(conn, proposal["proposal_id"], note=note)
            decided["rejected"] += 1
        elif choice == "q":
            break
        else:
            decided["skipped"] += 1
    return decided


def main(argv=None):
    parser = argparse.ArgumentParser(description="Propositions de rattachement source -> projet")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="copie en memoire (defaut)")
    group.add_argument("--review", action="store_true", help="validation interactive")
    parser.add_argument("--show", type=int, default=20)
    parser.add_argument("--show-low", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    if args.review:
        backup = _backup(args.db)
        print(f"Sauvegarde : {backup}")
        from vault_db import connect

        conn = connect(args.db)
        try:
            counts = record_proposals(conn, analyze(conn))
            print(f"Propositions : {counts}")
            print(f"Decisions : {review(conn)}")
        finally:
            conn.close()
        return 0

    import mentor_proactive as proactive

    conn = proactive.open_dry_run_copy(args.db)
    try:
        analysis = analyze(conn)
    finally:
        conn.close()
    if args.json:
        payload = {
            "stats": stats(analysis),
            "proposals": [
                {k: v for k, v in c.items() if k != "signals"} | {"explanation": explain(c)}
                for r in analysis["results"] for c in r["candidates"]
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(analysis, show=args.show, show_low=args.show_low))
    return 0


if __name__ == "__main__":
    sys.exit(main())
