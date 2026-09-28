
def finalize(
    *,
    heuristic_score,
    current_outcome,
    qwen=None,
    agency=None,
    needs_agency=False,
):
    reasons = []

    qrel = float(qwen.get("relevance", 0)) if qwen else 0.0
    qconf = float(qwen.get("confidence", 0)) if qwen else 0.0

    # -----------------------------------------------------
    # Agency a ?t? appel?e : elle devient l'arbitre principal
    # -----------------------------------------------------
    if agency:
        arel = float(agency.get("relevance", 0))
        aconf = float(agency.get("confidence", 0))
        outcome = agency.get("classification") or current_outcome

        # Heuristique = signal stable
        # Qwen rapide = signal secondaire
        # Agency = arbitre principal
        score = (
            0.25 * float(heuristic_score)
            + 0.15 * qrel
            + 0.60 * arel
        )

        # Agency remplace la confiance du Qwen rapide.
        final_confidence = aconf

        routes = {
            r.get("route")
            for r in agency.get("routes", [])
        }

        sensitive = bool(
            routes & {
                "research_evidence",
                "finance",
                "legal",
            }
        )

        # Cloud = exception.
        # - confiance Agency r?ellement faible
        # - ou domaine sensible + Agency demande v?rification
        # - ou domaine normal mais Agency reste franchement incertaine
        needs_cloud = (
            aconf < 0.55
            or (
                sensitive
                and bool(agency.get("needs_cloud"))
            )
            or (
                not sensitive
                and bool(agency.get("needs_cloud"))
                and aconf < 0.70
            )
        )

    # -----------------------------------------------------
    # Pas d'Agency
    # -----------------------------------------------------
    elif qwen:
        outcome = current_outcome

        score = (
            0.45 * float(heuristic_score)
            + 0.55 * qrel
        )

        final_confidence = qconf

        # Si Agency ?tait n?cessaire mais n'a pas fonctionn?,
        # on escalade au lieu de publier un r?sultat fragile.
        needs_cloud = bool(needs_agency)

        if qconf < 0.55:
            reasons.append("low_qwen_confidence")

    # -----------------------------------------------------
    # Heuristique seule
    # -----------------------------------------------------
    else:
        outcome = current_outcome
        score = float(heuristic_score)
        final_confidence = 0.50
        needs_cloud = False
        reasons.append("heuristic_only")

    # -----------------------------------------------------
    # Anti-bruit
    # -----------------------------------------------------
    if outcome == "low_signal":
        score = min(score, 0.44)
        reasons.append("low_signal")

    score = round(
        max(0.0, min(1.0, score)),
        3
    )

    final_confidence = round(
        max(0.0, min(1.0, final_confidence)),
        3
    )

    publishable = (
        outcome != "low_signal"
        and score >= 0.60
        and final_confidence >= 0.60
        and not needs_cloud
        and not (
            needs_agency
            and agency is None
        )
    )

    if not publishable:
        if score < 0.60:
            reasons.append("below_relevance_threshold")

        if final_confidence < 0.60:
            reasons.append("below_confidence_threshold")

        if needs_cloud:
            reasons.append("awaiting_cloud")

    return {
        "outcome": outcome,
        "relevance_score": score,
        "final_confidence": final_confidence,
        "needs_cloud": needs_cloud,
        "publishable": publishable,
        "anti_noise_reasons": sorted(set(reasons)),
    }
