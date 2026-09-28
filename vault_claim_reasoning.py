#!/usr/bin/env python3
"""
vault_claim_reasoning.py - Convergence / contradiction / remplacement /
reactivation a partir des claims STRUCTURES (Life Intelligence V2).

Regle cardinale : ne jamais declarer une contradiction parce que deux textes
utilisent des mots differents. Une CONTRADICTION exige que TOUTES ces
verifications passent :
    1. meme sujet            (subject_key ou entite identique)
    2. meme predicat         (famille de predicat normalisee)
    3. meme perimetre        (portee/population identique ou absente des deux)
    4. temporalite compatible (pas d'annees differentes explicites)
    5. modalites comparables  (deux affirmations assertees/probables, de type
                               fait ou experience ; pas une opinion vs un fait)
et que les valeurs soient INCOMPATIBLES (polarite opposee sur le meme objet,
ou chiffres qui divergent au-dela de la tolerance).

Si seules certaines conditions passent :
    - portees explicitement differentes  -> rien (ce ne sont pas les memes gens)
    - dates differentes / source plus recente -> "possible_supersession"
    - le reste                           -> "possible_tension" (review)
Une CONVERGENCE exige des sources INDEPENDANTES (sources et auteurs
differents) et des valeurs compatibles.

Tout est tracable : chaque evaluation stocke claim_ids, source_ids et le
detail des verifications. Rien n'est modifie dans les claims eux-memes.
"""

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import v2_guard
import vault_claims as claims_layer

KINDS = ("convergence", "contradiction", "possible_tension",
         "possible_supersession", "reactivation")
NUMERIC_TOLERANCE = 0.25          # 25 % d'ecart relatif = encore compatible
SUPERSESSION_MIN_DAYS = 180       # ecart minimal entre deux observations
REACTIVATION_AGE_DAYS = 180       # "ancien" claim
RECENT_DAYS = 30                  # "recent" claim ou source
FACTUAL_KINDS = {"fact", "personal_experience"}
COMPARABLE_MODALITIES = {"asserted", "probable"}
REPLACEMENT_CUES = ("remplace", "n est plus", "desormais", "nouvelle version",
                    "obsolete", "no longer", "replaced", "deprecated", "now ")

SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_claim_assessments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK (kind IN
                  ('convergence', 'contradiction', 'possible_tension',
                   'possible_supersession', 'reactivation')),
    fingerprint TEXT NOT NULL UNIQUE,
    claim_ids   TEXT NOT NULL,
    source_ids  TEXT NOT NULL,
    subject_key TEXT,
    predicate_key TEXT,
    checks      TEXT NOT NULL DEFAULT '{}',
    summary     TEXT NOT NULL,
    confidence  REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed', 'needs_review', 'confirmed', 'dismissed')),
    stale_at    TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_assessments_kind
    ON intelligence_claim_assessments(kind);
"""


def ensure_schema(conn):
    v2_guard.require_v2_writes(conn, "evaluations des claims")
    claims_layer.ensure_schema(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _now():
    return datetime.now(timezone.utc)


def _parse_date(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_claims(conn):
    cursor = conn.execute(
        """
        SELECT s.*, c.claim_text, src.author AS source_author, src.source_url
          FROM intelligence_claim_structure s
          JOIN intelligence_claims c ON c.id = s.claim_id
          JOIN intelligence_sources src ON src.id = s.source_id
         WHERE s.superseded_at IS NULL
        """
    )
    # Fonctionne avec sqlite3.Row comme avec des tuples.
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Comparaisons elementaires
# ---------------------------------------------------------------------------

def same_subject(a, b):
    if a.get("subject_entity_id") and a.get("subject_entity_id") == b.get("subject_entity_id"):
        return True
    return bool(a["subject_key"]) and a["subject_key"] == b["subject_key"]


def same_predicate(a, b):
    return (a["predicate_key"] != "unspecified"
            and a["predicate_key"] == b["predicate_key"])


def scope_state(a, b):
    """identical | both_unspecified | partial | different"""
    sa, sb = a.get("scope_key"), b.get("scope_key")
    if not sa and not sb:
        return "both_unspecified"
    if sa and sb:
        return "identical" if sa == sb else "different"
    return "partial"


def time_state(a, b):
    """compatible | different_years | undated_gap"""
    ya, yb = a.get("temporal_year"), b.get("temporal_year")
    if ya and yb and ya != yb:
        return "different_years"
    da, db = _parse_date(a.get("observed_at")), _parse_date(b.get("observed_at"))
    if not (ya and yb) and da and db and abs((da - db).days) >= SUPERSESSION_MIN_DAYS:
        return "undated_gap"
    return "compatible"


def modalities_comparable(a, b):
    return (a["modality"] in COMPARABLE_MODALITIES
            and b["modality"] in COMPARABLE_MODALITIES
            and a["claim_kind"] in FACTUAL_KINDS
            and b["claim_kind"] in FACTUAL_KINDS)


def _token_overlap(x, y):
    tx, ty = set((x or "").split()), set((y or "").split())
    if not tx or not ty:
        return 0.0
    return len(tx & ty) / min(len(tx), len(ty))


def compare_values(a, b):
    """compatible | incompatible | unknown (jamais "incompatible" par defaut)."""
    na, nb = a.get("object_number"), b.get("object_number")
    if na is not None and nb is not None:
        if (a.get("object_unit") or None) != (b.get("object_unit") or None):
            return "unknown"
        if a["polarity"] != b["polarity"]:
            return "incompatible"
        biggest = max(abs(na), abs(nb)) or 1.0
        return "compatible" if abs(na - nb) / biggest <= NUMERIC_TOLERANCE else "incompatible"
    overlap = _token_overlap(a.get("object_key"), b.get("object_key"))
    if not a.get("object_key") and not b.get("object_key"):
        overlap = 1.0  # predicat seul ("X est gratuit" / "X n'est pas gratuit")
    if overlap >= 0.5:
        return "compatible" if a["polarity"] == b["polarity"] else "incompatible"
    return "unknown"


def independent(a, b):
    if a["source_id"] == b["source_id"]:
        return False
    if a.get("source_url") and a.get("source_url") == b.get("source_url"):
        return False
    author_a = claims_layer._fold(a.get("source_author") or "")
    author_b = claims_layer._fold(b.get("source_author") or "")
    return not (author_a and author_a == author_b)


def _has_replacement_cue(claim):
    folded = claims_layer._fold(claim.get("evidence_excerpt")) + " "
    return any(claims_layer._has_cue(folded, cue.strip()) for cue in REPLACEMENT_CUES)


def _newer(a, b):
    """Retourne (ancien, recent) si l'ordre est etabli, sinon None."""
    ya, yb = a.get("temporal_year"), b.get("temporal_year")
    if ya and yb and ya != yb:
        return (a, b) if ya < yb else (b, a)
    da, db = _parse_date(a.get("observed_at")), _parse_date(b.get("observed_at"))
    if da and db and da != db:
        return (a, b) if da < db else (b, a)
    return None


def classify_pair(a, b):
    """
    Evalue une paire de claims. Retourne None (rien a signaler) ou
    {"kind", "checks", "confidence"}.
    """
    checks = {
        "same_subject": same_subject(a, b),
        "same_predicate": same_predicate(a, b),
        "scope": scope_state(a, b),
        "time": time_state(a, b),
        "modalities_comparable": modalities_comparable(a, b),
        "values": compare_values(a, b),
        "independent_sources": independent(a, b),
    }
    if not (checks["same_subject"] and checks["same_predicate"]):
        return None
    if a["source_id"] == b["source_id"]:
        return None
    # Populations / perimetres explicitement differents : ce ne sont pas les
    # memes affirmations. Ni contradiction, ni convergence.
    if checks["scope"] == "different":
        return None

    values = checks["values"]
    confidence = round(min(a["confidence"], b["confidence"]), 3)

    if values == "compatible":
        if checks["independent_sources"] and checks["time"] != "different_years":
            return {"kind": "convergence", "checks": checks, "confidence": confidence}
        return None

    if values == "incompatible":
        order = _newer(a, b)
        if checks["time"] in ("different_years", "undated_gap") and order:
            return {"kind": "possible_supersession", "checks": checks,
                    "confidence": round(confidence * 0.8, 3), "order": order}
        all_pass = (
            checks["scope"] in ("identical", "both_unspecified")
            and checks["time"] == "compatible"
            and checks["modalities_comparable"]
        )
        if all_pass:
            return {"kind": "contradiction", "checks": checks, "confidence": confidence}
        return {"kind": "possible_tension", "checks": checks,
                "confidence": round(confidence * 0.6, 3)}

    # Valeurs non comparables : on ne conclut rien, sauf marqueur explicite de
    # remplacement dans la source la plus recente.
    order = _newer(a, b)
    if order and _has_replacement_cue(order[1]):
        return {"kind": "possible_supersession", "checks": checks,
                "confidence": round(confidence * 0.6, 3), "order": order}
    return None


# ---------------------------------------------------------------------------
# Moteur
# ---------------------------------------------------------------------------

def _fingerprint(kind, claim_ids):
    raw = f"{kind}:{','.join(str(i) for i in sorted(claim_ids))}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _summary(kind, claims, order=None):
    def short(claim):
        text = claim["claim_text"]
        return text if len(text) <= 90 else text[:87].rsplit(" ", 1)[0] + "..."

    if kind == "convergence":
        return (f"{len({c['source_id'] for c in claims})} sources indépendantes "
                f"soutiennent : « {short(claims[0])} »")
    if kind == "contradiction":
        return f"« {short(claims[0])} » vs « {short(claims[1])} »"
    if kind == "possible_tension":
        return (f"« {short(claims[0])} » / « {short(claims[1])} » "
                "(conditions de comparaison non toutes réunies)")
    if kind == "possible_supersession":
        old, new = order
        return f"« {short(new)} » pourrait remplacer « {short(old)} »"
    if kind == "reactivation":
        return f"« {short(claims[0])} » redevient d'actualité"
    return short(claims[0])


def _status(kind, claims):
    # Une contradiction, une tension ou un remplacement ne sont jamais
    # "confirmes" automatiquement. Une convergence sur un sujet sensible non plus.
    if kind in ("contradiction", "possible_tension", "possible_supersession"):
        return "needs_review"
    if any(c.get("sensitivity") for c in claims):
        return "needs_review"
    return "proposed"


def assess(conn, now=None):
    """
    Recalcule toutes les evaluations. Idempotent : une evaluation deja connue
    est mise a jour ; celle qui ne tient plus est marquee stale_at (jamais
    supprimee).
    """
    ensure_schema(conn)
    now = now or _now()
    claims = load_claims(conn)
    by_group = {}
    for claim in claims:
        by_group.setdefault(claim["predicate_key"], []).append(claim)

    found = {}
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for group in by_group.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                verdict = classify_pair(a, b)
                if verdict is None:
                    continue
                if verdict["kind"] == "convergence":
                    parent[find(a["claim_id"])] = find(b["claim_id"])
                    continue
                ids = [a["claim_id"], b["claim_id"]]
                found[_fingerprint(verdict["kind"], ids)] = {
                    "kind": verdict["kind"],
                    "claims": [a, b],
                    "checks": verdict["checks"],
                    "confidence": verdict["confidence"],
                    "order": verdict.get("order"),
                }

    # Convergences : un groupe par composante, >= 2 sources independantes.
    clusters = {}
    by_id = {c["claim_id"]: c for c in claims}
    for claim_id in list(parent):
        clusters.setdefault(find(claim_id), []).append(by_id[claim_id])
    for members in clusters.values():
        sources = {m["source_id"] for m in members}
        if len(sources) < 2:
            continue
        ids = [m["claim_id"] for m in members]
        mean_conf = sum(m["confidence"] for m in members) / len(members)
        found[_fingerprint("convergence", ids)] = {
            "kind": "convergence",
            "claims": members,
            "checks": {"independent_sources": len(sources),
                       "same_subject": True, "same_predicate": True,
                       "values": "compatible"},
            "confidence": round(min(1.0, (0.4 + 0.15 * len(sources)) * mean_conf / 0.7), 3),
        }
        # Reactivation : un ancien claim rejoint par une source recente.
        dated = [(claims_layer_date(m), m) for m in members]
        dated = [(d, m) for d, m in dated if d]
        if dated:
            oldest = min(dated, key=lambda item: item[0])
            newest = max(dated, key=lambda item: item[0])
            if ((now - oldest[0]).days >= REACTIVATION_AGE_DAYS
                    and (now - newest[0]).days <= RECENT_DAYS):
                rids = [oldest[1]["claim_id"], newest[1]["claim_id"]]
                found[_fingerprint("reactivation", rids)] = {
                    "kind": "reactivation",
                    "claims": [oldest[1], newest[1]],
                    "checks": {"old_observed_at": oldest[1]["observed_at"],
                               "recent_observed_at": newest[1]["observed_at"]},
                    "confidence": round(min(oldest[1]["confidence"],
                                            newest[1]["confidence"]), 3),
                }

    # Reactivation par l'attention : ancien claim dont l'entite est "now".
    for claim in claims:
        entity_id = claim.get("subject_entity_id")
        observed = claims_layer_date(claim)
        if not entity_id or not observed or (now - observed).days < REACTIVATION_AGE_DAYS:
            continue
        row = conn.execute(
            "SELECT attention_status FROM intelligence_entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if row and row[0] == "now":
            found[_fingerprint("reactivation", [claim["claim_id"]])] = {
                "kind": "reactivation",
                "claims": [claim],
                "checks": {"entity_attention": "now", "old_observed_at": claim["observed_at"]},
                "confidence": round(claim["confidence"] * 0.8, 3),
            }

    stamp = now.isoformat()
    existing = {
        row[0]: row[1]
        for row in conn.execute("SELECT fingerprint, id FROM intelligence_claim_assessments")
    }
    written = {kind: 0 for kind in KINDS}
    for fingerprint, item in found.items():
        claim_ids = sorted({c["claim_id"] for c in item["claims"]})
        source_ids = sorted({c["source_id"] for c in item["claims"]})
        summary = _summary(item["kind"], item["claims"], item.get("order"))
        subject_key = item["claims"][0]["subject_key"]
        predicate_key = item["claims"][0]["predicate_key"]
        checks = json.dumps(item["checks"], ensure_ascii=False, sort_keys=True)
        if fingerprint in existing:
            conn.execute(
                """
                UPDATE intelligence_claim_assessments
                   SET checks = ?, summary = ?, confidence = ?, stale_at = NULL, updated_at = ?
                 WHERE fingerprint = ?
                """,
                (checks, summary, item["confidence"], stamp, fingerprint),
            )
        else:
            conn.execute(
                """
                INSERT INTO intelligence_claim_assessments(
                    kind, fingerprint, claim_ids, source_ids, subject_key, predicate_key,
                    checks, summary, confidence, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item["kind"], fingerprint, json.dumps(claim_ids), json.dumps(source_ids),
                 subject_key, predicate_key, checks, summary, item["confidence"],
                 _status(item["kind"], item["claims"]), stamp, stamp),
            )
        written[item["kind"]] += 1

    stale = set(existing) - set(found)
    for fingerprint in stale:
        conn.execute(
            "UPDATE intelligence_claim_assessments SET stale_at = ?, updated_at = ? "
            "WHERE fingerprint = ? AND stale_at IS NULL",
            (stamp, stamp, fingerprint),
        )
    conn.commit()
    return {"claims_considered": len(claims), "assessments": written, "stale": len(stale)}


def claims_layer_date(claim):
    return _parse_date(claim.get("observed_at"))


def assessments_for_sources(conn, source_ids, limit=6):
    """Evaluations actives touchant ces sources (lecture seule, pour Ask)."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='intelligence_claim_assessments'"
    ).fetchone()
    if not exists or not source_ids:
        return []
    wanted = set(int(s) for s in source_ids)
    rows = conn.execute(
        """
        SELECT id, kind, claim_ids, source_ids, summary, confidence, status, checks
          FROM intelligence_claim_assessments
         WHERE stale_at IS NULL AND status <> 'dismissed'
         ORDER BY CASE kind WHEN 'contradiction' THEN 0 WHEN 'possible_supersession' THEN 1
                  WHEN 'convergence' THEN 2 ELSE 3 END, confidence DESC
        """
    ).fetchall()
    out = []
    for row in rows:
        item = dict(zip(("id", "kind", "claim_ids", "source_ids", "summary",
                         "confidence", "status", "checks"), tuple(row)))
        item["claim_ids"] = json.loads(item["claim_ids"])
        item["source_ids"] = json.loads(item["source_ids"])
        item["checks"] = json.loads(item["checks"] or "{}")
        if wanted & set(item["source_ids"]):
            out.append(item)
        if len(out) >= limit:
            break
    return out


def main():
    parser = argparse.ArgumentParser(description="Convergences / contradictions (V2)")
    parser.add_argument("--db", required=True)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    conn = sqlite3.connect(args.db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        if args.list:
            # Lecture seule : ne cree aucune table.
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='intelligence_claim_assessments'"
            ).fetchone()
            result = [dict(r) for r in conn.execute(
                "SELECT * FROM intelligence_claim_assessments WHERE stale_at IS NULL")] if exists else []
        else:
            result = assess(conn)
    except v2_guard.V2WritesBlocked as exc:
        result = {"status": "blocked", "reason": str(exc)}
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
