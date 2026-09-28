#!/usr/bin/env python3

import argparse
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from vault_notion_cockpit import NotionCockpitClient, CockpitError

DEFAULT_TASKS_DATA_SOURCE_ID = "d83de47c-efa9-4309-9770-1203114e72e1"

STATUS_TO_NOTION = {
    "approved": "Pas commencée",
    "todo": "Pas commencée",
    "doing": "En cours",
    "done": "Terminé",
}

NOTION_TO_STATUS = {
    "Pas commencée": "todo",
    "En cours": "doing",
    "Terminé": "done",
    "Archivé": "archived",
}

PRIORITY_TO_NOTION = {
    1: "Urgente",
    2: "Haute",
    3: "Normale",
    4: "Basse",
}

NOTION_TO_PRIORITY = {
    "Urgente": 1,
    "Haute": 2,
    "Normale": 3,
    "Basse": 4,
}

RIGIDITY_TO_NOTION = {
    "fixed": "Fixe",
    "flexible": "Flexible",
    "optional": "Optionnel",
}

NOTION_TO_RIGIDITY = {v: k for k, v in RIGIDITY_TO_NOTION.items()}

DEADLINE_TO_NOTION = {
    "hard": "Dure",
    "target": "Souhaitée",
    "none": "Aucune",
}

NOTION_TO_DEADLINE = {v: k for k, v in DEADLINE_TO_NOTION.items()}

ENERGY_TO_NOTION = {
    "low": "Basse",
    "medium": "Moyenne",
    "high": "Haute",
}

NOTION_TO_ENERGY = {v: k for k, v in ENERGY_TO_NOTION.items()}

PLANNING_TO_NOTION = {
    "unplanned": "Non planifiée",
    "proposed": "Proposée",
    "planned": "Planifiée",
    "reschedule": "À replanifier",
    "done": "Planifiée",
}

NOTION_TO_PLANNING = {
    "Non planifiée": "unplanned",
    "Proposée": "proposed",
    "Planifiée": "planned",
    "À replanifier": "reschedule",
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_planning_rows(conn):
    actions = conn.execute("""
        SELECT id, due_date
        FROM actions
        WHERE status IN ('approved', 'todo', 'doing', 'done')
        ORDER BY id
    """).fetchall()

    for action in actions:
        deadline_type = "target" if action["due_date"] else "none"

        conn.execute("""
            INSERT INTO intelligence_action_planning(
                action_id,
                rigidity,
                deadline_type,
                splittable,
                planning_status,
                synced_at
            )
            VALUES (?, 'flexible', ?, 1, 'unplanned', ?)
            ON CONFLICT(action_id) DO NOTHING
        """, (
            action["id"],
            deadline_type,
            now(),
        ))

    conn.commit()


def list_tasks(conn):
    ensure_planning_rows(conn)

    return conn.execute("""
        SELECT
            a.id,
            a.title,
            a.description,
            a.status,
            a.priority,
            a.due_date,
            a.updated_at,

            p.notion_page_id,
            p.notion_url,
            p.duration_minutes,
            p.rigidity,
            p.deadline_type,
            p.energy,
            p.splittable,
            p.planning_status,
            p.calendar_event_ids,
            p.context,
            p.synced_at

        FROM actions a
        JOIN intelligence_action_planning p
          ON p.action_id = a.id

        WHERE a.status IN ('approved', 'todo', 'doing', 'done')

        ORDER BY
            CASE WHEN a.due_date IS NULL THEN 1 ELSE 0 END,
            a.due_date,
            COALESCE(a.priority, 3),
            a.id
    """).fetchall()


def rich_text(value):
    value = str(value or "").strip()

    if not value:
        return []

    return [{
        "type": "text",
        "text": {
            "content": value[:2000]
        }
    }]


def payload(row):
    priority = PRIORITY_TO_NOTION.get(row["priority"] or 3, "Normale")

    energy = (
        {"select": {"name": ENERGY_TO_NOTION[row["energy"]]}}
        if row["energy"] in ENERGY_TO_NOTION
        else {"select": None}
    )

    due = (
        {"date": {"start": row["due_date"]}}
        if row["due_date"]
        else {"date": None}
    )

    try:
        calendar_refs = json.loads(row["calendar_event_ids"] or "[]")
    except Exception:
        calendar_refs = []

    return {
        "Nom de la tâche": {
            "title": rich_text(row["title"])
        },

        "État": {
            "status": {
                "name": STATUS_TO_NOTION.get(
                    row["status"],
                    "Pas commencée"
                )
            }
        },

        "Priorité": {
            "select": {
                "name": priority
            }
        },

        "Échéance": due,

        "Durée estimée (min)": {
            "number": row["duration_minutes"]
        },

        "Rigidité": {
            "select": {
                "name": RIGIDITY_TO_NOTION.get(
                    row["rigidity"],
                    "Flexible"
                )
            }
        },

        "Type échéance": {
            "select": {
                "name": DEADLINE_TO_NOTION.get(
                    row["deadline_type"],
                    "Aucune"
                )
            }
        },

        "Énergie": energy,

        "Fractionnable": {
            "checkbox": bool(row["splittable"])
        },

        "Planification": {
            "select": {
                "name": PLANNING_TO_NOTION.get(
                    row["planning_status"],
                    "Non planifiée"
                )
            }
        },

        "Calendar refs": {
            "rich_text": rich_text(
                json.dumps(
                    calendar_refs,
                    ensure_ascii=False
                )
            )
        },

        "Contexte": {
            "rich_text": rich_text(
                row["context"]
                or "Life Intelligence / action Vault"
            )
        },

        "Vault ref": {
            "rich_text": rich_text(
                f"action:{row['id']}"
            )
        },
    }


def text_value(prop):
    items = prop.get("rich_text") or []
    return "".join(
        str(item.get("plain_text") or "")
        for item in items
    ).strip()


def select_value(prop):
    value = prop.get("select")
    return value.get("name") if value else None


def status_value(prop):
    value = prop.get("status")
    return value.get("name") if value else None


def date_value(prop):
    value = prop.get("date")
    return value.get("start") if value else None


def push(conn, confirm=False):
    tasks = list_tasks(conn)

    if not confirm:
        return {
            "writes_performed": False,
            "items": [
                {
                    "action_id": row["id"],
                    "title": row["title"],
                    "status": row["status"],
                    "priority": row["priority"],
                    "due_date": row["due_date"],
                    "notion_page_id": row["notion_page_id"],
                    "planning_status": row["planning_status"],
                }
                for row in tasks
            ],
        }

    client = NotionCockpitClient(
        data_source_id=DEFAULT_TASKS_DATA_SOURCE_ID
    )

    result = {
        "created": 0,
        "updated": 0,
        "items": len(tasks),
    }

    for row in tasks:
        props = payload(row)

        if row["notion_page_id"]:
            try:
                page = client.update_page(
                    row["notion_page_id"],
                    props,
                )
                result["updated"] += 1

            except CockpitError as exc:
                if getattr(exc, "status", None) != 404:
                    raise

                page = client.create_page(props)
                result["created"] += 1

        else:
            page = client.create_page(props)
            result["created"] += 1

        conn.execute("""
            UPDATE intelligence_action_planning
               SET notion_page_id = ?,
                   notion_url = ?,
                   synced_at = ?
             WHERE action_id = ?
        """, (
            page["id"],
            page.get("url"),
            now(),
            row["id"],
        ))

    conn.commit()
    return result


def pull(conn, confirm=False):
    client = NotionCockpitClient(
        data_source_id=DEFAULT_TASKS_DATA_SOURCE_ID
    )

    tasks = list_tasks(conn)

    changes = []

    for row in tasks:
        if not row["notion_page_id"]:
            continue

        page = client.get_page(row["notion_page_id"])
        props = page.get("properties") or {}

        current_expected_status = STATUS_TO_NOTION.get(
            row["status"],
            "Pas commencée"
        )

        notion_status = status_value(
            props.get("État") or {}
        )

        notion_priority = select_value(
            props.get("Priorité") or {}
        )

        notion_due = date_value(
            props.get("Échéance") or {}
        )

        notion_rigidity = select_value(
            props.get("Rigidité") or {}
        )

        notion_deadline_type = select_value(
            props.get("Type échéance") or {}
        )

        notion_energy = select_value(
            props.get("Énergie") or {}
        )

        notion_planning = select_value(
            props.get("Planification") or {}
        )

        duration = (
            (props.get("Durée estimée (min)") or {})
            .get("number")
        )

        splittable = bool(
            (props.get("Fractionnable") or {})
            .get("checkbox")
        )

        context = text_value(
            props.get("Contexte") or {}
        )

        calendar_refs_text = text_value(
            props.get("Calendar refs") or {}
        )

        try:
            calendar_refs = json.loads(calendar_refs_text) if calendar_refs_text else []
            if not isinstance(calendar_refs, list):
                calendar_refs = []
        except Exception:
            calendar_refs = []

        try:
            current_calendar_refs = json.loads(
                row["calendar_event_ids"] or "[]"
            )
            if not isinstance(current_calendar_refs, list):
                current_calendar_refs = []
        except Exception:
            current_calendar_refs = []

        updates_action = {}
        updates_plan = {}

        if (
            notion_status
            and notion_status != current_expected_status
            and notion_status in NOTION_TO_STATUS
        ):
            updates_action["status"] = NOTION_TO_STATUS[notion_status]

            if notion_status == "Terminé":
                updates_plan["planning_status"] = "done"

        if notion_priority in NOTION_TO_PRIORITY:
            priority = NOTION_TO_PRIORITY[notion_priority]

            if priority != (row["priority"] or 3):
                updates_action["priority"] = priority

        if notion_due != row["due_date"]:
            updates_action["due_date"] = notion_due

        if notion_rigidity in NOTION_TO_RIGIDITY:
            value = NOTION_TO_RIGIDITY[notion_rigidity]

            if value != row["rigidity"]:
                updates_plan["rigidity"] = value

        if notion_deadline_type in NOTION_TO_DEADLINE:
            value = NOTION_TO_DEADLINE[notion_deadline_type]

            if value != row["deadline_type"]:
                updates_plan["deadline_type"] = value

        if notion_energy in NOTION_TO_ENERGY:
            value = NOTION_TO_ENERGY[notion_energy]

            if value != row["energy"]:
                updates_plan["energy"] = value

        elif notion_energy is None and row["energy"] is not None:
            updates_plan["energy"] = None

        if duration != row["duration_minutes"]:
            updates_plan["duration_minutes"] = duration

        if int(splittable) != int(row["splittable"]):
            updates_plan["splittable"] = int(splittable)

        if notion_planning in NOTION_TO_PLANNING:
            value = NOTION_TO_PLANNING[notion_planning]

            if (
                row["planning_status"] != "done"
                and value != row["planning_status"]
            ):
                updates_plan["planning_status"] = value

        if context != (row["context"] or ""):
            updates_plan["context"] = context or None

        if calendar_refs != current_calendar_refs:
            updates_plan["calendar_event_ids"] = json.dumps(
                calendar_refs,
                ensure_ascii=False
            )

        if not updates_action and not updates_plan:
            continue

        changes.append({
            "action_id": row["id"],
            "title": row["title"],
            "action_changes": updates_action,
            "planning_changes": updates_plan,
        })

        if not confirm:
            continue

        if updates_action:
            fields = []
            values = []

            for key, value in updates_action.items():
                fields.append(f"{key} = ?")
                values.append(value)

            fields.append("updated_at = ?")
            values.append(now())
            values.append(row["id"])

            conn.execute(
                "UPDATE actions SET "
                + ", ".join(fields)
                + " WHERE id = ?",
                values,
            )

        if updates_plan:
            fields = []
            values = []

            for key, value in updates_plan.items():
                fields.append(f"{key} = ?")
                values.append(value)

            fields.append("synced_at = ?")
            values.append(now())
            values.append(row["id"])

            conn.execute(
                "UPDATE intelligence_action_planning SET "
                + ", ".join(fields)
                + " WHERE action_id = ?",
                values,
            )

    if confirm:
        conn.commit()

    return {
        "writes_performed": bool(confirm),
        "changes": changes,
        "change_count": len(changes),
    }


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "mode",
        choices=(
            "dry-run",
            "push",
            "pull-dry-run",
            "pull",
        )
    )

    parser.add_argument(
        "--db",
        # Chemin absolu : jamais de base vide creee dans le repertoire courant.
        default=str(Path(__file__).resolve().parent / "vault.db")
    )

    parser.add_argument(
        "--confirm",
        action="store_true"
    )

    args = parser.parse_args()

    conn = connect(args.db)

    try:
        if args.mode == "dry-run":
            result = push(
                conn,
                confirm=False
            )

        elif args.mode == "push":
            if not args.confirm:
                raise SystemExit(
                    "Push refusé : ajouter --confirm."
                )

            result = push(
                conn,
                confirm=True
            )

        elif args.mode == "pull-dry-run":
            result = pull(
                conn,
                confirm=False
            )

        elif args.mode == "pull":
            if not args.confirm:
                raise SystemExit(
                    "Pull refusé : ajouter --confirm."
                )

            result = pull(
                conn,
                confirm=True
            )

        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2
            )
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
