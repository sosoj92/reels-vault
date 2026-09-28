import argparse
import datetime as dt

from core.registre import outil
from tools.mentor_calendar import build_horizon


def _parse_datetime(value, local_tz):
    if not value:
        return None

    if len(value) == 10:
        d = dt.date.fromisoformat(value)
        return dt.datetime.combine(d, dt.time.min, tzinfo=local_tz)

    value = value.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(value)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz)

    return parsed.astimezone(local_tz)


def _merge(intervals):
    if not intervals:
        return []

    intervals = sorted(intervals, key=lambda x: x[0])
    merged = [intervals[0]]

    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]

        if start <= last_end:
            merged[-1] = (
                last_start,
                max(last_end, end)
            )
        else:
            merged.append((start, end))

    return merged


def _calendar_busy(events, day, local_tz, day_start, day_end, buffer_min):
    work_start = dt.datetime.combine(
        day,
        dt.time(day_start, 0),
        tzinfo=local_tz
    )

    work_end = dt.datetime.combine(
        day,
        dt.time(day_end, 0),
        tzinfo=local_tz
    )

    busy = []

    for event in events:
        start = _parse_datetime(event.get("start"), local_tz)
        end = _parse_datetime(event.get("end"), local_tz)

        if not start or not end:
            continue

        if event.get("all_day"):
            if start.date() <= day < end.date():
                return [(work_start, work_end)]
            continue

        if start.date() != day and end.date() != day:
            if not (start.date() < day < end.date()):
                continue

        buffer = dt.timedelta(minutes=buffer_min)

        start = max(
            work_start,
            start - buffer
        )

        end = min(
            work_end,
            end + buffer
        )

        if start < end:
            busy.append((start, end))

    return _merge(busy)


def _find_slot(
    events,
    start_date,
    last_date,
    duration_min,
    local_tz,
    day_start,
    day_end,
    buffer_min,
    reserved
):
    duration = dt.timedelta(minutes=duration_min)
    now = dt.datetime.now(local_tz)

    day = start_date

    while day <= last_date:
        work_start = dt.datetime.combine(
            day,
            dt.time(day_start, 0),
            tzinfo=local_tz
        )

        work_end = dt.datetime.combine(
            day,
            dt.time(day_end, 0),
            tzinfo=local_tz
        )

        if day == now.date():
            rounded_now = now.replace(
                second=0,
                microsecond=0
            )

            if rounded_now.minute % 15:
                rounded_now += dt.timedelta(
                    minutes=15 - rounded_now.minute % 15
                )

            work_start = max(
                work_start,
                rounded_now
            )

        if work_start >= work_end:
            day += dt.timedelta(days=1)
            continue

        busy = _calendar_busy(
            events,
            day,
            local_tz,
            day_start,
            day_end,
            buffer_min
        )

        for start, end in reserved:
            if start.date() == day:
                busy.append((
                    start,
                    end + dt.timedelta(minutes=buffer_min)
                ))

        busy = _merge(busy)

        cursor = work_start

        for busy_start, busy_end in busy:
            if cursor + duration <= busy_start:
                return cursor, cursor + duration

            cursor = max(cursor, busy_end)

        if cursor + duration <= work_end:
            return cursor, cursor + duration

        day += dt.timedelta(days=1)

    return None


def propose_slots(
    days=14,
    day_start=9,
    day_end=19,
    fallback_duration=45,
    buffer_min=15
):
    data = build_horizon(days)

    local_tz = dt.datetime.now().astimezone().tzinfo
    today = dt.datetime.now(local_tz).date()
    horizon_end = today + dt.timedelta(days=days)

    tasks = [
        t for t in data["tasks"]
        if t.get("planning_status") in (
            None,
            "unplanned",
            "reschedule"
        )
    ]

    tasks.sort(
        key=lambda t: (
            t.get("due_date") is None,
            t.get("due_date") or "9999-12-31",
            t.get("priority") or 3,
            t["id"]
        )
    )

    proposals = []
    reserved = []

    for task in tasks:
        duration = (
            task.get("duration_minutes")
            or fallback_duration
        )

        duration_source = (
            "renseignée"
            if task.get("duration_minutes")
            else "fallback"
        )

        due_date = task.get("due_date")

        if due_date:
            try:
                last_date = min(
                    dt.date.fromisoformat(due_date[:10]),
                    horizon_end
                )
            except Exception:
                last_date = horizon_end
        else:
            last_date = horizon_end

        if last_date < today:
            last_date = today

        slot = _find_slot(
            data["calendar_events"],
            today,
            last_date,
            int(duration),
            local_tz,
            day_start,
            day_end,
            buffer_min,
            reserved
        )

        if slot:
            start, end = slot
            reserved.append((start, end))

            proposals.append({
                "action_id": task["id"],
                "title": task["title"],
                "start": start,
                "end": end,
                "duration": int(duration),
                "duration_source": duration_source,
                "due_date": due_date,
            })
        else:
            proposals.append({
                "action_id": task["id"],
                "title": task["title"],
                "start": None,
                "end": None,
                "duration": int(duration),
                "duration_source": duration_source,
                "due_date": due_date,
            })

    return proposals


def planner_text(
    days=14,
    day_start=9,
    day_end=19,
    fallback_duration=45,
    buffer_min=15
):
    proposals = propose_slots(
        days,
        day_start,
        day_end,
        fallback_duration,
        buffer_min
    )

    lines = [
        "DYNAMIC PLANNER — PROPOSITIONS UNIQUEMENT",
        (
            f"Fenêtre temporaire : {day_start:02d}:00–"
            f"{day_end:02d}:00 | buffer {buffer_min} min | "
            f"durée fallback {fallback_duration} min"
        ),
        ""
    ]

    for p in proposals:
        if p["start"] is None:
            lines.append(
                f"- {p['title']} : aucun créneau trouvé avant l'échéance."
            )
            continue

        source = (
            "durée renseignée"
            if p["duration_source"] == "renseignée"
            else "durée temporaire estimée"
        )

        lines.append(
            f"- {p['title']} → "
            f"{p['start'].strftime('%d/%m %H:%M')}–"
            f"{p['end'].strftime('%H:%M')} "
            f"({p['duration']} min, {source})"
        )

    lines.extend([
        "",
        "Aucun événement Calendar n'a été créé ou déplacé."
    ])

    return "\n".join(lines)


@outil(
    nom="dynamic_planner",
    description=(
        "Propose des blocs de travail autour des rendez-vous Calendar "
        "sans modifier l'agenda. Les rendez-vous existants restent fixes."
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
    phrase_attente="Je cherche les meilleurs créneaux disponibles.",
    mcp_expose=False,
)
def dynamic_planner(jours: int = 14):
    return planner_text(days=jours)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--start", type=int, default=9)
    parser.add_argument("--end", type=int, default=19)
    parser.add_argument("--fallback", type=int, default=45)
    parser.add_argument("--buffer", type=int, default=15)

    args = parser.parse_args()

    print(
        planner_text(
            days=args.days,
            day_start=args.start,
            day_end=args.end,
            fallback_duration=args.fallback,
            buffer_min=args.buffer
        )
    )


if __name__ == "__main__":
    main()

