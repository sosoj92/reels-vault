import datetime as dt
import sqlite3
from pathlib import Path

from core.config import reglage
from core.registre import outil
from tools.mentor_calendar import build_horizon
from tools.dynamic_planner import _find_slot
from tools.replanning_watch import detect_replanning


def _vault_root():
    configured = (
        reglage("integrations.life_intelligence_vault", "")
        or ""
    ).strip()

    if configured:
        return Path(configured).expanduser().resolve()

    return (Path.home() / "reels-vault").resolve()


def _task(action_id):
    conn = sqlite3.connect(str(_vault_root() / "vault.db"))
    conn.row_factory = sqlite3.Row

    try:
        row = conn.execute("""
            SELECT
                a.id,
                a.title,
                a.due_date,
                p.duration_minutes,
                p.calendar_event_ids
            FROM actions a
            JOIN intelligence_action_planning p
              ON p.action_id = a.id
            WHERE a.id = ?
        """, (action_id,)).fetchone()

        return dict(row) if row else None

    finally:
        conn.close()


def propose_replanning(days=14):
    issues = detect_replanning(days)

    if not issues:
        return []

    horizon = build_horizon(days)
    events = horizon["calendar_events"]

    local_tz = dt.datetime.now().astimezone().tzinfo
    today = dt.datetime.now(local_tz).date()
    horizon_end = today + dt.timedelta(days=days)

    replan_types = {
        "calendar_event_missing",
        "calendar_conflict",
        "elapsed_block_open_task",
        "after_deadline",
        "missing_calendar_ref",
    }

    sync_types = {
        "calendar_moved",
        "calendar_duration_changed",
    }

    release_types = {
        "completed_with_future_block",
    }

    proposals = []
    seen = set()

    for issue in issues:
        action_id = issue["action_id"]
        issue_type = issue.get("type")

        key = (action_id, issue_type)

        if key in seen:
            continue

        seen.add(key)

        task = _task(action_id)

        if not task:
            continue

        if issue_type in sync_types:
            proposals.append({
                "action_id": action_id,
                "title": task["title"],
                "decision": "sync_calendar_to_vault",
                "reason": issue["message"],
                "event_id": issue.get("event_id"),
            })
            continue

        if issue_type in release_types:
            proposals.append({
                "action_id": action_id,
                "title": task["title"],
                "decision": "release_future_block",
                "reason": issue["message"],
                "event_id": issue.get("event_id"),
            })
            continue

        if issue_type not in replan_types:
            continue

        duration = task.get("duration_minutes") or 45

        due = task.get("due_date")

        if due:
            try:
                last_date = min(
                    dt.date.fromisoformat(str(due)[:10]),
                    horizon_end,
                )
            except Exception:
                last_date = horizon_end
        else:
            last_date = horizon_end

        own_ids = set()

        try:
            import json
            own_ids = set(
                json.loads(task.get("calendar_event_ids") or "[]")
            )
        except Exception:
            pass

        filtered_events = [
            e for e in events
            if e.get("event_id") not in own_ids
        ]

        slot = _find_slot(
            filtered_events,
            today,
            last_date,
            int(duration),
            local_tz,
            9,
            19,
            15,
            [],
        )

        proposals.append({
            "action_id": action_id,
            "title": task["title"],
            "decision": "replan",
            "reason": issue["message"],
            "start": slot[0] if slot else None,
            "end": slot[1] if slot else None,
            "duration": int(duration),
        })

    return proposals


def text(days=14):
    proposals = propose_replanning(days)

    lines = [
        "REPLANNING PROPOSER ? PROPOSITIONS UNIQUEMENT",
        ""
    ]

    if not proposals:
        lines.append(
            "Aucune replanification n?cessaire."
        )
        return "\n".join(lines)

    for p in proposals:
        decision = p["decision"]

        if decision == "sync_calendar_to_vault":
            lines.append(
                f"- {p['title']} : d?placement humain d?tect? ? "
                "synchroniser le nouvel horaire Calendar vers le Vault."
            )
            continue

        if decision == "release_future_block":
            lines.append(
                f"- {p['title']} : t?che termin?e ? "
                "lib?rer le bloc Calendar futur apr?s validation."
            )
            continue

        if decision == "replan":
            if not p["start"]:
                lines.append(
                    f"- {p['title']} : aucun nouveau cr?neau trouv?."
                )
                continue

            lines.append(
                f"- {p['title']} ? nouveau cr?neau propos? : "
                f"{p['start'].strftime('%d/%m %H:%M')}?"
                f"{p['end'].strftime('%H:%M')} "
                f"({p['duration']} min)"
            )

    lines.extend([
        "",
        "Aucune modification Calendar ou Vault n'a ?t? effectu?e."
    ])

    return "\n".join(lines)


@outil(
    nom="replanning_proposer",
    description=(
        "Propose un nouveau créneau lorsqu'un écart de planning "
        "est détecté, sans modifier Google Calendar."
    ),
    parametres={
        "type": "object",
        "properties": {
            "jours": {
                "type": "integer"
            }
        }
    },
    lent=True,
    phrase_attente="Je cherche un nouveau créneau.",
    mcp_expose=False,
)
def replanning_proposer(jours: int = 14):
    return text(jours)


if __name__ == "__main__":
    print(text(14))
