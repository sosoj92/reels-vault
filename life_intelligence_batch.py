#!/usr/bin/env python3
"""
life_intelligence_batch.py - Traitements lourds V2, HORS du temps interactif.

Etapes (chacune isolee : l'echec de l'une n'arrete pas les autres) :
1. claims structures pour quelques sources en attente (Qwen local) ;
2. convergences / contradictions / remplacements / reactivations ;
3. index semantique incremental ;
4. revue cloud de la file de review — UNIQUEMENT avec --cloud-review
   (appel payant) et si une cle est configuree.

Ask interactif n'appelle jamais ce script.

    python life_intelligence_batch.py --claims-limit 3
    python life_intelligence_batch.py --claims-limit 0 --cloud-review
"""

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _step(report, name, func):
    started = time.perf_counter()
    import v2_guard

    try:
        report[name] = {"status": "ok", **(func() or {})}
    except v2_guard.V2WritesBlocked as exc:
        report[name] = {"status": "blocked", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - chaque etape est facultative
        report[name] = {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    report[name]["elapsed_s"] = round(time.perf_counter() - started, 1)


def run(db_path, *, claims_limit=3, extractor_name="ollama", cloud_review=False,
        semantic=True):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    report = {"db": str(db_path)}
    try:
        if claims_limit > 0:
            def claims_step():
                import vault_claims

                extractor = vault_claims.extractor_from_name(extractor_name)
                ids = vault_claims.pending_source_ids(conn, extractor, claims_limit)
                results = [vault_claims.extract_source(conn, sid, extractor) for sid in ids]
                return {
                    "sources": len(results),
                    "claims_written": sum(len(r.get("claim_ids", [])) for r in results),
                    "rejected": sum(len(r.get("rejected", [])) for r in results),
                    "failed": [r["source_id"] for r in results if r["status"] == "failed"],
                }

            _step(report, "claims", claims_step)

        def reasoning_step():
            import vault_claim_reasoning

            return vault_claim_reasoning.assess(conn)

        _step(report, "reasoning", reasoning_step)

        if semantic:
            def semantic_step():
                import vault_embeddings

                backend = vault_embeddings.default_backend()
                if backend is None:
                    return {"skipped": "model_unavailable"}
                return vault_embeddings.index_sources(conn, backend)

            _step(report, "semantic_index", semantic_step)

        if cloud_review:
            def review_step():
                import mentor_review

                result = mentor_review.process_queue(conn, limit=5)
                return {k: v for k, v in result.items() if k != "processed"} | {
                    "decisions": [
                        {"source_id": item["source_id"], "status": item["status"]}
                        for item in result.get("processed", [])
                    ]
                }

            _step(report, "cloud_review", review_step)
        else:
            report["cloud_review"] = {"status": "skipped", "reason": "--cloud-review non demande"}
    finally:
        conn.close()
    return report


def main():
    parser = argparse.ArgumentParser(description="Traitements lourds Life Intelligence V2")
    parser.add_argument("--db", default=os.environ.get("REELS_VAULT_DB") or str(ROOT / "vault.db"))
    parser.add_argument("--claims-limit", type=int, default=3)
    parser.add_argument("--extractor", default=os.environ.get("VAULT_CLAIMS_EXTRACTOR", "ollama"))
    parser.add_argument("--cloud-review", action="store_true",
                        help="traiter la file de review avec le provider cloud (payant)")
    parser.add_argument("--skip-semantic", action="store_true")
    args = parser.parse_args()
    if not Path(args.db).exists():
        raise SystemExit(f"Base Vault introuvable : {args.db}")
    print(json.dumps(
        run(args.db, claims_limit=args.claims_limit, extractor_name=args.extractor,
            cloud_review=args.cloud_review, semantic=not args.skip_semantic),
        ensure_ascii=False, indent=2,
    ))


if __name__ == "__main__":
    main()
