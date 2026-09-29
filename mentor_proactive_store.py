#!/usr/bin/env python3
"""
mentor_proactive_store.py - Stockage du canal PROACTIF du Mentor (V3, phase 1).

Tables additives dans vault.db (aucune table existante n'est modifiee) :
- mentor_events              : file d'evenements persistante (survit a l'arret du PC) ;
- mentor_candidates          : candidats detectes, y compris REJETES (journal interne) ;
- mentor_suggestions         : suggestions confirmees SUG-xxxx et leur cycle de vie ;
- mentor_agent_consultations : journal des consultations Agency Agents ;
- mentor_watch_state         : derniere empreinte connue d'un objet surveille
                               (detecte les changements de projet sans bruit initial).

Regles :
- migration idempotente (CREATE ... IF NOT EXISTS), sans perte de donnees ;
- un candidat n'est JAMAIS projete dans Notion : seule une suggestion peut
  l'etre (voir `projectable_suggestions`) ;
- aucune action externe n'est creee ici ;
- verrou sur le VRAI Vault tant que la phase proactive n'est pas validee :
  fichier `mentor_proactive.enabled` (non versionne) ou VAULT_PROACTIVE_WRITES=1.
  Les bases temporaires, copies et tests ne sont pas concernes.
"""

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import v2_guard

ROOT = Path(__file__).resolve().parent
FLAG = ROOT / "mentor_proactive.enabled"

PROACTIVE_SCHEMA_VERSION = 1

EVENT_TYPES = (
    "source_ingested",
    "insight_created",
    "insight_validated",
    "task_created",
    "task_completed",
    "task_overdue",
    "project_changed",
    "question_asked",
    "daily_pass",
)
EVENT_STATUSES = ("pending", "processed", "failed", "ignored")
MAX_EVENT_ATTEMPTS = 3

CANDIDATE_TYPES = (
    "convergence",
    "opportunity",
    "reactivation",
    "tension",
    "forgotten",
    "next_action",
)
# detected    : score suffisant, en attente de shortlist ;
# watching    : conserve et re-score a chaque nouvelle preuve, pas presente ;
# shortlisted : retenu pour la revue suivante (Qwen / Agency, phase 2) ;
# rejected    : journalise, jamais presente, jamais projete ;
# promoted    : a donne une suggestion ;
# expired     : perime ou remplace par un candidat plus fort.
CANDIDATE_STATUSES = (
    "detected", "watching", "shortlisted", "rejected", "promoted", "expired",
)
OPEN_CANDIDATE_STATUSES = ("detected", "watching", "shortlisted")

SUGGESTION_STATUSES = (
    "ready", "sent", "opened", "accepted", "dismissed", "snoozed", "expired",
    # Etapes ulterieures (prevues dans le schema, non utilisees en phase 1).
    "action_proposed", "approved", "executed",
)
SUGGESTION_TRANSITIONS = {
    "ready": {"sent", "accepted", "dismissed", "snoozed", "expired"},
    "sent": {"opened", "accepted", "dismissed", "snoozed", "expired"},
    "opened": {"accepted", "dismissed", "snoozed", "expired"},
    "snoozed": {"ready", "accepted", "dismissed", "expired"},
    "accepted": {"action_proposed"},
    "action_proposed": {"approved", "dismissed"},
    "approved": {"executed"},
    "dismissed": set(),
    "expired": set(),
    "executed": set(),
}
# Statuts qui constituent une decision humaine.
DECISION_STATUSES = {"accepted": "accepted", "dismissed": "dismissed", "snoozed": "snoozed"}
# Seules ces suggestions peuvent etre projetees (Notion, plus tard Telegram).
PROJECTABLE_SUGGESTION_STATUSES = (
    "ready", "sent", "opened", "accepted", "snoozed", "action_proposed", "approved",
)

CONSULTATION_MODES = ("A", "B", "C")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mentor_events (
    event_id     TEXT PRIMARY KEY,
    dedup_key    TEXT NOT NULL UNIQUE,
    event_type   TEXT NOT NULL CHECK (event_type IN (
                   'source_ingested', 'insight_created', 'insight_validated',
                   'task_created', 'task_completed', 'task_overdue',
                   'project_changed', 'question_asked', 'daily_pass')),
    entity_type  TEXT,
    entity_id    INTEGER,
    project_id   INTEGER,
    payload      TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'processed', 'failed', 'ignored')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    result       TEXT NOT NULL DEFAULT '{}',
    publisher    TEXT NOT NULL DEFAULT 'vault',
    created_at   TEXT NOT NULL,
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mentor_events_status
    ON mentor_events(status, created_at);
CREATE INDEX IF NOT EXISTS idx_mentor_events_entity
    ON mentor_events(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS mentor_candidates (
    candidate_id     TEXT PRIMARY KEY,
    candidate_type   TEXT NOT NULL CHECK (candidate_type IN (
                       'convergence', 'opportunity', 'reactivation',
                       'tension', 'forgotten', 'next_action')),
    project_id       INTEGER,
    focus_entity_id  INTEGER,
    focus_key        TEXT NOT NULL,
    title            TEXT NOT NULL,
    score            REAL NOT NULL CHECK (score >= 0 AND score <= 1),
    score_breakdown  TEXT NOT NULL DEFAULT '{}',
    evidence         TEXT NOT NULL DEFAULT '{}',
    source_ids       TEXT NOT NULL DEFAULT '[]',
    claim_ids        TEXT NOT NULL DEFAULT '[]',
    task_ids         TEXT NOT NULL DEFAULT '[]',
    insight_ids      TEXT NOT NULL DEFAULT '[]',
    event_ids        TEXT NOT NULL DEFAULT '[]',
    fingerprint      TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'detected'
                     CHECK (status IN ('detected', 'watching', 'shortlisted',
                                       'rejected', 'promoted', 'expired')),
    rejection_reason TEXT,
    seen_count       INTEGER NOT NULL DEFAULT 1,
    detector_version TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    closed_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_mentor_candidates_status
    ON mentor_candidates(status, score);
CREATE INDEX IF NOT EXISTS idx_mentor_candidates_focus
    ON mentor_candidates(candidate_type, focus_key);
CREATE INDEX IF NOT EXISTS idx_mentor_candidates_project
    ON mentor_candidates(project_id);

CREATE TABLE IF NOT EXISTS mentor_suggestions (
    suggestion_id       TEXT PRIMARY KEY,
    candidate_id        TEXT NOT NULL UNIQUE
                        REFERENCES mentor_candidates(candidate_id),
    suggestion_type     TEXT NOT NULL CHECK (suggestion_type IN (
                          'convergence', 'opportunity', 'reactivation',
                          'tension', 'forgotten', 'next_action')),
    title               TEXT NOT NULL,
    summary             TEXT NOT NULL,
    explanation         TEXT,
    project_id          INTEGER,
    score               REAL NOT NULL CHECK (score >= 0 AND score <= 1),
    evidence            TEXT NOT NULL DEFAULT '{}',
    fingerprint         TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL DEFAULT 'ready'
                        CHECK (status IN ('ready', 'sent', 'opened', 'accepted',
                                          'dismissed', 'snoozed', 'expired',
                                          'action_proposed', 'approved', 'executed')),
    decision            TEXT CHECK (decision IS NULL OR decision IN
                                    ('accepted', 'dismissed', 'snoozed')),
    decision_note       TEXT,
    decided_by          TEXT,
    decided_at          TEXT,
    snoozed_until       TEXT,
    notion_page_id      TEXT,
    telegram_message_id TEXT,
    status_history      TEXT NOT NULL DEFAULT '[]',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mentor_suggestions_status
    ON mentor_suggestions(status);

CREATE TABLE IF NOT EXISTS mentor_agent_consultations (
    consultation_id TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    agent_name      TEXT NOT NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('A', 'B', 'C')),
    session_id      TEXT,
    suggestion_id   TEXT REFERENCES mentor_suggestions(suggestion_id),
    candidate_id    TEXT REFERENCES mentor_candidates(candidate_id),
    inputs          TEXT NOT NULL DEFAULT '{}',
    verdict         TEXT,
    value_score     INTEGER CHECK (value_score IS NULL OR
                                   (value_score >= 0 AND value_score <= 5)),
    proposed_action TEXT,
    main_objection  TEXT,
    confidence      REAL CHECK (confidence IS NULL OR
                                (confidence >= 0 AND confidence <= 1)),
    evidence_refs   TEXT NOT NULL DEFAULT '[]',
    model           TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mentor_consultations_suggestion
    ON mentor_agent_consultations(suggestion_id);
CREATE INDEX IF NOT EXISTS idx_mentor_consultations_session
    ON mentor_agent_consultations(session_id);

CREATE TABLE IF NOT EXISTS mentor_watch_state (
    watch_key  TEXT PRIMARY KEY,
    state_hash TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT '{}',
    seen_at    TEXT NOT NULL
);
"""

TABLES = (
    "mentor_events",
    "mentor_candidates",
    "mentor_suggestions",
    "mentor_agent_consultations",
    "mentor_watch_state",
)


class ProactiveStoreError(ValueError):
    pass


class ProactiveWritesBlocked(RuntimeError):
    """Ecriture proactive refusee sur le vrai Vault (phase non validee)."""


# ------------------------------------------------------------------ utilitaires

def now_iso(moment=None):
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value, default=None):
    try:
        parsed = json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed if parsed is not None else default


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _row(row):
    return dict(row) if row is not None else None


# ------------------------------------------------------------------ verrou

def enabled():
    if os.environ.get("VAULT_PROACTIVE_WRITES", "").strip().lower() in (
        "1", "on", "true", "oui",
    ):
        return True
    return FLAG.exists()


def writes_allowed(conn):
    return not v2_guard.is_real_vault(v2_guard.db_path(conn)) or enabled()


def require_writes(conn, what):
    if not writes_allowed(conn):
        raise ProactiveWritesBlocked(
            f"{what} : ecriture proactive bloquee sur le vrai Vault tant que la "
            f"phase n'est pas validee (fichier {FLAG.name} absent). "
            "Utilise --dry-run."
        )


# ------------------------------------------------------------------ schema

def schema_present(conn):
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    return all(table in names for table in TABLES)


def ensure_schema(conn):
    """Cree les tables proactives. Idempotent ; ne touche aucune table existante."""
    require_writes(conn, "schema proactif")
    conn.executescript(SCHEMA)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    current = conn.execute(
        "SELECT value FROM meta WHERE key = 'mentor_proactive_schema_version'"
    ).fetchone()
    if current is None or current[0] != str(PROACTIVE_SCHEMA_VERSION):
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('mentor_proactive_schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(PROACTIVE_SCHEMA_VERSION),),
        )
    conn.commit()
    return conn


# ------------------------------------------------------------------ evenements

def event_id_for(dedup_key):
    return "EVT-" + sha256(dedup_key)[:16]


def default_dedup_key(event_type, entity_type=None, entity_id=None, payload=None):
    if event_type == "question_asked":
        question = " ".join(str((payload or {}).get("question") or "").lower().split())
        if not question:
            raise ProactiveStoreError("question_asked exige payload.question")
        day = str((payload or {}).get("asked_on") or now_iso()[:10])
        return f"question_asked:{sha256(question)[:16]}:{day}"
    if event_type == "daily_pass":
        day = str((payload or {}).get("day") or now_iso()[:10])
        return f"daily_pass:{day}"
    if entity_id is None:
        raise ProactiveStoreError(
            f"{event_type} : entity_id ou dedup_key explicite obligatoire"
        )
    return f"{event_type}:{entity_type or 'entity'}:{entity_id}"


def publish_event(
    conn,
    event_type,
    *,
    entity_type=None,
    entity_id=None,
    project_id=None,
    payload=None,
    dedup_key=None,
    publisher="vault",
    created_at=None,
    commit=True,
):
    """
    Ajoute un evenement a la file. Idempotent : le meme dedup_key donne le
    meme event_id et n'est jamais insere deux fois.
    Retourne (event_id, created).
    """
    if event_type not in EVENT_TYPES:
        raise ProactiveStoreError(f"type d'evenement inconnu : {event_type!r}")
    require_writes(conn, "publication d'evenement")
    if not schema_present(conn):
        ensure_schema(conn)
    payload = dict(payload or {})
    dedup_key = dedup_key or default_dedup_key(
        event_type, entity_type, entity_id, payload
    )
    event_id = event_id_for(dedup_key)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO mentor_events (
            event_id, dedup_key, event_type, entity_type, entity_id,
            project_id, payload, publisher, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id, dedup_key, event_type, entity_type, entity_id,
            project_id, dumps(payload), publisher, created_at or now_iso(),
        ),
    )
    if commit:
        conn.commit()
    return event_id, cursor.rowcount == 1


def get_event(conn, event_id):
    return _row(conn.execute(
        "SELECT * FROM mentor_events WHERE event_id = ?", (event_id,)
    ).fetchone())


def pending_events(conn, limit=200):
    """Evenements a traiter : pending, et failed avec des essais restants."""
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM mentor_events
             WHERE status = 'pending'
                OR (status = 'failed' AND attempts < ?)
             ORDER BY created_at, event_id
             LIMIT ?
            """,
            (MAX_EVENT_ATTEMPTS, int(limit)),
        ).fetchall()
    ]


def mark_event_processed(conn, event_id, result=None, *, at=None):
    conn.execute(
        """
        UPDATE mentor_events
           SET status = 'processed', processed_at = ?, result = ?,
               attempts = attempts + 1, last_error = NULL
         WHERE event_id = ?
        """,
        (at or now_iso(), dumps(result or {}), event_id),
    )


def mark_event_failed(conn, event_id, error, *, at=None):
    conn.execute(
        """
        UPDATE mentor_events
           SET status = 'failed', attempts = attempts + 1,
               last_error = ?, processed_at = ?
         WHERE event_id = ?
        """,
        (str(error)[:500], at or now_iso(), event_id),
    )


def event_counts(conn):
    if not schema_present(conn):
        return {}
    return {
        f"{row[0]}:{row[1]}": row[2]
        for row in conn.execute(
            "SELECT event_type, status, COUNT(*) FROM mentor_events "
            "GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
    }


# ------------------------------------------------------------------ etat surveille

def watch_state(conn, watch_key):
    return _row(conn.execute(
        "SELECT * FROM mentor_watch_state WHERE watch_key = ?", (watch_key,)
    ).fetchone())


def set_watch_state(conn, watch_key, state, *, at=None):
    state_hash = sha256(dumps(state))
    conn.execute(
        """
        INSERT INTO mentor_watch_state (watch_key, state_hash, state, seen_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(watch_key) DO UPDATE SET
            state_hash = excluded.state_hash,
            state = excluded.state,
            seen_at = excluded.seen_at
        """,
        (watch_key, state_hash, dumps(state), at or now_iso()),
    )
    return state_hash


# ------------------------------------------------------------------ candidats

def fingerprint_for(candidate_type, focus_key, *parts):
    """Empreinte de dedoublonnage : type + objet + parties discriminantes."""
    canonical = "|".join([candidate_type, focus_key, *[str(p) for p in parts]])
    return sha256(canonical)


def candidate_id_for(fingerprint):
    return "CAND-" + fingerprint[:12]


def get_candidate(conn, candidate_id):
    return _decode_candidate(conn.execute(
        "SELECT * FROM mentor_candidates WHERE candidate_id = ?", (candidate_id,)
    ).fetchone())


def get_candidate_by_fingerprint(conn, fingerprint):
    return _decode_candidate(conn.execute(
        "SELECT * FROM mentor_candidates WHERE fingerprint = ?", (fingerprint,)
    ).fetchone())


def _decode_candidate(row):
    if row is None:
        return None
    item = dict(row)
    for key in ("source_ids", "claim_ids", "task_ids", "insight_ids", "event_ids"):
        item[key] = loads(item.get(key), [])
    for key in ("evidence", "score_breakdown"):
        item[key] = loads(item.get(key), {})
    return item


def list_candidates(conn, status=None, limit=500):
    if not schema_present(conn):
        return []
    sql = "SELECT * FROM mentor_candidates"
    params = []
    if status:
        statuses = [status] if isinstance(status, str) else list(status)
        sql += f" WHERE status IN ({','.join('?' for _ in statuses)})"
        params.extend(statuses)
    sql += " ORDER BY score DESC, created_at, candidate_id LIMIT ?"
    params.append(int(limit))
    return [_decode_candidate(row) for row in conn.execute(sql, params).fetchall()]


def insert_candidate(conn, record):
    conn.execute(
        """
        INSERT INTO mentor_candidates (
            candidate_id, candidate_type, project_id, focus_entity_id, focus_key,
            title, score, score_breakdown, evidence, source_ids, claim_ids,
            task_ids, insight_ids, event_ids, fingerprint, status,
            rejection_reason, seen_count, detector_version, created_at,
            updated_at, last_seen_at, closed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            record["candidate_id"], record["candidate_type"], record.get("project_id"),
            record.get("focus_entity_id"), record["focus_key"], record["title"],
            record["score"], dumps(record.get("score_breakdown") or {}),
            dumps(record.get("evidence") or {}),
            dumps(record.get("source_ids") or []), dumps(record.get("claim_ids") or []),
            dumps(record.get("task_ids") or []), dumps(record.get("insight_ids") or []),
            dumps(record.get("event_ids") or []), record["fingerprint"],
            record["status"], record.get("rejection_reason"),
            record["detector_version"], record["at"], record["at"], record["at"],
            record["at"] if record["status"] == "rejected" else None,
        ),
    )


def update_candidate(conn, candidate_id, changes, *, at):
    allowed = {
        "project_id", "title", "score", "score_breakdown", "evidence",
        "source_ids", "claim_ids", "task_ids", "insight_ids", "event_ids",
        "status", "rejection_reason", "closed_at",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ProactiveStoreError(f"champs candidat non modifiables : {sorted(unknown)}")
    sets, params = [], []
    for key, value in changes.items():
        if key in {"score_breakdown", "evidence", "source_ids", "claim_ids",
                   "task_ids", "insight_ids", "event_ids"}:
            value = dumps(value)
        sets.append(f"{key} = ?")
        params.append(value)
    sets.extend(["updated_at = ?", "last_seen_at = ?", "seen_count = seen_count + 1"])
    params.extend([at, at, candidate_id])
    conn.execute(
        f"UPDATE mentor_candidates SET {', '.join(sets)} WHERE candidate_id = ?",
        params,
    )


def touch_candidate(conn, candidate_id, event_ids, *, at):
    """Doublon d'un candidat ferme : on note seulement qu'il a ete revu."""
    conn.execute(
        """
        UPDATE mentor_candidates
           SET seen_count = seen_count + 1, last_seen_at = ?, event_ids = ?
         WHERE candidate_id = ?
        """,
        (at, dumps(event_ids), candidate_id),
    )


def set_candidate_status(conn, candidate_id, status, *, reason=None, at=None):
    if status not in CANDIDATE_STATUSES:
        raise ProactiveStoreError(f"statut candidat inconnu : {status!r}")
    at = at or now_iso()
    closed = status in ("rejected", "promoted", "expired")
    conn.execute(
        """
        UPDATE mentor_candidates
           SET status = ?, rejection_reason = COALESCE(?, rejection_reason),
               updated_at = ?, closed_at = CASE WHEN ? THEN ? ELSE closed_at END
         WHERE candidate_id = ?
        """,
        (status, reason, at, 1 if closed else 0, at, candidate_id),
    )


def candidate_counts(conn):
    if not schema_present(conn):
        return {}
    return {
        f"{row[0]}:{row[1]}": row[2]
        for row in conn.execute(
            "SELECT candidate_type, status, COUNT(*) FROM mentor_candidates "
            "GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
    }


# ------------------------------------------------------------------ suggestions

def _next_suggestion_id(conn):
    row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(suggestion_id, 5) AS INTEGER)) FROM mentor_suggestions"
    ).fetchone()
    return f"SUG-{(row[0] or 0) + 1:04d}"


def _decode_suggestion(row):
    if row is None:
        return None
    item = dict(row)
    item["evidence"] = loads(item.get("evidence"), {})
    item["status_history"] = loads(item.get("status_history"), [])
    return item


def get_suggestion(conn, suggestion_id):
    return _decode_suggestion(conn.execute(
        "SELECT * FROM mentor_suggestions WHERE suggestion_id = ?", (suggestion_id,)
    ).fetchone())


def promote_candidate(conn, candidate_id, *, title, summary, explanation=None,
                      score=None, evidence=None, at=None, commit=True):
    """
    Cree la suggestion confirmee d'un candidat shortlist (statut `ready`).
    Point d'entree de la phase 2 (apres Qwen / Agency). Un candidat rejete,
    expire ou deja promu ne peut pas devenir une suggestion.
    """
    require_writes(conn, "creation de suggestion")
    candidate = get_candidate(conn, candidate_id)
    if candidate is None:
        raise ProactiveStoreError(f"candidat introuvable : {candidate_id}")
    if candidate["status"] not in ("shortlisted", "detected"):
        raise ProactiveStoreError(
            f"candidat {candidate_id} au statut {candidate['status']} : non promouvable"
        )
    at = at or now_iso()
    suggestion_id = _next_suggestion_id(conn)
    history = [{"status": "ready", "at": at}]
    conn.execute(
        """
        INSERT INTO mentor_suggestions (
            suggestion_id, candidate_id, suggestion_type, title, summary,
            explanation, project_id, score, evidence, fingerprint, status,
            status_history, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?)
        """,
        (
            suggestion_id, candidate_id, candidate["candidate_type"], title, summary,
            explanation, candidate.get("project_id"),
            candidate["score"] if score is None else float(score),
            dumps(evidence if evidence is not None else candidate["evidence"]),
            candidate["fingerprint"], dumps(history), at, at,
        ),
    )
    set_candidate_status(conn, candidate_id, "promoted", at=at)
    if commit:
        conn.commit()
    return get_suggestion(conn, suggestion_id)


def transition_suggestion(conn, suggestion_id, new_status, *, decided_by=None,
                          note=None, snoozed_until=None, at=None, commit=True):
    """Fait avancer une suggestion dans son cycle de vie (transitions controlees)."""
    require_writes(conn, "cycle de vie de suggestion")
    suggestion = get_suggestion(conn, suggestion_id)
    if suggestion is None:
        raise ProactiveStoreError(f"suggestion introuvable : {suggestion_id}")
    current = suggestion["status"]
    if new_status not in SUGGESTION_TRANSITIONS.get(current, set()):
        raise ProactiveStoreError(f"transition interdite : {current} -> {new_status}")
    if new_status == "snoozed" and not snoozed_until:
        raise ProactiveStoreError("snoozed exige snoozed_until")
    at = at or now_iso()
    history = suggestion["status_history"] + [
        {"status": new_status, "at": at, "by": decided_by}
    ]
    decision = DECISION_STATUSES.get(new_status)
    conn.execute(
        """
        UPDATE mentor_suggestions
           SET status = ?,
               decision = COALESCE(?, decision),
               decision_note = COALESCE(?, decision_note),
               decided_by = CASE WHEN ? IS NOT NULL THEN ? ELSE decided_by END,
               decided_at = CASE WHEN ? IS NOT NULL THEN ? ELSE decided_at END,
               snoozed_until = CASE WHEN ? = 'snoozed' THEN ? ELSE snoozed_until END,
               status_history = ?, updated_at = ?
         WHERE suggestion_id = ?
        """,
        (
            new_status, decision, note,
            decision, decided_by, decision, at,
            new_status, snoozed_until,
            dumps(history), at, suggestion_id,
        ),
    )
    if commit:
        conn.commit()
    return get_suggestion(conn, suggestion_id)


def wake_snoozed(conn, *, at=None):
    """Remet en `ready` les suggestions dont le report est echu."""
    at = at or now_iso()
    woken = []
    for row in conn.execute(
        "SELECT suggestion_id FROM mentor_suggestions "
        "WHERE status = 'snoozed' AND snoozed_until <= ?",
        (at,),
    ).fetchall():
        transition_suggestion(conn, row[0], "ready", decided_by="system", at=at,
                              commit=False)
        woken.append(row[0])
    conn.commit()
    return woken


def projectable_suggestions(conn):
    """
    Ce qu'une projection externe (Notion 🧭 Actions proposees, plus tard
    Telegram) a le droit de lire : uniquement des suggestions confirmees.
    Les candidats, et a fortiori les candidats rejetes, n'y figurent jamais.
    """
    if not schema_present(conn):
        return []
    placeholders = ",".join("?" for _ in PROJECTABLE_SUGGESTION_STATUSES)
    return [
        _decode_suggestion(row)
        for row in conn.execute(
            f"SELECT * FROM mentor_suggestions WHERE status IN ({placeholders}) "
            "ORDER BY created_at, suggestion_id",
            PROJECTABLE_SUGGESTION_STATUSES,
        ).fetchall()
    ]


# ------------------------------------------------------------------ consultations

def record_consultation(
    conn,
    *,
    agent_id,
    agent_name,
    mode,
    inputs,
    verdict=None,
    value_score=None,
    proposed_action=None,
    main_objection=None,
    confidence=None,
    evidence_refs=None,
    session_id=None,
    suggestion_id=None,
    candidate_id=None,
    model=None,
    at=None,
    commit=True,
):
    """
    Journalise une consultation Agency Agents. L'agent vient du registre
    existant (vault_agent_registry) : ce module ne le reconstruit pas.
    """
    require_writes(conn, "journal de consultation")
    if mode not in CONSULTATION_MODES:
        raise ProactiveStoreError(f"mode de consultation inconnu : {mode!r}")
    if value_score is not None and not (0 <= int(value_score) <= 5):
        raise ProactiveStoreError("value_score doit etre entre 0 et 5")
    if confidence is not None and not (0 <= float(confidence) <= 1):
        raise ProactiveStoreError("confidence doit etre entre 0 et 1")
    consultation_id = "CONS-" + uuid.uuid4().hex[:12]
    conn.execute(
        """
        INSERT INTO mentor_agent_consultations (
            consultation_id, agent_id, agent_name, mode, session_id,
            suggestion_id, candidate_id, inputs, verdict, value_score,
            proposed_action, main_objection, confidence, evidence_refs,
            model, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            consultation_id, agent_id, agent_name, mode, session_id,
            suggestion_id, candidate_id, dumps(inputs or {}), verdict,
            None if value_score is None else int(value_score),
            proposed_action, main_objection,
            None if confidence is None else float(confidence),
            dumps(evidence_refs or []), model, at or now_iso(),
        ),
    )
    if commit:
        conn.commit()
    return consultation_id


def list_consultations(conn, *, suggestion_id=None, session_id=None):
    if not schema_present(conn):
        return []
    sql = "SELECT * FROM mentor_agent_consultations WHERE 1 = 1"
    params = []
    if suggestion_id:
        sql += " AND suggestion_id = ?"
        params.append(suggestion_id)
    if session_id:
        sql += " AND session_id = ?"
        params.append(session_id)
    sql += " ORDER BY created_at, consultation_id"
    rows = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        item["inputs"] = loads(item["inputs"], {})
        item["evidence_refs"] = loads(item["evidence_refs"], [])
        rows.append(item)
    return rows
