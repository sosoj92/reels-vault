import argparse
import datetime as dt
from collections import Counter

from core.registre import outil
from tools.mentor_calendar import build_horizon


def _event_day(value):
    if not value:
        return None

    try:
        return dt.date.fromisoformat(value[:10])
    except Exception:
        return None


def _task_line(task):
    title = task["title"]
    due = task.get("due_date")
    delta = task.get("days_until")

    if delta is None:
        when = "sans échéance"
    elif delta < 0:
        when = f"en retard de {-delta} j"
    elif delta == 0:
        when = "aujourd'hui"
    elif delta == 1:
        when = "demain"
    else:
        when = f"dans {delta} j"

    return f"- {title} — {when}"


def build_weekly_horizon(days=14):
    data = build_horizon(days)

    tasks = data["tasks"]
    events = data["calendar_events"]
    summary = data["summary"]

    urgent = [
        t for t in tasks
        if t.get("days_until") is not None
        and t["days_until"] <= 3
    ]

    next_7 = [
        t for t in tasks
        if t.get("days_until") is not None
        and 4 <= t["days_until"] <= 7
    ]

    horizon_8_14 = [
        t for t in tasks
        if t.get("days_until") is not None
        and 8 <= t["days_until"] <= days
    ]

    unplanned = [
        t for t in tasks
        if t.get("planning_status") in (
            None,
            "unplanned",
            "reschedule",
        )
    ]

    event_counts = Counter()

    for event in events:
        day = _event_day(event.get("start"))
        if day:
            event_counts[day] += 1

    busiest = sorted(
        event_counts.items(),
        key=lambda item: (-item[1], item[0]),
    )[:3]

    lines = [
        f"WEEKLY HORIZON — {days} jours",
        "",
        (
            f"{summary['calendar_events']} événement(s) calendrier, "
            f"{summary['active_tasks']} tâche(s) active(s), "
            f"{summary['tasks_due_within_horizon']} échéance(s) "
            f"dans l'horizon."
        ),
    ]

    if urgent:
        lines.extend([
            "",
            "À TRAITER EN PRIORITÉ",
            *[_task_line(t) for t in urgent],
        ])

    if next_7:
        lines.extend([
            "",
            "À VENIR CETTE SEMAINE",
            *[_task_line(t) for t in next_7],
        ])

    if horizon_8_14:
        lines.extend([
            "",
            "HORIZON 8–14 JOURS",
            *[_task_line(t) for t in horizon_8_14],
        ])

    if unplanned:
        lines.extend([
            "",
            f"PLANIFICATION : {len(unplanned)} tâche(s) "
            f"doivent encore recevoir un bloc de travail.",
        ])

    if busiest:
        lines.append("")
        lines.append("JOURS LES PLUS CHARGÉS")

        for day, count in busiest:
            lines.append(
                f"- {day.strftime('%d/%m')} : "
                f"{count} événement(s)"
            )

    if summary["overdue_tasks"]:
        lines.extend([
            "",
            (
                f"ALERTE : {summary['overdue_tasks']} "
                f"tâche(s) en retard."
            )
        ])

    return "\n".join(lines)


@outil(
    nom="weekly_horizon",
    description=(
        "Produit une vue stratégique des 14 prochains jours "
        "à partir du Vault et de Google Calendar : échéances, "
        "tâches non planifiées et journées chargées."
    ),
    parametres={
        "type": "object",
        "properties": {
            "jours": {
                "type": "integer",
                "description": "Horizon en jours, 14 par défaut."
            }
        }
    },
    lent=True,
    phrase_attente="Je regarde les deux prochaines semaines.",
    mcp_expose=False,
)
def weekly_horizon(jours: int = 14):
    return build_weekly_horizon(jours)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args()

    print(build_weekly_horizon(args.days))


if __name__ == "__main__":
    main()
