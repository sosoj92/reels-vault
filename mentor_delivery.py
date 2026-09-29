#!/usr/bin/env python3
"""
mentor_delivery.py - Quand et comment le Mentor parle, et ce que Rosy decide.

Le Mentor observe en continu mais ne parle que si c'est assez bon :
- au plus 1 suggestion poussee par jour (budget), le reste en digest quotidien ;
- heures calmes, pause (« jusqu'a lundi ») et mode focus : il journalise, n'envoie rien ;
- snooze (ce soir / demain / lundi) ecrit dans vault.db, reevaluation avant renvoi ;
- fatigue : les retours negatifs recents penalisent un projet/type ;
- silence si rien n'est suffisamment bon.

Les decisions (Telegram ou Notion) passent toutes par ce module : vault.db reste
l'arbitre. Aucune action externe sans clic de Rosy ; une tache n'est creee que
sur « Creer la tache » / « Approuver ».
"""

from datetime import datetime, time, timedelta, timezone

import mentor_council as council
import mentor_proactive_store as store

DEFAULTS = {
    "max_push_per_day": "1",
    "push_min_score": "0.75",
    "digest_min_score": "0.55",
    "quiet_start": "21:30",
    "quiet_end": "08:30",
    "digest_time": "18:30",
    "suggestion_ttl_days": "14",
    "paused_until": "",
}
SNOOZE_CHOICES = {"tonight": "Ce soir", "tomorrow": "Demain", "monday": "Lundi"}
DISMISS_REASONS = {
    "not_now": "Pas le moment",
    "already_known": "Deja connu",
    "off_topic": "Hors sujet",
    "bad_idea": "Mauvaise idee",
}
NOTION_DECISIONS = {
    "✅ Approuver": "task",
    "📌 Garder comme insight": "insight",
    "⏰ Reporter": "snooze",
    "⛔ Ignorer": "dismiss",
}
NOTION_KIND = "Suggestion"
OPEN_STATUSES = ("ready", "sent", "opened", "snoozed")
FATIGUE_SCORE_PENALTY = 0.1


class DeliveryError(ValueError):
    pass


# ------------------------------------------------------------------ reglages

def get(conn, key):
    import vault_db

    return vault_db.get_setting(conn, f"mentor.{key}", DEFAULTS[key])


def put(conn, key, value):
    import vault_db

    if key not in DEFAULTS:
        raise DeliveryError(f"reglage inconnu : {key}")
    vault_db.set_setting(conn, f"mentor.{key}", str(value))


def _hm(value):
    hours, minutes = str(value).split(":")
    return time(int(hours), int(minutes))


def local(now, tz=None):
    return now.astimezone(tz) if tz else now.astimezone()


def in_quiet_hours(conn, now, tz=None):
    current = local(now, tz).time()
    start, end = _hm(get(conn, "quiet_start")), _hm(get(conn, "quiet_end"))
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def paused_until(conn, now):
    raw = get(conn, "paused_until")
    if not raw:
        return None
    until = datetime.fromisoformat(raw)
    return until if until > now else None


def pause(conn, until, *, now=None):
    now = now or datetime.now(timezone.utc)
    if until <= now:
        raise DeliveryError("la pause doit finir dans le futur")
    put(conn, "paused_until", until.astimezone(timezone.utc).isoformat(timespec="seconds"))
    return until


def resume(conn):
    put(conn, "paused_until", "")


def next_weekday(now, weekday, at, tz=None):
    """Prochain `weekday` (0 = lundi) a l'heure `at`, strictement dans le futur."""
    base = local(now, tz)
    days = (weekday - base.weekday()) % 7 or 7
    target = datetime.combine((base + timedelta(days=days)).date(), at, base.tzinfo)
    return target.astimezone(timezone.utc)


def snooze_until(choice, now, tz=None):
    base = local(now, tz)
    if choice == "tonight":
        target = datetime.combine(base.date(), time(19, 0), base.tzinfo)
        if target <= base + timedelta(minutes=30):
            target = base + timedelta(hours=2)
    elif choice == "tomorrow":
        target = datetime.combine(base.date() + timedelta(days=1), time(9, 0), base.tzinfo)
    elif choice == "monday":
        return next_weekday(now, 0, time(9, 0), tz)
    else:
        raise DeliveryError(f"report inconnu : {choice}")
    return target.astimezone(timezone.utc)


def focus_until(now, tz=None):
    """Mode focus : silence jusqu'au lendemain 8h30."""
    base = local(now, tz)
    target = datetime.combine(base.date() + timedelta(days=1), time(8, 30), base.tzinfo)
    return target.astimezone(timezone.utc)


# ------------------------------------------------------------------ reevaluation

def effective_score(conn, suggestion, now):
    tiredness = council.fatigue(conn, suggestion.get("project_id"),
                                suggestion["suggestion_type"], now)
    return round(suggestion["score"] - FATIGUE_SCORE_PENALTY * tiredness, 3), tiredness


def reevaluate(conn, suggestion, now):
    """Toujours pertinente ? (avant un renvoi apres snooze ou une livraison)."""
    created = datetime.fromisoformat(suggestion["created_at"])
    if now - created > timedelta(days=int(get(conn, "suggestion_ttl_days"))):
        return False, "trop ancienne"
    active, why = council.project_is_active(conn, suggestion.get("project_id"))
    if not active:
        return False, f"projet : {why}"
    candidate = store.get_candidate(conn, suggestion["candidate_id"])
    if candidate is None:
        return False, "candidat introuvable"
    for item in store.feedback_since(conn, store.now_iso(now - timedelta(days=90))):
        if item["reason"] == "already_known" and item["focus_key"] == candidate["focus_key"] \
                and item["suggestion_id"] != suggestion["suggestion_id"]:
            return False, "deja connu"
    _, tiredness = effective_score(conn, suggestion, now)
    if tiredness >= council.FATIGUE_REJECT:
        return False, f"fatigue {tiredness}"
    return True, None


def expire(conn, suggestion_id, reason, *, at=None):
    suggestion = store.get_suggestion(conn, suggestion_id)
    if suggestion["status"] not in OPEN_STATUSES:
        return suggestion
    return store.transition_suggestion(conn, suggestion_id, "expired", decided_by="mentor",
                                       note=f"expiree : {reason}", at=at)


def wake_due(conn, now):
    """Reports echus : reevaluation, puis retour en `ready` ou expiration silencieuse."""
    at = store.now_iso(now)
    outcome = []
    for suggestion in store.list_suggestions(conn, "snoozed"):
        if suggestion["snoozed_until"] and suggestion["snoozed_until"] <= at:
            ok, why = reevaluate(conn, suggestion, now)
            if ok:
                store.transition_suggestion(conn, suggestion["suggestion_id"], "ready",
                                            decided_by="mentor", at=at)
                outcome.append((suggestion["suggestion_id"], "ready"))
            else:
                expire(conn, suggestion["suggestion_id"], why, at=at)
                outcome.append((suggestion["suggestion_id"], "expired"))
    return outcome


# ------------------------------------------------------------------ planification

def pushes_today(conn, now, tz=None):
    midnight = datetime.combine(local(now, tz).date(), time(0, 0), local(now, tz).tzinfo)
    since = store.now_iso(midnight.astimezone(timezone.utc))
    return len([n for n in store.notifications(conn, kind="push", since=since)
                if n["status"] == "sent"])


def digest_sent_today(conn, now, tz=None):
    midnight = datetime.combine(local(now, tz).date(), time(0, 0), local(now, tz).tzinfo)
    since = store.now_iso(midnight.astimezone(timezone.utc))
    return any(n["status"] == "sent" for n in store.notifications(conn, kind="digest",
                                                                   since=since))


def plan(conn, now, tz=None):
    """Que faut-il envoyer maintenant ? Silence par defaut."""
    at = store.now_iso(now)
    result = {"push": None, "digest": [], "silenced": None, "expired": []}
    fresh = []
    for suggestion in store.list_suggestions(conn, "ready"):
        if store.was_presented(conn, suggestion["suggestion_id"]):
            continue
        ok, why = reevaluate(conn, suggestion, now)
        if not ok:
            expire(conn, suggestion["suggestion_id"], why, at=at)
            result["expired"].append(suggestion["suggestion_id"])
            continue
        score, _ = effective_score(conn, suggestion, now)
        fresh.append((score, suggestion))
    fresh.sort(key=lambda item: (-item[0], item[1]["suggestion_id"]))
    if not fresh:
        result["silenced"] = "rien d'assez bon"
        return result
    pause_end = paused_until(conn, now)
    if pause_end:
        result["silenced"] = f"pause jusqu'a {pause_end.isoformat()}"
        return result
    if in_quiet_hours(conn, now, tz):
        result["silenced"] = "heures calmes"
        return result
    push_min = float(get(conn, "push_min_score"))
    digest_min = float(get(conn, "digest_min_score"))
    if fresh[0][0] >= push_min and pushes_today(conn, now, tz) < int(get(conn, "max_push_per_day")):
        result["push"] = fresh.pop(0)[1]
    digest_at = _hm(get(conn, "digest_time"))
    if local(now, tz).time() >= digest_at and not digest_sent_today(conn, now, tz):
        result["digest"] = [s for score, s in fresh if score >= digest_min]
    if not result["push"] and not result["digest"]:
        result["silenced"] = "budget du jour atteint ou digest pas encore du"
    return result


def deliver(conn, now=None, *, sender=None, notion=None, tz=None):
    """
    Un tick de livraison : reports echus, plan, envoi (Telegram si configure),
    journal, projection Notion des suggestions reellement presentees.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    at = store.now_iso(now)
    report = {"woken": wake_due(conn, now), "pushed": None, "digest": [], "silenced": None}
    decision = plan(conn, now, tz)
    report["silenced"] = decision["silenced"]
    report["expired"] = decision["expired"]
    channel = "telegram" if sender is not None and sender.available() else "notion"
    if decision["push"]:
        suggestion = decision["push"]
        message_id = sender.send_suggestion(suggestion) if channel == "telegram" else None
        store.record_notification(conn, kind="push", channel=channel,
                                  suggestion_ids=[suggestion["suggestion_id"]],
                                  chat_id=getattr(sender, "chat_id", None),
                                  message_id=message_id, at=at)
        store.transition_suggestion(conn, suggestion["suggestion_id"], "sent",
                                    decided_by="mentor", at=at)
        if message_id is not None:
            store.set_telegram_message(conn, suggestion["suggestion_id"], message_id, at=at)
        report["pushed"] = suggestion["suggestion_id"]
    if decision["digest"]:
        ids = [s["suggestion_id"] for s in decision["digest"]]
        message_id = sender.send_digest(decision["digest"]) if channel == "telegram" else None
        store.record_notification(conn, kind="digest", channel=channel, suggestion_ids=ids,
                                  chat_id=getattr(sender, "chat_id", None),
                                  message_id=message_id, at=at)
        report["digest"] = ids
    if notion is not None and notion.configured():
        report["notion"] = project_suggestions(conn, notion)
    return report


# ------------------------------------------------------------------ decisions

def _to(conn, suggestion_id, target, *, by, note=None, snoozed_until=None, at=None):
    """Transition par le chemin autorise le plus court (ex. ready -> accepted)."""
    suggestion = store.get_suggestion(conn, suggestion_id)
    if suggestion is None:
        raise DeliveryError(f"suggestion introuvable : {suggestion_id}")
    if suggestion["status"] == target:
        return suggestion
    if target in store.SUGGESTION_TRANSITIONS.get(suggestion["status"], set()):
        return store.transition_suggestion(conn, suggestion_id, target, decided_by=by,
                                           note=note, snoozed_until=snoozed_until, at=at)
    raise DeliveryError(f"{suggestion_id} est {suggestion['status']} : '{target}' impossible")


def open_detail(conn, suggestion_id, *, at=None):
    suggestion = store.get_suggestion(conn, suggestion_id)
    if suggestion and suggestion["status"] == "sent":
        suggestion = store.transition_suggestion(conn, suggestion_id, "opened", decided_by="rosy",
                                                 at=at)
    return suggestion


def snooze(conn, suggestion_id, choice, *, now=None, by="rosy", tz=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    until = snooze_until(choice, now, tz)
    return _to(conn, suggestion_id, "snoozed", by=by,
               note=f"reporte : {SNOOZE_CHOICES[choice]}",
               snoozed_until=store.now_iso(until), at=store.now_iso(now))


def dismiss(conn, suggestion_id, reason, *, channel="telegram", by="rosy", at=None):
    if reason not in store.FEEDBACK_REASONS:
        raise DeliveryError(f"raison inconnue : {reason}")
    suggestion = _to(conn, suggestion_id, "dismissed", by=by,
                     note=DISMISS_REASONS.get(reason, "Non merci"), at=at)
    store.record_feedback(conn, suggestion_id, reason, channel=channel, at=at)
    return suggestion


def keep_as_insight(conn, suggestion_id, *, by="rosy", at=None):
    return _to(conn, suggestion_id, "accepted", by=by, note="garde comme insight", at=at)


def create_task(conn, suggestion_id, *, by="rosy", at=None):
    """
    Clic « Creer la tache » / « Approuver » : la tache est creee dans vault.db
    (proposed par le Mentor, puis approved par la decision de Rosy).
    Retourne l'action creee. Sa projection Notion passe par push_task().
    """
    import vault_db

    suggestion = store.get_suggestion(conn, suggestion_id)
    if suggestion is None:
        raise DeliveryError(f"suggestion introuvable : {suggestion_id}")
    existing = suggestion["evidence"].get("task_action_id")
    if existing:
        return vault_db.get_action(conn, existing)
    if suggestion["status"] not in ("accepted", "action_proposed"):
        _to(conn, suggestion_id, "accepted", by=by, note="tache demandee", at=at)
    evidence = suggestion["evidence"]
    actions = evidence.get("actions") or []
    content_id = None
    for source_id in (evidence.get("candidate") or {}).get("source_ids", []):
        row = conn.execute("SELECT content_id FROM intelligence_sources WHERE id = ?",
                           (source_id,)).fetchone()
        if row and row[0]:
            content_id = row[0]
            break
    action = vault_db.create_action(conn, {
        "title": (actions[0] if actions else suggestion["title"])[:200],
        "description": f"{suggestion['suggestion_id']} - {suggestion['summary']}",
        "content_id": content_id,
        "priority": 2,
        "proposed_by": "mentor",
    })
    action = vault_db.set_action_status(conn, action["id"], "approved")
    updated = dict(evidence, task_action_id=action["id"])
    conn.execute("UPDATE mentor_suggestions SET evidence = ? WHERE suggestion_id = ?",
                 (store.dumps(updated), suggestion_id))
    conn.commit()
    store.transition_suggestion(conn, suggestion_id, "action_proposed", decided_by=by,
                                note=f"tache #{action['id']}", at=at)
    store.transition_suggestion(conn, suggestion_id, "approved", decided_by=by, at=at)
    return action


def tasks_client():
    """Client de la base Taches Notion EXISTANTE (meme integration que le cockpit)."""
    import vault_notion_tasks as tasks
    from vault_notion_cockpit import NotionCockpitClient

    return NotionCockpitClient(data_source_id=tasks.DEFAULT_TASKS_DATA_SOURCE_ID)


def push_task(conn, action_id, client):
    """Projette UNE tache validee dans la base Taches Notion existante."""
    import vault_notion_tasks as tasks

    if client is None or not client.configured():
        return None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='intelligence_action_planning'").fetchone() is None:
        return None
    row = next((r for r in tasks.list_tasks(conn) if r["id"] == action_id), None)
    if row is None:
        return None
    if row["notion_page_id"]:
        return row["notion_page_id"]
    page = client.create_page(tasks.payload(row))
    conn.execute(
        "UPDATE intelligence_action_planning SET notion_page_id = ?, notion_url = ?, "
        "synced_at = ? WHERE action_id = ?",
        (page["id"], page.get("url"), tasks.now(), action_id),
    )
    conn.commit()
    return page["id"]


# ------------------------------------------------------------------ Notion (cockpit existant)

def _attention(status):
    return {
        "ready": "🔥 Maintenant", "sent": "🔥 Maintenant", "opened": "🔥 Maintenant",
        "snoozed": "🕒 Plus tard", "accepted": "✅ En place", "action_proposed": "✅ En place",
        "approved": "✅ En place", "executed": "✅ En place",
        "dismissed": "⛔ Écarté", "expired": "⛔ Écarté",
    }[status]


def _action_status(status):
    return {
        "ready": "proposed", "sent": "proposed", "opened": "proposed", "snoozed": "proposed",
        "accepted": "approved", "action_proposed": "approved", "approved": "approved",
        "executed": "done", "dismissed": "dismissed", "expired": "archived",
    }[status]


def notion_row(suggestion):
    evidence = suggestion["evidence"]
    sources = evidence.get("sources") or []
    agents = evidence.get("agents") or []
    verdicts = "; ".join(f"{a['agent']} {a.get('value_score', '?')}/5" for a in agents)
    next_step = {
        "snoozed": f"Reportee jusqu'au {str(suggestion.get('snoozed_until'))[:16]}.",
        "dismissed": "Ecartee.", "expired": "Expiree.",
        "accepted": "Gardee comme insight.",
        "approved": "Tache creee.", "action_proposed": "Tache en creation.",
    }.get(suggestion["status"], "Decider : Approuver (tache), Garder comme insight, "
                                "Reporter ou Ignorer.")
    return {
        "vault_ref": f"suggestion:{suggestion['suggestion_id']}",
        "item": f"💡 {suggestion['title']}",
        "kind": NOTION_KIND,
        "attention": _attention(suggestion["status"]),
        "priority": 1 if suggestion["score"] >= 0.85 else 2,
        "why": (f"{suggestion['summary']} Agents : {verdicts}."
                if verdicts else suggestion["summary"]),
        "next_step": next_step,
        "verification": "n/a",
        "source": (sources[0].get("url") if sources else None),
        "action_status": _action_status(suggestion["status"]),
    }


def project_suggestions(conn, client):
    """
    Projette dans le cockpit existant les suggestions reellement presentees a Rosy.
    Les candidats (et a fortiori les rejetes) ne sont jamais projetes.
    """
    from vault_notion_cockpit import (_existing_link, _payload, _payload_hash, _record_link,
                                      ensure_schema)

    ensure_schema(conn)
    result = {"created": 0, "updated": 0, "unchanged": 0}
    for suggestion in store.projectable_suggestions(conn) + store.list_suggestions(
            conn, ("dismissed", "expired")):
        if not store.was_presented(conn, suggestion["suggestion_id"]):
            continue
        row = notion_row(suggestion)
        existing = _existing_link(conn, row["vault_ref"])
        if existing is None and suggestion["status"] in ("dismissed", "expired"):
            continue  # jamais cree pour etre aussitot ecarte
        properties = _payload(row)
        fingerprint = _payload_hash(properties)
        if existing and existing["payload_hash"] == fingerprint:
            result["unchanged"] += 1
            continue
        if existing:
            page = client.update_page(existing["notion_page_id"], properties)
            result["updated"] += 1
        else:
            page = client.create_page(properties)
            result["created"] += 1
        _record_link(conn, row["vault_ref"], page, fingerprint)
        store.set_notion_page(conn, suggestion["suggestion_id"], page["id"])
    return result


def apply_notion_decisions(conn, client, *, now=None, tz=None, tasks=None):
    """
    Lit la colonne « Decision » des suggestions projetees ; vault.db tranche.
    Retourne [(suggestion_id, decision)] pour refleter l'etat dans Telegram.
    """
    from vault_notion_cockpit import ensure_schema as ensure_cockpit_schema

    ensure_cockpit_schema(conn)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    changed = []
    rows = conn.execute(
        "SELECT vault_ref, notion_page_id FROM intelligence_cockpit_links "
        "WHERE vault_ref LIKE 'suggestion:%' ORDER BY vault_ref"
    ).fetchall()
    for vault_ref, page_id in rows:
        suggestion_id = vault_ref.split(":", 1)[1]
        page = client.get_page(page_id)
        selected = (((page.get("properties") or {}).get("Décision") or {}).get("select") or {})
        decision = NOTION_DECISIONS.get(selected.get("name"))
        if not decision:
            continue
        suggestion = store.get_suggestion(conn, suggestion_id)
        try:
            if suggestion is None or suggestion["status"] in ("dismissed", "expired",
                                                                "approved", "executed"):
                pass
            elif decision == "task":
                action = create_task(conn, suggestion_id, by="rosy (Notion)")
                push_task(conn, action["id"], tasks)
            elif decision == "insight":
                keep_as_insight(conn, suggestion_id, by="rosy (Notion)")
            elif decision == "snooze":
                snooze(conn, suggestion_id, "tomorrow", now=now, by="rosy (Notion)", tz=tz)
            elif decision == "dismiss":
                dismiss(conn, suggestion_id, "no_thanks", channel="notion", by="rosy (Notion)")
            changed.append((suggestion_id, decision))
        except DeliveryError:
            pass
        client.clear_decision(page_id)
    if changed:
        project_suggestions(conn, client)
    return changed


def ensure_notion_options(client):
    """
    Ajoute a la base cockpit EXISTANTE les options manquantes : Décision
    (⏰ Reporter, 📌 Garder comme insight) et Kind (Suggestion). Idempotent.
    """
    schema = client._request("GET", f"/data_sources/{client.data_source_id}")
    wanted = {"Décision": list(NOTION_DECISIONS), "Kind": [NOTION_KIND]}
    patch = {}
    for prop, names in wanted.items():
        current = (schema.get("properties", {}).get(prop) or {}).get("select", {}).get("options", [])
        existing = {o["name"] for o in current}
        missing = [n for n in names if n not in existing]
        if missing:
            patch[prop] = {"select": {"options": [{"id": o["id"], "name": o["name"]}
                                                  for o in current]
                                      + [{"name": n} for n in missing]}}
    if patch:
        client._request("PATCH", f"/data_sources/{client.data_source_id}",
                        {"properties": patch})
    return sorted(patch)
