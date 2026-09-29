#!/usr/bin/env python3
"""
mentor_project_links.py - Propositions de rattachement SOURCE -> PROJET (V3).

Phase intermediaire du Mentor proactif : comprendre a quel projet une source
existante peut etre utile, SANS creer de faux rattachements.

Reutilise :
- intelligence_entities + intelligence_entity_bindings : les projets ;
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
RULES_VERSION = "project-links-v1"
LINK_ROLE = "relevant_to"

LEVELS = ("high", "medium", "low", "ambiguous")
REVIEWABLE_LEVELS = ("high", "medium")
STATUSES = ("proposed", "approved", "rejected", "superseded")

# ------------------------------------------------------------------ profils
#
# Profils minimaux des projets. Chaque terme porte sa provenance :
#   name     = nom du projet dans le Vault ;
#   rosy     = exemple donne par Rosy (« Nexdoor Talent + sourcing/recrutement »,
#              « Halidi + import/export/logistique ») ;
#   synonym  = variante ou traduction directe d'un terme name/rosy ;
#   inferred = deduit du nom, A VALIDER (poids faible : terme de contexte).
# Les projets sans theme connu ne sont proposes que sur mention explicite.
#
# core    : CONCEPTS specifiques (synonymes regroupes) ; il en faut au moins
#           2 distincts, plus un element de contexte.
# context : termes d'appoint, jamais suffisants seuls.
# named   : client/prospect : seul son nom peut le designer.

PROJECT_PROFILES = {
    "activity:nexdoor_talent": {
        "names": {"nexdoor talent": "name"},
        "core": {
            "recrutement": ("rosy", ["recrutement", "recruter", "recruteur", "recruteurs",
                                     "recruiting", "recruiter", "recruiters", "hiring",
                                     "embauche"]),
            "sourcing": ("rosy", ["sourcing", "chasse de tetes", "chasseur de tetes",
                                  "headhunting", "headhunter", "talent acquisition"]),
            "candidats": ("inferred", ["candidat", "candidats", "candidate", "candidates"]),
        },
        "context": {
            "cv": "inferred", "linkedin": "inferred", "entretien": "inferred",
            "commercial": "rosy", "commerciale": "rosy", "prospection": "inferred",
        },
    },
    "activity:recherche_emploi_remote": {
        "names": {"recherche emploi remote": "name"},
        "core": {
            "emploi": ("name", ["emploi", "job", "jobs", "offre d'emploi", "offres d'emploi",
                                "job board"]),
            "remote": ("name", ["remote", "full remote", "teletravail"]),
            "candidature": ("inferred", ["candidature", "candidatures"]),
        },
        "context": {
            "cv": "inferred", "freelance": "inferred", "certification": "inferred",
            "certifications": "inferred", "carriere": "inferred",
            "portfolio": "inferred", "entretien": "inferred",
        },
    },
    "activity:side_businesses": {
        "names": {
            "side business": "name", "side businesses": "name",
            "side hustle": "synonym",
        },
        "core": {},
        "context": {},
    },
    "activity:nexdoor_partners": {
        "names": {"nexdoor partners": "name"},
        "core": {},
        "context": {},
    },
    "vertical:nexdoor_partners:logistique": {
        "names": {"verticale logistique": "name"},
        "core": {
            "logistique": ("name", ["logistique", "logisticien", "supply chain", "entrepot"]),
            "transport": ("rosy", ["transport", "transporteur", "transporteurs", "fret",
                                   "transitaire"]),
            "import-export": ("rosy", ["import-export", "import/export", "douane"]),
        },
        "context": {"livraison": "inferred", "conteneur": "inferred", "camion": "inferred"},
    },
    "client:nexdoor_partners:logistique:halidi_transport_international": {
        "names": {"halidi": "name", "halidi transport": "name",
                  "halidi transport international": "name"},
        "named": True,
    },
    "vertical:nexdoor_partners:neuropsychologues": {
        "names": {"verticale neuropsychologues": "name"},
        "core": {
            "neuropsychologie": ("name", ["neuropsychologue", "neuropsychologues",
                                          "neuropsychologie", "neuropsychologique",
                                          "neuropsychologiques", "neuropsychologist"]),
        },
        "context": {"cabinet": "inferred", "psychologue": "inferred", "bilan": "inferred"},
    },
    "prospect:nexdoor_partners:neuropsychologues:neurosolutions_center": {
        "names": {"neurosolutions": "name", "neurosolutions center": "name"},
        "named": True,
    },
}

# Alias partages entre plusieurs projets : jamais decisifs seuls.
SHARED_ALIASES = {
    "nexdoor": ("activity:nexdoor_partners", "activity:nexdoor_talent"),
}

# Outils, canaux et themes transversaux : jamais un signal de projet.
GENERIC_TERMS = frozenset({
    "ia", "ai", "chatgpt", "claude", "claude code", "codex", "llm", "gpt",
    "automation", "automatisation", "automatiser", "agent", "agents", "agent ia",
    "prompt", "prompts", "outil", "outils", "tool", "tools", "saas", "business",
    "startup", "github", "notion", "api", "no-code", "workflow", "productivite",
})

WEIGHTS = {
    "name": 0.85,            # nom complet du projet ou du client/prospect
    "shared_alias": 0.40,    # « Nexdoor » seul : plusieurs projets possibles
    "core": 0.30,            # par terme specifique distinct (max 3)
    "core_ai_only": 0.15,    # terme vu seulement dans le resume IA (seconde main)
    "context": 0.10,         # par terme de contexte distinct (max 2)
    "classification": 0.15,  # tag / sous-theme / categorie du Vault = terme du profil
    "proximity": 0.25,       # par source deja liee au projet et proche (max 2)
    "active": 0.05,          # projet marque « Maintenant »
}
MEDIUM_MIN = 0.55
HIGH_MIN = 0.80
AMBIGUITY_GAP = 0.15

PRIMARY_FIELDS = ("title", "description_manual", "description_original", "transcript")
SECOND_HAND_FIELDS = ("description_ai",)
CLASSIFICATION_FIELDS = ("tags", "subthemes", "category")

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


def _excerpt(text, match, width=60):
    start = max(0, match.start() - width)
    end = min(len(text), match.end() + width)
    return ("..." if start else "") + text[start:end].strip() + ("..." if end < len(text) else "")


def _find(term, fields):
    """Premiere occurrence du terme par champ : {champ: extrait}."""
    pattern = _term_pattern(term)
    found = {}
    for field, text in fields.items():
        match = pattern.search(text)
        if match:
            found[field] = _excerpt(text, match)
    return found


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


def load_projects(conn):
    """Projets connus du Vault, relies a leur profil par canonical_key."""
    import mentor_proactive as proactive

    view = proactive.VaultView(conn)
    projects = {}
    for entity_id in view.project_entities():
        row = conn.execute(
            "SELECT id, entity_type, canonical_key, name, attention_status "
            "FROM intelligence_entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        entity = dict(row)
        profile = PROJECT_PROFILES.get(entity["canonical_key"])
        if profile is None:
            # Projet sans profil : seul son nom exact peut le designer.
            profile = {"names": {entity["name"].lower(): "name"}, "core": {}, "context": {}}
        entity["profile"] = profile
        entity["parent_entity_id"] = view.entity(entity_id)["parent_entity_id"]
        projects[entity_id] = entity
    return projects


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
                source["tags"] = tags
                source["subthemes"] = subthemes
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


def _source_entities(conn, generic_names):
    """Entites specifiques (non generiques) citees par chaque source."""
    result = {}
    if not _table_exists(conn, "intelligence_entity_sources"):
        return result
    for source_id, entity_id, name in conn.execute(
        "SELECT es.source_id, e.id, e.name FROM intelligence_entity_sources es "
        "JOIN intelligence_entities e ON e.id = es.entity_id WHERE es.role <> 'primary'"
    ).fetchall():
        if fold(name) in generic_names:
            continue
        result.setdefault(source_id, {})[entity_id] = name
    return result


# ------------------------------------------------------------------ analyse

def _signals_for(project, source, *, proximity, sensitive):
    profile = project["profile"]
    fields = source["fields"]
    primary = {k: v for k, v in fields.items() if k in PRIMARY_FIELDS}
    second_hand = {k: v for k, v in fields.items() if k in SECOND_HAND_FIELDS}
    classification = {k: v for k, v in fields.items() if k in CLASSIFICATION_FIELDS}
    signals = []

    for term, provenance in profile.get("names", {}).items():
        found = _find(term, {**primary, **second_hand})
        if found:
            signals.append({"kind": "name", "term": term, "provenance": provenance,
                            "fields": found, "weight": WEIGHTS["name"]})
    if any(s["kind"] == "name" for s in signals):
        signals = [max(
            (s for s in signals if s["kind"] == "name"), key=lambda s: len(s["term"])
        )]

    if not profile.get("named"):
        # Un signal par CONCEPT (les synonymes d'une meme idee ne s'additionnent pas).
        concepts = 0
        for concept, (provenance, terms) in profile.get("core", {}).items():
            if concepts >= 3:
                break
            best = None
            for term in sorted(terms, key=len, reverse=True):
                if fold(term) in GENERIC_TERMS:
                    continue
                in_primary = _find(term, primary)
                in_ai = _find(term, second_hand)
                if in_primary:
                    best = ("core", term, in_primary)
                    break
                if in_ai and best is None:
                    best = ("core_ai_only", term, in_ai)
            if best:
                kind, term, found = best
                signals.append({"kind": kind, "concept": concept, "term": term,
                                "provenance": provenance, "fields": found,
                                "weight": WEIGHTS[kind]})
                concepts += 1

        context = []
        for term, provenance in profile.get("context", {}).items():
            found = _find(term, {**primary, **second_hand, **classification})
            if found and fold(term) not in GENERIC_TERMS:
                context.append({"kind": "context", "term": term, "provenance": provenance,
                                "fields": found, "weight": WEIGHTS["context"]})
        signals.extend(context[:2])

        for concept, (provenance, terms) in profile.get("core", {}).items():
            hit = next(((t, f) for t in terms for f in [_find(t, classification)] if f), None)
            if hit:
                signals.append({"kind": "classification", "concept": concept,
                                "term": hit[0], "provenance": provenance,
                                "fields": hit[1], "weight": WEIGHTS["classification"]})
                break

    for item in proximity[:2]:
        signals.append({"kind": "proximity", "weight": WEIGHTS["proximity"], **item})
    if project["attention_status"] == "now":
        signals.append({"kind": "active", "weight": WEIGHTS["active"]})
    return signals


def _level(project, signals, sensitive):
    kinds = [s["kind"] for s in signals]
    confidence = _noisy_or(s["weight"] for s in signals)
    core_count = sum(1 for k in kinds if k in ("core", "core_ai_only"))
    strong_core = kinds.count("core")
    support = ("context" in kinds or "classification" in kinds or "proximity" in kinds)

    if not signals:
        return None, 0.0, None
    if "name" in kinds:
        return "high", max(confidence, WEIGHTS["name"]), "mention explicite du projet"
    if sensitive:
        return "low", confidence, "source sensible (sante/finance/droit) sans mention explicite du projet"
    # Un troisieme concept distinct vaut element de contexte.
    if core_count >= 2 and strong_core >= 1 and (support or core_count >= 3):
        independent = len({k for k in kinds if k in ("core", "classification", "proximity")})
        if independent >= 3 and confidence >= HIGH_MIN:
            return "high", confidence, "plusieurs signaux independants coherents"
        if confidence >= MEDIUM_MIN:
            return "medium", confidence, "theme du projet present avec des elements de contexte"
        return "low", confidence, "theme present mais signaux trop faibles"
    if core_count >= 2:
        return "low", confidence, "theme du projet sans element de contexte"
    if core_count == 1:
        return "low", confidence, "un seul terme du projet : simple mot-cle"
    if signals:
        return "low", confidence, "seulement des termes de contexte"
    return None, 0.0, None


def analyze(conn):
    """Calcule toutes les propositions. Lecture seule."""
    import mentor_proactive as proactive

    projects = load_projects(conn)
    sources = load_sources(conn)
    linked = _linked_sources(conn)
    generic_names = set(GENERIC_TERMS)
    source_entities = _source_entities(conn, generic_names)
    view = proactive.VaultView(conn)
    by_key = {p["canonical_key"]: pid for pid, p in projects.items()}

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
                    proximity.append({
                        "source_id": other,
                        "shared_entities": [mine[e] for e in shared],
                    })
            signals = _signals_for(project, source, proximity=proximity, sensitive=sensitive)
            level, confidence, reason = _level(project, signals, sensitive)
            if level is None:
                continue
            candidates.append({
                "source_id": sid, "entity_id": pid, "project": project["name"],
                "project_type": project["entity_type"], "level": level,
                "confidence": round(confidence, 3), "reason": reason,
                "signals": signals,
            })

        # « Nexdoor » seul : plusieurs projets possibles.
        for alias, keys in SHARED_ALIASES.items():
            found = _find(alias, {k: v for k, v in source["fields"].items()
                                  if k not in CLASSIFICATION_FIELDS})
            named = {c["entity_id"] for c in candidates if c["level"] == "high"}
            ids = [by_key[k] for k in keys if k in by_key]
            if found and not named & set(ids) and ids:
                candidates.append({
                    "source_id": sid, "entity_id": ids[0], "project": " / ".join(
                        projects[i]["name"] for i in ids),
                    "project_type": "shared_alias", "level": "ambiguous",
                    "confidence": WEIGHTS["shared_alias"],
                    "reason": f"alias '{alias}' partage par plusieurs projets",
                    "signals": [{"kind": "shared_alias", "term": alias, "fields": found,
                                 "weight": WEIGHTS["shared_alias"]}],
                    "competitors": ids,
                })

        _resolve_competition(candidates, projects)
        source["sensitive"] = sensitive
        results.append({"source": source, "candidates": candidates})
    return {"projects": projects, "results": results}


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
    "core": "terme du projet",
    "core_ai_only": "terme du projet (resume IA seulement, seconde main)",
    "context": "element de contexte",
    "classification": "classement du Vault",
    "proximity": "proche d'une source deja liee au projet",
    "active": "projet marque Maintenant",
}
PROVENANCE_LABELS = {
    "name": "nom du projet", "rosy": "exemple de Rosy",
    "synonym": "synonyme", "inferred": "deduit, a valider",
}


def explain(candidate):
    lines = []
    for signal in candidate["signals"]:
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
        provenance = PROVENANCE_LABELS.get(signal.get("provenance"), "")
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
        (s["kind"], s.get("term") or str(s.get("source_id") or "")) for s in candidate["signals"]
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
