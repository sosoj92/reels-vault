import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

from core.config import reglage
from core.registre import outil
from tools import agenda


def _vault_root():
    configured = (reglage("integrations.life_intelligence_vault", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "reels-vault").resolve()


def _vault_db():
    return _vault_root() / "vault.db"


def _horizon_label(due_date, today, days=14):
    if not due_date:
        return "someday", None

    try:
        due = dt.date.fromisoformat(str(due_date)[:10])
    except ValueError:
        return "unknown", None

    delta = (due - today).days

    if delta < 0:
        return "overdue", delta
    if delta == 0:
        return "now", delta
    if delta <= 7:
        return "next", delta
    if delta <= days:
        return "horizon", delta
    return "later", delta


def _tasks(days=14):
    db = _vault_db()
    if not db.exists():
        return []

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute("""
            SELECT
                a.id,
                a.title,
                a.description,
                a.status,
                a.priority,
                a.due_date,
                p.duration_minutes,
                p.rigidity,
                p.deadline_type,
                p.energy,
                p.splittable,
                p.planning_status,
                p.calendar_event_ids,
                p.context
            FROM actions a
            LEFT JOIN intelligence_action_planning p
              ON p.action_id = a.id
            WHERE a.status IN ('approved', 'todo', 'doing')
            ORDER BY
                CASE WHEN a.due_date IS NULL THEN 1 ELSE 0 END,
                a.due_date,
                COALESCE(a.priority, 3),
                a.id
        """).fetchall()

        today = dt.datetime.now().astimezone().date()
        out = []

        for row in rows:
            item = dict(row)
            horizon, delta = _horizon_label(
                item.get("due_date"),
                today,
                days=days,
            )
            item["horizon"] = horizon
            item["days_until"] = delta
            out.append(item)

        return out

    finally:
        conn.close()


def _calendar_events(days=14):
    service = agenda._service()

    start = dt.datetime.now().astimezone()
    end = start + dt.timedelta(days=days)

    events = []

    for calendar_id, info in agenda._calendriers(service).items():
        try:
            result = service.events().list(
                calendarId=calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
            ).execute()
        except Exception:
            continue

        for event in result.get("items", []):
            start_data = event.get("start", {})
            end_data = event.get("end", {})

            events.append({
                "event_id": event.get("id"),
                "title": event.get("summary", "(sans titre)"),
                "start": (
                    start_data.get("dateTime")
                    or start_data.get("date")
                ),
                "end": (
                    end_data.get("dateTime")
                    or end_data.get("date")
                ),
                "all_day": "date" in start_data,
                "calendar": info.get("nom"),
                "calendar_role": info.get("role"),
                "rigidity": "fixed",
            })

    return events


def build_horizon(days=14):
    days = max(1, min(int(days), 60))

    tasks = _tasks(days)
    events = _calendar_events(days)

    due_in_horizon = [
        t for t in tasks
        if t["horizon"] in ("overdue", "now", "next", "horizon")
    ]

    overdue = [
        t for t in tasks
        if t["horizon"] == "overdue"
    ]

    unplanned = [
        t for t in tasks
        if t.get("planning_status") in (None, "unplanned", "reschedule")
    ]

    return {
        "generated_at": dt.datetime.now().astimezone().isoformat(),
        "horizon_days": days,
        "calendar_events": events,
        "tasks": tasks,
        "summary": {
            "calendar_events": len(events),
            "active_tasks": len(tasks),
            "tasks_due_within_horizon": len(due_in_horizon),
            "overdue_tasks": len(overdue),
            "unplanned_tasks": len(unplanned),
        },
    }


def _brief_text(days=14):
    data = build_horizon(days)
    s = data["summary"]

    parts = [
        f"Sur les {days} prochains jours : "
        f"{s['calendar_events']} événement(s) calendrier, "
        f"{s['active_tasks']} tâche(s) active(s), "
        f"{s['tasks_due_within_horizon']} échéance(s) dans l'horizon."
    ]

    if s["overdue_tasks"]:
        parts.append(
            f"{s['overdue_tasks']} tâche(s) sont en retard."
        )

    if s["unplanned_tasks"]:
        parts.append(
            f"{s['unplanned_tasks']} tâche(s) ne sont pas encore planifiées."
        )

    upcoming = [
        t for t in data["tasks"]
        if t["horizon"] in ("overdue", "now", "next", "horizon")
    ][:5]

    if upcoming:
        labels = []
        for task in upcoming:
            delta = task["days_until"]

            if delta is None:
                when = ""
            elif delta < 0:
                when = f"en retard de {-delta} j"
            elif delta == 0:
                when = "aujourd'hui"
            else:
                when = f"dans {delta} j"

            labels.append(f"{task['title']} ({when})")

        parts.append(
            "Prochaines échéances : " + " ; ".join(labels) + "."
        )

    return " ".join(parts)


@outil(
    nom="mentor_horizon",
    description=(
        "Lit l'agenda Google et les tâches du Life Intelligence System "
        "pour donner une vue des prochains jours : rendez-vous, échéances, "
        "retards et tâches non planifiées."
    ),
    parametres={
        "type": "object",
        "properties": {
            "jours": {
                "type": "integer",
                "description": "Nombre de jours à regarder, 14 par défaut."
            }
        }
    },
    lent=True,
    phrase_attente="Je regarde ton agenda et tes prochaines échéances.",
    mcp_expose=False,
)
def mentor_horizon(jours: int = 14):
    return _brief_text(jours)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--text", action="store_true")
    args = parser.parse_args()

    if args.text:
        print(_brief_text(args.days))
    else:
        print(json.dumps(
            build_horizon(args.days),
            ensure_ascii=False,
            indent=2
        ))


if __name__ == "__main__":
    main()
