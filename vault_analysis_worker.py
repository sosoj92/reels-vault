#!/usr/bin/env python3
"""
vault_analysis_worker.py - Worker non bloquant d'analyse semantique V3.

Ce worker est volontairement separe de l'ingestion :
- inbox.bat reste rapide et fiable ;
- seules les sources encore non analysees sont traitees ;
- le fingerprint de vault_analysis.py garantit l'idempotence ;
- un echec est mis en cooldown pour eviter de marteler Ollama ;
- Mistral n'est jamais utilise implicitement ;
- apres au moins une analyse reussie, le refresh V3/Notion peut etre relance.

Usage :
    python vault_analysis_worker.py --db .\vault.db --vault . --limit 2
    python vault_analysis_worker.py --db .\vault.db --vault . --limit 1 --skip-refresh
"""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import vault_analysis as analysis
import vault_intelligence as intelligence
from vault_refresh import refresh


WORKER_SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_analysis_worker_attempts (
    source_id          INTEGER NOT NULL
                       REFERENCES intelligence_sources(id) ON DELETE CASCADE,
    source_fingerprint TEXT NOT NULL,
    failure_count      INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT,
    last_attempt_at    TEXT NOT NULL,
    retry_after        TEXT,
    PRIMARY KEY (source_id, source_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_analysis_worker_retry
    ON intelligence_analysis_worker_attempts(retry_after);
"""


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat(timespec="seconds")


def ensure_worker_schema(conn):
    analysis.ensure_analysis_schema(conn)
    conn.executescript(WORKER_SCHEMA)
    conn.commit()
    return conn


def _attempt_row(conn, source_id, fingerprint):
    row = conn.execute(
        """
        SELECT *
          FROM intelligence_analysis_worker_attempts
         WHERE source_id = ?
           AND source_fingerprint = ?
        """,
        (source_id, fingerprint),
    ).fetchone()
    return dict(row) if row else None


def _retry_is_blocked(row, now):
    if not row or not row.get("retry_after"):
        return False
    try:
        retry_after = datetime.fromisoformat(row["retry_after"])
    except ValueError:
        return False
    if retry_after.tzinfo is None:
        retry_after = retry_after.replace(tzinfo=timezone.utc)
    return retry_after > now


def _record_failure(
    conn,
    *,
    source_id,
    fingerprint,
    error,
    now,
    base_cooldown_hours,
):
    row = _attempt_row(conn, source_id, fingerprint)
    previous_failures = int(row["failure_count"]) if row else 0
    failure_count = previous_failures + 1

    # Backoff borne : 2h, 4h, 8h, puis 12h max par defaut.
    delay_hours = min(
        float(base_cooldown_hours) * (2 ** previous_failures),
        12.0,
    )
    retry_after = now + timedelta(hours=delay_hours)

    conn.execute(
        """
        INSERT INTO intelligence_analysis_worker_attempts(
            source_id, source_fingerprint, failure_count,
            last_error, last_attempt_at, retry_after
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, source_fingerprint) DO UPDATE SET
            failure_count = excluded.failure_count,
            last_error = excluded.last_error,
            last_attempt_at = excluded.last_attempt_at,
            retry_after = excluded.retry_after
        """,
        (
            source_id,
            fingerprint,
            failure_count,
            str(error)[:1000],
            _iso(now),
            _iso(retry_after),
        ),
    )
    conn.commit()
    return {
        "failure_count": failure_count,
        "retry_after": _iso(retry_after),
    }


def _clear_failure(conn, source_id, fingerprint):
    conn.execute(
        """
        DELETE FROM intelligence_analysis_worker_attempts
         WHERE source_id = ?
           AND source_fingerprint = ?
        """,
        (source_id, fingerprint),
    )
    conn.commit()


def run_worker(
    conn,
    provider,
    *,
    limit=2,
    cooldown_hours=2.0,
    scan_limit=500,
    now=None,
):
    """
    Traite un petit lot de sources non analysees.

    Une source en cooldown ne compte pas dans limit. Un rate-limit arrete le lot
    courant ; les autres erreurs sont isolees et le worker poursuit si possible.
    """
    ensure_worker_schema(conn)
    now = now or _utcnow()
    limit = max(1, int(limit))
    scan_limit = max(limit, int(scan_limit))

    candidates = analysis._candidate_source_ids(
        conn,
        provider,
        limit=scan_limit,
    )

    results = []
    attempted = 0
    completed = 0
    cooldown_skipped = 0
    errors = 0

    for source_id in candidates:
        payload = analysis.source_payload(
            conn,
            source_id,
            max_chars=getattr(provider, "max_chars", None),
        )
        fingerprint = analysis.source_fingerprint(payload)
        row = _attempt_row(conn, source_id, fingerprint)

        if _retry_is_blocked(row, now):
            cooldown_skipped += 1
            continue

        if attempted >= limit:
            break

        attempted += 1
        try:
            result = analysis.analyze_source(conn, source_id, provider)
        except analysis.AnalysisRateLimitError as exc:
            errors += 1
            cooldown = _record_failure(
                conn,
                source_id=source_id,
                fingerprint=fingerprint,
                error=exc,
                now=now,
                base_cooldown_hours=cooldown_hours,
            )
            results.append(
                {
                    "source_id": source_id,
                    "status": "error",
                    "error": str(exc),
                    **cooldown,
                }
            )
            break
        except Exception as exc:
            errors += 1
            cooldown = _record_failure(
                conn,
                source_id=source_id,
                fingerprint=fingerprint,
                error=exc,
                now=now,
                base_cooldown_hours=cooldown_hours,
            )
            results.append(
                {
                    "source_id": source_id,
                    "status": "error",
                    "error": str(exc),
                    **cooldown,
                }
            )
            continue

        _clear_failure(conn, source_id, fingerprint)
        if result.get("status") in {"completed", "cached"}:
            completed += 1
        results.append(result)

    return {
        "provider": provider.name,
        "model": getattr(provider, "model", None),
        "candidate_count": len(candidates),
        "attempted": attempted,
        "completed": completed,
        "errors": errors,
        "cooldown_skipped": cooldown_skipped,
        "results": results,
    }


def _append_run_log(vault_path, payload):
    log_dir = Path(vault_path) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "analysis-worker.jsonl"
    line = {
        "ran_at": _iso(_utcnow()),
        **payload,
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Worker non bloquant d'analyse semantique Life Intelligence V3"
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--vault", default=str(Path(__file__).resolve().parent))
    parser.add_argument(
        "--provider",
        default="auto",
        help="auto par defaut ; Mistral uniquement si demande explicitement",
    )
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--cooldown-hours", type=float, default=2.0)
    parser.add_argument("--skip-refresh", action="store_true")
    parser.add_argument("--skip-notion", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    vault_path = Path(args.vault).resolve()
    provider = analysis.provider_from_name(args.provider)

    conn = intelligence.connect(db_path)
    try:
        worker_result = run_worker(
            conn,
            provider,
            limit=args.limit,
            cooldown_hours=args.cooldown_hours,
        )
    finally:
        conn.close()

    refresh_result = {
        "status": "skipped",
        "reason": "no completed analysis",
    }
    if worker_result["completed"] > 0 and not args.skip_refresh:
        refresh_result = {
            "status": "completed",
            "result": refresh(
                vault_path,
                db_path,
                sync_notion=not args.skip_notion,
            ),
        }
    elif args.skip_refresh:
        refresh_result = {
            "status": "skipped",
            "reason": "disabled",
        }

    output = {
        "db": str(db_path),
        "worker": worker_result,
        "refresh": refresh_result,
    }
    _append_run_log(vault_path, output)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
