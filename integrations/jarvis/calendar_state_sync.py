import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

from core.config import reglage
from tools import agenda


def _vault_root():
    configured = (
        reglage("integrations.life_intelligence_vault", "")
        or ""
    ).strip()

    if configured:
        return Path(configured).expanduser().resolve()

    return (Path.home() / "reels-vault").resolve()


def _db():
    return _vault_root() / "vault.db"


def _refs(value):
    try:
        refs = json.loads(value or "[]")
        return refs if isinstance(refs, list) else []
    except Exception:
        return []


def inspect_changes():
    conn = sqlite3.connect(str(_db()))
    conn.row_factory = sqlite3.Row

    service = agenda._service()

    changes = []

    try:
        rows = conn.execute("""
            SELECT
                a.id,
                a.title,
                p.calendar_event_ids,
                p.scheduled_start_at,
                p.scheduled_end_at
            FROM actions a
            JOIN intelligence_action_planning p
              ON p.action_id = a.id
            WHERE p.planning_status = 'planned'
            ORDER BY a.id
        """).fetchall()

        for row in rows:
            refs = _refs(row["calendar_event_ids"])

            if not refs:
                continue

            event_id = refs[0]

            try:
                event = service.events().get(
                    calendarId="primary",
                    eventId=event_id
                ).execute()
            except Exception:
                continue

            actual_start = (
                event.get("start", {}).get("dateTime")
                or event.get("start", {}).get("date")
            )

            actual_end = (
                event.get("end", {}).get("dateTime")
                or event.get("end", {}).get("date")
            )

            if (
                actual_start != row["scheduled_start_at"]
                or actual_end != row["scheduled_end_at"]
            ):
                changes.append({
                    "action_id": row["id"],
                    "title": row["title"],
                    "event_id": event_id,
                    "old_start": row["scheduled_start_at"],
                    "old_end": row["scheduled_end_at"],
                    "new_start": actual_start,
                    "new_end": actual_end,
                })

        return changes

    finally:
        conn.close()


def apply_changes(changes):
    if not changes:
        return 0

    conn = sqlite3.connect(str(_db()))

    now = dt.datetime.now(dt.timezone.utc).isoformat()

    try:
        for change in changes:
            conn.execute("""
                UPDATE intelligence_action_planning
                SET scheduled_start_at = ?,
                    scheduled_end_at = ?,
                    calendar_synced_at = ?,
                    synced_at = ?
                WHERE action_id = ?
            """, (
                change["new_start"],
                change["new_end"],
                now,
                now,
                change["action_id"],
            ))

        conn.commit()
        return len(changes)

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--confirm",
        action="store_true"
    )

    args = parser.parse_args()

    changes = inspect_changes()

    print(json.dumps({
        "writes_performed": bool(args.confirm and changes),
        "change_count": len(changes),
        "changes": changes,
    }, ensure_ascii=False, indent=2))

    if args.confirm and changes:
        apply_changes(changes)


if __name__ == "__main__":
    main()
