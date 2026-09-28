"""
mentor_consensus.py - Double analyse Agency pour les cas limites (V2).

Probleme V1 : qwen3:1.7b donne des scores non reproductibles sur les sources
limites (source 64 : 0.65/0.75 a froid, 0.45/0.45 a chaud). Un verdict unique
proche du seuil n'est donc pas fiable.

REGLES (explicites, testees) :

1. Cas BORDERLINE apres la premiere analyse Agency, si au moins une condition :
   - |score_final - SEUIL_PUBLICATION| <= MARGE_SCORE        (0.60 +/- 0.10)
   - |confiance_agency - SEUIL_CONFIANCE| <= MARGE_CONFIANCE (0.60 +/- 0.10)
   - la classification de Qwen et celle d'Agency different ;
   - Agency demande une verification (needs_cloud) mais la decision ne
     l'escalade pas (signal contradictoire).
   Hors borderline : decision V1 inchangee (une seule analyse).

2. Cas borderline : seconde analyse Agency INDEPENDANTE (mentor_agency
   variant=2). ACCORD SUFFISANT si toutes les conditions :
   - meme classification ;
   - |relevance_1 - relevance_2| <= ECART_MAX_RELEVANCE (0.15) ;
   - |confidence_1 - confidence_2| <= ECART_MAX_CONFIANCE (0.20) ;
   - les deux confiances >= CONFIANCE_MIN_RUN (0.55).

3. PUBLICATION seulement si accord suffisant ET decision publiable calculee
   sur les valeurs CONSERVATRICES du consensus (relevance = min, confiance =
   min, needs_cloud = ou logique des deux runs -> meme regle mentor_decision).
   Sinon : review_required, avec la raison exacte.

On prefere toujours : review > faux insight.
Ce code ne tourne que dans le Mentor Worker (batch), jamais dans Ask.
"""

import mentor_decision

SEUIL_PUBLICATION = 0.60
SEUIL_CONFIANCE = 0.60
MARGE_SCORE = 0.10
MARGE_CONFIANCE = 0.10
ECART_MAX_RELEVANCE = 0.15
ECART_MAX_CONFIANCE = 0.20
CONFIANCE_MIN_RUN = 0.55


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def borderline_reasons(qwen, agency, decision):
    """Raisons qui rendent le verdict d'une seule analyse fragile ([] sinon)."""
    if not agency:
        return []
    reasons = []
    if abs(_f(decision.get("relevance_score")) - SEUIL_PUBLICATION) <= MARGE_SCORE:
        reasons.append("score_near_threshold")
    if abs(_f(agency.get("confidence")) - SEUIL_CONFIANCE) <= MARGE_CONFIANCE:
        reasons.append("confidence_near_threshold")
    if qwen and qwen.get("classification") and (
        qwen.get("classification") != agency.get("classification")
    ):
        reasons.append("qwen_agency_disagree")
    if agency.get("needs_cloud") and not decision.get("needs_cloud"):
        reasons.append("agency_requested_verification")
    return reasons


def agreement(run1, run2):
    """Retourne (accord: bool, details, raisons_de_desaccord)."""
    if not run1 or not run2:
        return False, {}, ["second_run_failed" if run1 else "first_run_failed"]
    details = {
        "same_classification": run1.get("classification") == run2.get("classification"),
        "relevance_gap": round(abs(_f(run1.get("relevance")) - _f(run2.get("relevance"))), 3),
        "confidence_gap": round(abs(_f(run1.get("confidence")) - _f(run2.get("confidence"))), 3),
        "min_confidence": round(min(_f(run1.get("confidence")), _f(run2.get("confidence"))), 3),
    }
    reasons = []
    if not details["same_classification"]:
        reasons.append("classification_disagreement")
    if details["relevance_gap"] > ECART_MAX_RELEVANCE:
        reasons.append("relevance_gap_too_large")
    if details["confidence_gap"] > ECART_MAX_CONFIANCE:
        reasons.append("confidence_gap_too_large")
    if details["min_confidence"] < CONFIANCE_MIN_RUN:
        reasons.append("low_confidence_run")
    return not reasons, details, reasons


def build_consensus(*, heuristic_score, current_outcome, qwen, run1, run2, needs_agency):
    """
    Decision finale d'un cas borderline. Retourne un dict compatible avec
    mentor_decision.finalize, enrichi de la trace de consensus.
    """
    agreed, details, disagreement = agreement(run1, run2)
    trace = {
        "agency_run_1": _public(run1),
        "agency_run_2": _public(run2),
        "agreement": details,
        "agreed": agreed,
    }
    if not agreed:
        base = mentor_decision.finalize(
            heuristic_score=heuristic_score,
            current_outcome=current_outcome,
            qwen=qwen,
            agency=run1,
            needs_agency=needs_agency,
        )
        return {
            **base,
            "publishable": False,
            "review_required": True,
            "review_reason": "agency_disagreement:" + ",".join(disagreement),
            "anti_noise_reasons": sorted(set(base["anti_noise_reasons"]) | {"agency_disagreement"}),
            "consensus": {**trace, "decision": "review"},
        }

    conservative = {
        "classification": run1.get("classification"),
        "relevance": min(_f(run1.get("relevance")), _f(run2.get("relevance"))),
        "confidence": min(_f(run1.get("confidence")), _f(run2.get("confidence"))),
        "needs_cloud": bool(run1.get("needs_cloud")) or bool(run2.get("needs_cloud")),
        "routes": run1.get("routes", []),
    }
    decision = mentor_decision.finalize(
        heuristic_score=heuristic_score,
        current_outcome=current_outcome,
        qwen=qwen,
        agency=conservative,
        needs_agency=needs_agency,
    )
    review_reason = None
    if not decision["publishable"]:
        review_reason = "consensus_below_publication_rule:" + ",".join(
            decision["anti_noise_reasons"])
    return {
        **decision,
        "review_required": not decision["publishable"],
        "review_reason": review_reason,
        "consensus": {
            **trace,
            "conservative": {k: v for k, v in conservative.items() if k != "routes"},
            "decision": "publish" if decision["publishable"] else "review",
        },
    }


def _public(run):
    if not run:
        return None
    return {
        key: run.get(key)
        for key in ("classification", "relevance", "confidence", "needs_cloud",
                    "agents", "variant")
    }
