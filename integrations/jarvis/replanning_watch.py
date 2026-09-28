import datetime as dt
import json
import sqlite3
from pathlib import Path

from core.config import reglage
from core.registre import outil
from tools.mentor_calendar import build_horizon


def _vault_root():
    configured = (
        reglage("integrations.life_intelligence_vault", "")
        or ""
    ).strip()

    if configured:
        return Path(configured).expanduser().resolve()

    return (Path.home() / "reels-vault").resolve()


def _parse(value):
    if not value:
        return None

    try:
        parsed = dt.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except Exception:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=dt.datetime.now().astimezone().tzinfo
        )

    return parsed.astimezone()


def _vault_tasks():
    conn = sqlite3.connect(
        str(_vault_root() / "vault.db")
    )

    conn.row_factory = sqlite3.Row

    try:
        return conn.execute("""
            SELECT
                a.id,
                a.title,
                a.status,
                a.due_date,

                p.planning_status,
                p.calendar_event_ids,
                p.scheduled_start_at,
                p.scheduled_end_at

            FROM actions a

            JOIN intelligence_action_planning p
              ON p.action_id = a.id

            WHERE
                a.status IN (
                    'approved',
                    'todo',
                    'doing',
                    'done'
                )

            ORDER BY a.id
        """).fetchall()

    finally:
        conn.close()


def _refs(value):
    try:
        refs = json.loads(value or "[]")
        return refs if isinstance(refs, list) else []
    except Exception:
        return []


def _events_index(days=14):
    horizon = build_horizon(days)

    return {
        e.get("event_id"): e
        for e in horizon["calendar_events"]
        if e.get("event_id")
    }, horizon["calendar_events"]


def _overlap(start1, end1, start2, end2):
    return (
        start1
        and end1
        and start2
        and end2
        and start1 < end2
        and start2 < end1
    )


def detect_replanning(days=14):
    now = dt.datetime.now().astimezone()

    tasks = _vault_tasks()

    events_by_id, all_events = _events_index(days)

    issues = []

    work_ids = set()

    for task in tasks:
        work_ids.update(
            _refs(task["calendar_event_ids"])
        )

    for task in tasks:
        action_id = task["id"]
        title = task["title"]
        status = task["status"]
        planning = task["planning_status"]

        refs = _refs(task["calendar_event_ids"])

        if planning == "planned" and not refs:
            issues.append({
                "action_id": action_id,
                "title": title,
                "type": "missing_calendar_ref",
                "message": (
                    "La tâche est marquée planifiée "
                    "mais aucun bloc Calendar n'est lié."
                ),
            })
            continue

        for event_id in refs:
            event = events_by_id.get(event_id)

            if not event:
                issues.append({
                    "action_id": action_id,
                    "title": title,
                    "type": "calendar_event_missing",
                    "event_id": event_id,
                    "message": (
                        "Le bloc Calendar lié est absent "
                        "de l'horizon actuel."
                    ),
                })
                continue

            actual_start = _parse(
                event.get("start")
            )

            actual_end = _parse(
                event.get("end")
            )

            scheduled_start = _parse(
                task["scheduled_start_at"]
            )

            scheduled_end = _parse(
                task["scheduled_end_at"]
            )

            if (
                scheduled_start
                and actual_start
                and abs(
                    (
                        actual_start
                        - scheduled_start
                    ).total_seconds()
                ) > 60
            ):
                issues.append({
                    "action_id": action_id,
                    "title": title,
                    "type": "calendar_moved",
                    "event_id": event_id,
                    "message": (
                        "Le bloc Calendar a été déplacé "
                        f"vers {actual_start.strftime('%d/%m %H:%M')}."
                    ),
                })

            if (
                scheduled_end
                and actual_end
                and abs(
                    (
                        actual_end
                        - scheduled_end
                    ).total_seconds()
                ) > 60
            ):
                issues.append({
                    "action_id": action_id,
                    "title": title,
                    "type": "calendar_duration_changed",
                    "event_id": event_id,
                    "message": (
                        "La durée ou l'heure de fin "
                        "du bloc Calendar a changé."
                    ),
                })

            if (
                status == "done"
                and actual_start
                and actual_start > now
            ):
                issues.append({
                    "action_id": action_id,
                    "title": title,
                    "type": "completed_with_future_block",
                    "event_id": event_id,
                    "message": (
                        "La tâche est terminée mais possède "
                        "encore un bloc futur."
                    ),
                })

            if (
                status != "done"
                and actual_end
                and actual_end < now
            ):
                issues.append({
                    "action_id": action_id,
                    "title": title,
                    "type": "elapsed_block_open_task",
                    "event_id": event_id,
                    "message": (
                        "Le bloc de travail est passé "
                        "mais la tâche est toujours ouverte."
                    ),
                })

            if (
                task["due_date"]
                and actual_end
            ):
                try:
                    deadline = dt.date.fromisoformat(
                        str(task["due_date"])[:10]
                    )

                    if actual_end.date() > deadline:
                        issues.append({
                            "action_id": action_id,
                            "title": title,
                            "type": "after_deadline",
                            "event_id": event_id,
                            "message": (
                                "Le bloc est placé après "
                                "l'échéance de la tâche."
                            ),
                        })

                except Exception:
                    pass

            if actual_start and actual_end:
                for other in all_events:
                    other_id = other.get("event_id")

                    if (
                        not other_id
                        or other_id == event_id
                    ):
                        continue

                    other_start = _parse(
                        other.get("start")
                    )

                    other_end = _parse(
                        other.get("end")
                    )

                    if _overlap(
                        actual_start,
                        actual_end,
                        other_start,
                        other_end,
                    ):
                        issues.append({
                            "action_id": action_id,
                            "title": title,
                            "type": "calendar_conflict",
                            "event_id": event_id,
                            "conflicting_event": (
                                other.get("title")
                                or "(sans titre)"
                            ),
                            "message": (
                                "Le bloc chevauche maintenant "
                                f"« {other.get('title') or '(sans titre)'} »."
                            ),
                        })

    return issues


def replanning_text(days=14):
    issues = detect_replanning(days)

    lines = [
        "REPLANNING WATCH — LECTURE SEULE",
        ""
    ]

    if not issues:
        lines.append(
            "Aucun écart nécessitant une replanification."
        )
        return "\n".join(lines)

    lines.append(
        f"{len(issues)} écart(s) détecté(s) :"
    )

    for issue in issues:
        lines.append(
            f"- {issue['title']} : "
            f"{issue['message']}"
        )

    lines.extend([
        "",
        "Aucun événement Calendar n'a été modifié."
    ])

    return "\n".join(lines)


@outil(
    nom="replanning_watch",
    description=(
        "Détecte les écarts entre les tâches du Vault "
        "et les blocs Google Calendar afin de déterminer "
        "si une replanification doit être proposée."
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
    phrase_attente=(
        "Je vérifie si ton planning doit être ajusté."
    ),
    mcp_expose=False,
)
def replanning_watch(jours: int = 14):
    return replanning_text(jours)


if __name__ == "__main__":
    print(replanning_text(14))
