#!/usr/bin/env python3
"""
mentor_proactive.py - Canal PROACTIF du Mentor Engine (V3, phase 1).

Chaine cible :
    EVENT -> detection deterministe -> candidat -> score -> shortlist
          -> Qwen -> Agency Agents -> suggestion -> decision humaine

Phase 1 (ce module) : EVENTS -> CANDIDATES -> SCORE -> DEDUP -> SHORTLIST.
Aucun LLM, aucun appel reseau, aucune ecriture Notion, aucune action externe.
Qwen et Agency Agents se brancheront sur la shortlist (voir REVIEW_STAGES et
mentor_proactive_store.promote_candidate / record_consultation).

Decouplage : vault_refresh.py ne fait que PUBLIER des evenements
(`publish_vault_events`). La passe se lance separement :

    python mentor_proactive.py                 # = --dry-run
    python mentor_proactive.py --dry-run       # copie en memoire, vault.db intact
    python mentor_proactive.py --confirm       # persiste (bases non verrouillees)
    python mentor_proactive.py --demo          # scenario fictif (4 sources, 1 projet)
    python mentor_proactive.py --status        # etat de la file et des candidats

Le dry-run travaille sur une copie en memoire de la base : il montre
exactement ce qu'une passe reelle ferait, sans aucune ecriture.
"""

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import mentor_proactive_store as store

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "vault.db"

DETECTOR_VERSION = "proactive-v1"

# Un « projet » = entite de pilotage (activite, verticale, client, prospect)
# ou entite rattachee a l'arborescence Notion/Drive (intelligence_entity_bindings).
PROJECT_ENTITY_TYPES = frozenset({"project", "activity", "vertical", "client", "prospect"})
OPEN_ACTION_STATUSES = ("proposed", "approved", "todo", "doing")
COMMITTED_ACTION_STATUSES = ("approved", "todo", "doing")
OPPORTUNITY_INSIGHT_TYPES = ("opportunity", "opportunity_convergence")
VALIDATED_INSIGHT_STATUSES = ("validated", "accepted", "approved", "confirmed")
DISMISSED_INSIGHT_STATUSES = ("dismissed", "rejected", "archived")
TENSION_SEVERITY = {
    "contradiction": 1.0,
    "possible_tension": 0.6,
    "possible_supersession": 0.5,
}
OPEN_ASSESSMENT_STATUSES = ("proposed", "needs_review")

CONVERGENCE_WINDOW_DAYS = 120
CONVERGENCE_MIN_SOURCES = 3
RECENT_DAYS = 30
DORMANT_DAYS = 180
FORGOTTEN_IDLE_DAYS = 21
WATCHING_EXPIRY_DAYS = 60

SCORE_FLOOR = 0.30           # en dessous : rejete (journalise)
SHORTLIST_THRESHOLD = 0.55   # au dessus, et ancre sur un projet : shortlist possible
DEFAULT_SHORTLIST_LIMIT = 5  # nouveaux candidats shortlistes par passe

# Score deterministe : somme ponderee de facteurs dans [0, 1].
# Les poids de chaque type somment a 1 ; le detail est conserve par candidat.
WEIGHTS = {
    "convergence": {
        "volume": 0.30, "independence": 0.20, "recency": 0.15,
        "anchor": 0.20, "insight_support": 0.15,
    },
    "opportunity": {
        "relevance": 0.45, "anchor": 0.25, "validated": 0.15, "recency": 0.15,
    },
    "reactivation": {
        "dormancy": 0.35, "anchor": 0.25, "new_signal": 0.20, "independence": 0.20,
    },
    "tension": {"confidence": 0.40, "severity": 0.30, "anchor": 0.30},
    "forgotten": {"overdue": 0.45, "priority": 0.30, "anchor": 0.25},
    "next_action": {"attention": 0.40, "anchor": 0.30, "momentum": 0.30},
}
PRIORITY_FACTOR = {1: 1.0, 2: 0.75, 3: 0.5, 4: 0.25}

# Phase 2 : etapes de revue appliquees a la shortlist (Qwen, puis Agency
# Agents via vault_orchestrator / vault_agent_registry). Vide en phase 1 :
# aucune n'est appelee automatiquement.
REVIEW_STAGES = ()

TYPE_LABELS = {
    "convergence": "Convergence",
    "opportunity": "Opportunite",
    "reactivation": "Reactivation",
    "tension": "Tension",
    "forgotten": "Oubli",
    "next_action": "Prochaine action",
}


# ------------------------------------------------------------------ dates

def _parse_dt(value):
    if not value:
        return None
    text = str(value).strip()
    try:
        if len(text) == 10:
            parsed = datetime.combine(date.fromisoformat(text), datetime.min.time())
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _days_between(older, newer):
    return (newer - older).total_seconds() / 86400.0


def _clamp(value):
    return max(0.0, min(1.0, float(value)))


def _author_key(author):
    key = " ".join(str(author or "").lower().lstrip("@").split())
    return key or "(auteur inconnu)"


# ------------------------------------------------------------------ lecture du Vault

class VaultView:
    """Lecture seule des tables existantes, tolerante aux tables absentes."""

    def __init__(self, conn, now=None):
        self.conn = conn
        self.now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.today = self.now.date()
        self._tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self._entities = {}
        self._sources = {}
        self._sensitive = {}

    def has(self, table):
        return table in self._tables

    def _all(self, sql, params=()):
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def _one(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    # entites / projets
    def entity(self, entity_id):
        if entity_id not in self._entities:
            row = None
            if self.has("intelligence_entities"):
                row = self._one(
                    "SELECT id, entity_type, name, attention_status, updated_at "
                    "FROM intelligence_entities WHERE id = ?",
                    (entity_id,),
                )
            if row is not None:
                row["bound"] = False
                row["parent_entity_id"] = None
                if self.has("intelligence_entity_bindings"):
                    binding = self._one(
                        "SELECT parent_entity_id FROM intelligence_entity_bindings "
                        "WHERE entity_id = ?",
                        (entity_id,),
                    )
                    if binding is not None:
                        row["bound"] = True
                        row["parent_entity_id"] = binding["parent_entity_id"]
            self._entities[entity_id] = row
        return self._entities[entity_id]

    def is_project(self, entity_id):
        entity = self.entity(entity_id)
        return bool(entity) and (
            entity["entity_type"] in PROJECT_ENTITY_TYPES or entity["bound"]
        )

    def project_entities(self):
        if not self.has("intelligence_entities"):
            return []
        return [
            row["id"]
            for row in self._all("SELECT id FROM intelligence_entities ORDER BY id")
            if self.is_project(row["id"])
        ]

    def anchor(self, entity_id=None, project_id=None):
        """1 = rattache a un projet ; 0,7 = entite marquee Maintenant ; 0 sinon."""
        if project_id is not None:
            return 1.0
        entity = self.entity(entity_id) if entity_id is not None else None
        if entity and entity["attention_status"] == "now":
            return 0.7
        return 0.0

    # sources
    def source(self, source_id):
        if source_id not in self._sources:
            row = None
            if self.has("intelligence_sources"):
                row = self._one(
                    "SELECT id, source_url, title, author, content_id, captured_at, "
                    "created_at FROM intelligence_sources WHERE id = ?",
                    (source_id,),
                )
            if row is not None:
                row["date"] = (
                    _parse_dt(row.get("captured_at"))
                    or _parse_dt(row.get("created_at"))
                    or self.now
                )
            self._sources[source_id] = row
        return self._sources[source_id]

    def sources_linked(self, entity_id):
        if not self.has("intelligence_entity_sources"):
            return []
        rows = self._all(
            "SELECT DISTINCT source_id FROM intelligence_entity_sources "
            "WHERE entity_id = ? AND role <> 'primary' ORDER BY source_id",
            (entity_id,),
        )
        return [s for s in (self.source(r["source_id"]) for r in rows) if s]

    def entities_for_source(self, source_id):
        if not self.has("intelligence_entity_sources"):
            return []
        return [
            row["entity_id"]
            for row in self._all(
                "SELECT DISTINCT entity_id FROM intelligence_entity_sources "
                "WHERE source_id = ? AND role <> 'primary' ORDER BY entity_id",
                (source_id,),
            )
            if self.entity(row["entity_id"])
        ]

    def projects_for_source(self, source_id):
        return [e for e in self.entities_for_source(source_id) if self.is_project(e)]

    def source_is_sensitive(self, source_id):
        if source_id not in self._sensitive:
            source = self.source(source_id)
            sensitive = False
            if source is not None:
                import vault_intelligence_signals as signals

                sensitive = signals._source_is_sensitive(self.conn, source)
            self._sensitive[source_id] = sensitive
        return self._sensitive[source_id]

    # insights du Mentor
    def insight(self, insight_id):
        if not self.has("intelligence_mentor_insights_local"):
            return None
        return self._one(
            "SELECT * FROM intelligence_mentor_insights_local WHERE id = ?",
            (insight_id,),
        )

    def insight_for_source(self, source_id):
        if not self.has("intelligence_mentor_insights_local"):
            return None
        return self._one(
            "SELECT * FROM intelligence_mentor_insights_local WHERE source_id = ?",
            (source_id,),
        )

    # taches (actions V2)
    def action(self, action_id):
        if not self.has("actions"):
            return None
        return self._one("SELECT * FROM actions WHERE id = ?", (action_id,))

    def entities_for_action(self, action):
        """Entites reliees explicitement a une tache (resource, puis contenu)."""
        found = []
        if action.get("resource_id") and self.has("intelligence_resource_links"):
            row = self._one(
                "SELECT entity_id FROM intelligence_resource_links WHERE resource_id = ?",
                (action["resource_id"],),
            )
            if row:
                found.append(row["entity_id"])
        if action.get("content_id") and self.has("intelligence_sources"):
            for row in self._all(
                "SELECT id FROM intelligence_sources WHERE content_id = ?",
                (action["content_id"],),
            ):
                for entity_id in self.entities_for_source(row["id"]):
                    if entity_id not in found:
                        found.append(entity_id)
        return [e for e in found if self.entity(e)]

    def open_actions_for_entity(self, entity_id, exclude_action_id=None):
        if not self.has("actions"):
            return []
        placeholders = ",".join("?" for _ in OPEN_ACTION_STATUSES)
        ids = set()
        if self.has("intelligence_resource_links"):
            for row in self._all(
                f"SELECT a.id FROM actions a JOIN intelligence_resource_links l "
                f"ON l.resource_id = a.resource_id WHERE l.entity_id = ? "
                f"AND a.status IN ({placeholders})",
                (entity_id, *OPEN_ACTION_STATUSES),
            ):
                ids.add(row["id"])
        if self.has("intelligence_entity_sources") and self.has("intelligence_sources"):
            for row in self._all(
                f"SELECT DISTINCT a.id FROM actions a "
                f"JOIN intelligence_sources s ON s.content_id = a.content_id "
                f"JOIN intelligence_entity_sources es ON es.source_id = s.id "
                f"WHERE es.entity_id = ? AND es.role <> 'primary' "
                f"AND a.status IN ({placeholders})",
                (entity_id, *OPEN_ACTION_STATUSES),
            ):
                ids.add(row["id"])
        ids.discard(exclude_action_id)
        return sorted(ids)

    def last_activity(self, entity_id):
        dates = [s["date"] for s in self.sources_linked(entity_id)]
        if self.has("actions") and self.has("intelligence_resource_links"):
            for row in self._all(
                "SELECT a.updated_at FROM actions a JOIN intelligence_resource_links l "
                "ON l.resource_id = a.resource_id WHERE l.entity_id = ?",
                (entity_id,),
            ):
                parsed = _parse_dt(row["updated_at"])
                if parsed:
                    dates.append(parsed)
        return max(dates) if dates else None


# ------------------------------------------------------------------ publication

def _project_state(view, entity_id):
    entity = view.entity(entity_id)
    return {
        "name": entity["name"],
        "entity_type": entity["entity_type"],
        "attention_status": entity["attention_status"],
        "parent_entity_id": entity["parent_entity_id"],
    }


def publish_vault_events(conn, now=None):
    """
    Derive les evenements de l'etat du Vault et les ajoute a la file.
    Idempotent (dedup_key stable) : relancer ne cree aucun doublon.
    Appele par vault_refresh.py ; ne lance aucune detection.
    """
    store.ensure_schema(conn)
    view = VaultView(conn, now)
    at = store.now_iso(view.now)
    counts = Counter()

    def publish(event_type, **kwargs):
        _, created = store.publish_event(
            conn, event_type, created_at=at, commit=False, **kwargs
        )
        counts[f"{event_type}:{'new' if created else 'known'}"] += 1

    if view.has("intelligence_sources"):
        for row in view._all(
            "SELECT id, content_id, title, author, captured_at "
            "FROM intelligence_sources ORDER BY id"
        ):
            projects = view.projects_for_source(row["id"])
            publish(
                "source_ingested",
                entity_type="source",
                entity_id=row["id"],
                project_id=projects[0] if projects else None,
                payload={
                    "source_id": row["id"],
                    "content_id": row["content_id"],
                    "title": row["title"],
                    "author": row["author"],
                    "captured_at": row["captured_at"],
                },
            )

    if view.has("intelligence_mentor_insights_local"):
        for row in view._all(
            "SELECT id, source_id, insight_type, relevance_score, status "
            "FROM intelligence_mentor_insights_local ORDER BY id"
        ):
            projects = view.projects_for_source(row["source_id"])
            payload = {
                "insight_id": row["id"],
                "source_id": row["source_id"],
                "insight_type": row["insight_type"],
                "relevance_score": row["relevance_score"],
                "status": row["status"],
            }
            kwargs = dict(
                entity_type="insight",
                entity_id=row["id"],
                project_id=projects[0] if projects else None,
                payload=payload,
            )
            publish("insight_created", **kwargs)
            if (row["status"] or "").lower() in VALIDATED_INSIGHT_STATUSES:
                publish("insight_validated", **kwargs)

    if view.has("actions"):
        for row in view._all("SELECT * FROM actions ORDER BY id"):
            entities = view.entities_for_action(row)
            projects = [e for e in entities if view.is_project(e)]
            kwargs = dict(
                entity_type="task",
                entity_id=row["id"],
                project_id=projects[0] if projects else None,
            )
            base = {
                "task_id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "due_date": row["due_date"],
                "priority": row["priority"],
            }
            publish("task_created", payload=base, **kwargs)
            if row["status"] == "done":
                publish("task_completed", payload=base, **kwargs)
            due = _parse_dt(row["due_date"])
            if (
                row["status"] in COMMITTED_ACTION_STATUSES
                and due is not None
                and due.date() < view.today
            ):
                publish(
                    "task_overdue",
                    payload=base,
                    dedup_key=f"task_overdue:task:{row['id']}:{row['due_date']}",
                    **kwargs,
                )

    # Projets : evenement seulement sur CHANGEMENT (la premiere observation
    # sert de reference, sans bruit initial).
    for entity_id in view.project_entities():
        state = _project_state(view, entity_id)
        watch_key = f"project:{entity_id}"
        previous = store.watch_state(conn, watch_key)
        new_hash = store.sha256(store.dumps(state))
        if previous is None:
            store.set_watch_state(conn, watch_key, state, at=at)
            counts["project_baseline"] += 1
            continue
        if previous["state_hash"] == new_hash:
            continue
        publish(
            "project_changed",
            entity_type="project",
            entity_id=entity_id,
            project_id=entity_id,
            payload={"before": store.loads(previous["state"], {}), "after": state},
            dedup_key=(
                f"project_changed:project:{entity_id}:"
                f"{previous['state_hash'][:10]}->{new_hash[:10]}"
            ),
        )
        store.set_watch_state(conn, watch_key, state, at=at)

    publish("daily_pass", payload={"day": view.today.isoformat()})
    conn.commit()
    return dict(sorted(counts.items()))


def publish_question(conn, question, source_ids=(), now=None):
    """Publie un evenement question_asked (meme question, meme jour = 1 evenement)."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return store.publish_event(
        conn,
        "question_asked",
        entity_type="question",
        payload={
            "question": question,
            "source_ids": [int(s) for s in source_ids],
            "asked_on": moment.date().isoformat(),
        },
        created_at=store.now_iso(moment),
    )


# ------------------------------------------------------------------ score

def score_candidate(candidate_type, factors):
    """Score deterministe dans [0, 1] et son detail facteur par facteur."""
    weights = WEIGHTS[candidate_type]
    if set(factors) != set(weights):
        raise ValueError(
            f"{candidate_type} : facteurs attendus {sorted(weights)}, recus {sorted(factors)}"
        )
    breakdown = {}
    total = 0.0
    for name, weight in weights.items():
        value = round(_clamp(factors[name]), 3)
        contribution = round(value * weight, 4)
        breakdown[name] = {"value": value, "weight": weight, "contribution": contribution}
        total += contribution
    return round(_clamp(total), 3), breakdown


def _draft(candidate_type, focus_key, parts, *, title, factors, project_id=None,
           focus_entity_id=None, source_ids=(), claim_ids=(), task_ids=(),
           insight_ids=(), evidence=None, reject=None, rank=0):
    return {
        "candidate_type": candidate_type,
        "focus_key": focus_key,
        "fingerprint": store.fingerprint_for(candidate_type, focus_key, *parts),
        "title": title,
        "factors": factors,
        "project_id": project_id,
        "focus_entity_id": focus_entity_id,
        "source_ids": sorted(set(source_ids)),
        "claim_ids": sorted(set(claim_ids)),
        "task_ids": sorted(set(task_ids)),
        "insight_ids": sorted(set(insight_ids)),
        "evidence": evidence or {},
        "reject": reject,
        "rank": rank,
    }


def _entity_brief(view, entity_id):
    entity = view.entity(entity_id)
    return {
        "id": entity_id,
        "name": entity["name"] if entity else None,
        "type": entity["entity_type"] if entity else None,
        "attention": entity["attention_status"] if entity else None,
    }


def _source_brief(source):
    return {
        "id": source["id"],
        "title": source.get("title"),
        "author": source.get("author"),
        "date": source["date"].date().isoformat(),
    }


# ------------------------------------------------------------------ detecteurs

def detect_convergence(view, entity_id):
    entity = view.entity(entity_id)
    if not entity:
        return []
    window_start = view.now - timedelta(days=CONVERGENCE_WINDOW_DAYS)
    sources = [s for s in view.sources_linked(entity_id) if s["date"] >= window_start]
    count = len(sources)
    if count < CONVERGENCE_MIN_SOURCES:
        return []

    authors = {_author_key(s.get("author")) for s in sources}
    recent = sum(1 for s in sources if _days_between(s["date"], view.now) <= RECENT_DAYS)
    with_insight = sum(1 for s in sources if view.insight_for_source(s["id"]))
    sensitive = sum(1 for s in sources if view.source_is_sensitive(s["id"]))
    project_id = entity_id if view.is_project(entity_id) else None
    tier, rank = ("t1", 1) if count <= 4 else ("t2", 2) if count <= 7 else ("t3", 3)

    reject = None
    if entity["attention_status"] == "dismissed":
        reject = "entity_dismissed"
    elif len(authors) < 2:
        reject = "same_author_only"
    elif sensitive * 2 >= count:
        reject = "sensitive_topic"

    return [_draft(
        "convergence",
        f"entity:{entity_id}",
        ("tier", tier),
        title=f"Convergence : {count} sources sur '{entity['name']}'",
        factors={
            "volume": (count - 2) / 4,
            "independence": len(authors) / count,
            "recency": recent / count,
            "anchor": view.anchor(entity_id, project_id),
            "insight_support": with_insight / count,
        },
        project_id=project_id,
        focus_entity_id=entity_id,
        source_ids=[s["id"] for s in sources],
        insight_ids=[
            i["id"] for i in (view.insight_for_source(s["id"]) for s in sources) if i
        ],
        evidence={
            "focus": _entity_brief(view, entity_id),
            "sources": [_source_brief(s) for s in sources],
            "distinct_authors": sorted(authors),
            "window_days": CONVERGENCE_WINDOW_DAYS,
            "tier": tier,
            "sensitive_sources": sensitive,
        },
        reject=reject,
        rank=rank,
    )]


def detect_reactivation(view, entity_id, source_id):
    entity = view.entity(entity_id)
    new = view.source(source_id)
    if not entity or not new:
        return []
    older = [
        s for s in view.sources_linked(entity_id)
        if s["id"] != source_id and s["date"] < new["date"]
    ]
    if not older:
        return []
    last = max(older, key=lambda s: s["date"])
    gap = _days_between(last["date"], new["date"])
    if gap < DORMANT_DAYS:
        return []
    project_id = entity_id if view.is_project(entity_id) else None
    insight = view.insight_for_source(source_id)
    old_authors = {_author_key(s.get("author")) for s in older}
    reject = None
    if entity["attention_status"] == "dismissed":
        reject = "entity_dismissed"
    elif view.source_is_sensitive(source_id):
        reject = "sensitive_topic"
    return [_draft(
        "reactivation",
        f"entity:{entity_id}",
        ("source", source_id),
        title=f"Reactivation : '{entity['name']}' revient apres {int(gap)} jours",
        factors={
            "dormancy": gap / 365,
            "anchor": view.anchor(entity_id, project_id),
            "new_signal": float(insight["relevance_score"]) if insight else 0.0,
            "independence": 0.5 if _author_key(new.get("author")) in old_authors else 1.0,
        },
        project_id=project_id,
        focus_entity_id=entity_id,
        source_ids=[source_id, last["id"]],
        insight_ids=[insight["id"]] if insight else [],
        evidence={
            "focus": _entity_brief(view, entity_id),
            "new_source": _source_brief(new),
            "previous_source": _source_brief(last),
            "gap_days": int(gap),
        },
        reject=reject,
    )]


def detect_reactivation_from_question(view, payload, event_id):
    drafts = []
    question = str(payload.get("question") or "")
    for source_id in payload.get("source_ids") or []:
        if not view.source(source_id):
            continue
        for entity_id in view.entities_for_source(source_id):
            entity = view.entity(entity_id)
            last = view.last_activity(entity_id)
            if last is None:
                continue
            idle = _days_between(last, view.now)
            if idle < DORMANT_DAYS:
                continue
            project_id = entity_id if view.is_project(entity_id) else None
            reject = None
            if entity["attention_status"] == "dismissed":
                reject = "entity_dismissed"
            elif view.source_is_sensitive(source_id):
                reject = "sensitive_topic"
            drafts.append(_draft(
                "reactivation",
                f"entity:{entity_id}",
                ("question", event_id),
                title=(
                    f"Reactivation : question sur '{entity['name']}', "
                    f"inactif depuis {int(idle)} jours"
                ),
                factors={
                    "dormancy": idle / 365,
                    "anchor": view.anchor(entity_id, project_id),
                    # Une question explicite de Rosy est un signal d'interet fort.
                    "new_signal": 1.0,
                    "independence": 1.0,
                },
                project_id=project_id,
                focus_entity_id=entity_id,
                source_ids=[source_id],
                evidence={
                    "focus": _entity_brief(view, entity_id),
                    "question": question[:300],
                    "idle_days": int(idle),
                },
                reject=reject,
            ))
    return drafts


def detect_opportunity(view, insight_id, validated=False):
    insight = view.insight(insight_id)
    if not insight or insight["insight_type"] not in OPPORTUNITY_INSIGHT_TYPES:
        return []
    source = view.source(insight["source_id"])
    if not source:
        return []
    projects = view.projects_for_source(source["id"])
    project_id = projects[0] if projects else None
    entities = view.entities_for_source(source["id"])
    anchor = view.anchor(project_id=project_id) if project_id else max(
        [view.anchor(e) for e in entities] or [0.0]
    )
    status = (insight.get("status") or "").lower()
    age = _days_between(source["date"], view.now)
    reject = None
    if status in DISMISSED_INSIGHT_STATUSES:
        reject = "insight_dismissed"
    elif view.source_is_sensitive(source["id"]):
        reject = "sensitive_topic"
    return [_draft(
        "opportunity",
        f"source:{source['id']}",
        ("insight", insight_id),
        title=f"Opportunite : {(source.get('title') or 'source %s' % source['id'])[:90]}",
        factors={
            "relevance": float(insight["relevance_score"] or 0),
            "anchor": anchor,
            "validated": 1.0 if (validated or status in VALIDATED_INSIGHT_STATUSES) else 0.0,
            "recency": 1.0 - max(0.0, age - RECENT_DAYS) / 150,
        },
        project_id=project_id,
        focus_entity_id=project_id,
        source_ids=[source["id"]],
        insight_ids=[insight_id],
        evidence={
            "source": _source_brief(source),
            "insight_type": insight["insight_type"],
            "insight_status": insight.get("status"),
            "observation": (insight.get("observation") or "")[:300],
            "projects": [_entity_brief(view, p) for p in projects],
        },
        reject=reject,
    )]


def detect_tensions(view, source_ids=None):
    if not view.has("intelligence_claim_assessments"):
        return []
    kinds = tuple(TENSION_SEVERITY)
    rows = view._all(
        f"SELECT * FROM intelligence_claim_assessments "
        f"WHERE kind IN ({','.join('?' for _ in kinds)}) "
        f"AND status IN ({','.join('?' for _ in OPEN_ASSESSMENT_STATUSES)}) "
        f"AND stale_at IS NULL ORDER BY id",
        (*kinds, *OPEN_ASSESSMENT_STATUSES),
    )
    wanted = set(source_ids or [])
    drafts = []
    for row in rows:
        sids = [int(s) for s in store.loads(row["source_ids"], [])]
        if wanted and not wanted & set(sids):
            continue
        projects = sorted({p for s in sids for p in view.projects_for_source(s)})
        project_id = projects[0] if projects else None
        entity_anchor = max(
            [view.anchor(e) for s in sids for e in view.entities_for_source(s)] or [0.0]
        )
        reject = "sensitive_topic" if any(view.source_is_sensitive(s) for s in sids) else None
        drafts.append(_draft(
            "tension",
            f"assessment:{row['id']}",
            ("assessment", row["id"]),
            title=f"Tension ({row['kind']}) : {row['summary'][:90]}",
            factors={
                "confidence": float(row["confidence"] or 0),
                "severity": TENSION_SEVERITY[row["kind"]],
                "anchor": 1.0 if project_id else entity_anchor,
            },
            project_id=project_id,
            source_ids=sids,
            claim_ids=[int(c) for c in store.loads(row["claim_ids"], [])],
            evidence={
                "assessment_id": row["id"],
                "kind": row["kind"],
                "summary": row["summary"],
                "subject_key": row.get("subject_key"),
            },
            reject=reject,
        ))
    return drafts


def detect_overdue_task(view, action_id):
    action = view.action(action_id)
    if not action or action["status"] not in COMMITTED_ACTION_STATUSES:
        return []
    due = _parse_dt(action.get("due_date"))
    if due is None or due.date() >= view.today:
        return []
    days = (view.today - due.date()).days
    entities = view.entities_for_action(action)
    projects = [e for e in entities if view.is_project(e)]
    project_id = projects[0] if projects else None
    anchor = 1.0 if project_id else max([view.anchor(e) for e in entities] or [0.0])
    return [_draft(
        "forgotten",
        f"task:{action_id}",
        ("task", action_id, "due", action["due_date"]),
        title=f"Oubli : '{action['title'][:80]}' en retard de {days} jour(s)",
        factors={
            "overdue": days / 14,
            "priority": PRIORITY_FACTOR.get(action.get("priority"), 0.5),
            "anchor": anchor,
        },
        project_id=project_id,
        focus_entity_id=project_id,
        task_ids=[action_id],
        evidence={
            "task": {
                "id": action_id,
                "title": action["title"],
                "status": action["status"],
                "due_date": action["due_date"],
                "priority": action.get("priority"),
            },
            "overdue_days": days,
            "entities": [_entity_brief(view, e) for e in entities],
        },
    )]


def detect_idle_now_entities(view):
    if not view.has("intelligence_entities"):
        return []
    drafts = []
    for row in view._all(
        "SELECT id FROM intelligence_entities WHERE attention_status = 'now' ORDER BY id"
    ):
        entity_id = row["id"]
        entity = view.entity(entity_id)
        last = view.last_activity(entity_id)
        idle = _days_between(last, view.now) if last else None
        if idle is not None and idle < FORGOTTEN_IDLE_DAYS:
            continue
        project_id = entity_id if view.is_project(entity_id) else None
        since = last.date().isoformat() if last else "never"
        drafts.append(_draft(
            "forgotten",
            f"entity:{entity_id}",
            ("idle_since", since),
            title=(
                f"Oubli : '{entity['name']}' marque Maintenant, "
                + (f"sans activite depuis {int(idle)} jours" if idle else "sans activite")
            ),
            factors={
                "overdue": (idle / (2 * FORGOTTEN_IDLE_DAYS)) if idle else 1.0,
                "priority": PRIORITY_FACTOR[2],
                "anchor": view.anchor(entity_id, project_id),
            },
            project_id=project_id,
            focus_entity_id=entity_id,
            evidence={
                "focus": _entity_brief(view, entity_id),
                "last_activity": since,
                "idle_days": int(idle) if idle is not None else None,
            },
        ))
    return drafts


def detect_next_action_after_task(view, action_id):
    action = view.action(action_id)
    if not action or action["status"] != "done":
        return []
    completed = _parse_dt(action.get("updated_at")) or view.now
    age = _days_between(completed, view.now)
    drafts = []
    for entity_id in view.entities_for_action(action):
        entity = view.entity(entity_id)
        is_project = view.is_project(entity_id)
        if not is_project and entity["attention_status"] != "now":
            continue  # ne pas inventer un projet a poursuivre
        if entity["attention_status"] == "dismissed":
            continue
        if view.open_actions_for_entity(entity_id, exclude_action_id=action_id):
            continue
        project_id = entity_id if is_project else None
        drafts.append(_draft(
            "next_action",
            f"entity:{entity_id}",
            ("after_task", action_id),
            title=(
                f"Prochaine action : '{entity['name']}' n'a plus de tache ouverte "
                f"apres '{action['title'][:60]}'"
            ),
            factors={
                "attention": 1.0 if entity["attention_status"] == "now" else 0.5,
                "anchor": view.anchor(entity_id, project_id),
                "momentum": 1.0 - min(1.0, age / 14),
            },
            project_id=project_id,
            focus_entity_id=entity_id,
            task_ids=[action_id],
            evidence={
                "focus": _entity_brief(view, entity_id),
                "completed_task": {"id": action_id, "title": action["title"]},
            },
        ))
    return drafts


def detect_next_action_for_entity(view, entity_id):
    entity = view.entity(entity_id)
    if not entity or entity["attention_status"] != "now":
        return []
    if view.open_actions_for_entity(entity_id):
        return []
    project_id = entity_id if view.is_project(entity_id) else None
    return [_draft(
        "next_action",
        f"entity:{entity_id}",
        ("now_since", entity.get("updated_at")),
        title=f"Prochaine action : '{entity['name']}' est Maintenant sans tache ouverte",
        factors={
            "attention": 1.0,
            "anchor": view.anchor(entity_id, project_id),
            "momentum": 0.5,
        },
        project_id=project_id,
        focus_entity_id=entity_id,
        evidence={"focus": _entity_brief(view, entity_id)},
    )]


def detect(view, event):
    """Evenement -> brouillons de candidats. Deterministe, sans ecriture."""
    event_type = event["event_type"]
    entity_id = event.get("entity_id")
    payload = store.loads(event.get("payload"), {})

    if event_type == "source_ingested":
        drafts = []
        for linked in view.entities_for_source(entity_id):
            drafts += detect_convergence(view, linked)
            drafts += detect_reactivation(view, linked, entity_id)
        return drafts + detect_tensions(view, [entity_id])
    if event_type == "insight_created":
        return detect_opportunity(view, entity_id)
    if event_type == "insight_validated":
        return detect_opportunity(view, entity_id, validated=True)
    if event_type == "task_created":
        return []
    if event_type == "task_completed":
        return detect_next_action_after_task(view, entity_id)
    if event_type == "task_overdue":
        return detect_overdue_task(view, entity_id)
    if event_type == "project_changed":
        return detect_next_action_for_entity(view, entity_id) + detect_convergence(
            view, entity_id
        )
    if event_type == "question_asked":
        return detect_reactivation_from_question(view, payload, event["event_id"])
    if event_type == "daily_pass":
        drafts = []
        if view.has("intelligence_entity_sources"):
            for row in view._all(
                "SELECT entity_id FROM intelligence_entity_sources WHERE role <> 'primary' "
                "GROUP BY entity_id HAVING COUNT(DISTINCT source_id) >= ? ORDER BY entity_id",
                (CONVERGENCE_MIN_SOURCES,),
            ):
                drafts += detect_convergence(view, row["entity_id"])
        if view.has("actions"):
            for row in view._all("SELECT id FROM actions ORDER BY id"):
                drafts += detect_overdue_task(view, row["id"])
        drafts += detect_idle_now_entities(view)
        for project_id in view.project_entities():
            drafts += detect_next_action_for_entity(view, project_id)
        return drafts + detect_tensions(view)
    raise ValueError(f"type d'evenement non gere : {event_type}")


# ------------------------------------------------------------------ candidats

def _classify(draft, score):
    if draft["reject"]:
        return "rejected", draft["reject"], None
    if score < SCORE_FLOOR:
        return "rejected", f"below_floor:{score:.3f}", None
    if draft["factors"]["anchor"] <= 0:
        return "watching", None, "no_project_anchor"
    if score < SHORTLIST_THRESHOLD:
        return "watching", None, "below_shortlist_threshold"
    return "detected", None, None


def upsert_candidate(conn, draft, event_id, *, at):
    """Cree, met a jour ou dedoublonne un candidat. Retourne (outcome, candidate_id)."""
    score, breakdown = score_candidate(draft["candidate_type"], draft["factors"])
    status, rejection, watch_reason = _classify(draft, score)
    evidence = dict(draft["evidence"], rank=draft["rank"])
    if watch_reason:
        evidence["watch_reason"] = watch_reason

    # Un candidat plus fort sur le meme objet (ex. convergence t2 vs t1) l'emporte.
    if draft["rank"]:
        for row in conn.execute(
            "SELECT candidate_id, evidence, status FROM mentor_candidates "
            "WHERE candidate_type = ? AND focus_key = ? AND fingerprint <> ?",
            (draft["candidate_type"], draft["focus_key"], draft["fingerprint"]),
        ).fetchall():
            other_rank = store.loads(row["evidence"], {}).get("rank", 0)
            if other_rank > draft["rank"]:
                return "superseded", row["candidate_id"]

    existing = store.get_candidate_by_fingerprint(conn, draft["fingerprint"])
    if existing is None:
        candidate_id = store.candidate_id_for(draft["fingerprint"])
        store.insert_candidate(conn, {
            **draft,
            "candidate_id": candidate_id,
            "score": score,
            "score_breakdown": breakdown,
            "evidence": evidence,
            "event_ids": [event_id],
            "status": status,
            "rejection_reason": rejection,
            "detector_version": DETECTOR_VERSION,
            "at": at,
        })
        if draft["rank"] and status != "rejected":
            _expire_weaker(conn, draft, candidate_id, at)
        return ("rejected" if status == "rejected" else "created"), candidate_id

    candidate_id = existing["candidate_id"]
    event_ids = existing["event_ids"] + (
        [event_id] if event_id not in existing["event_ids"] else []
    )
    if existing["status"] not in store.OPEN_CANDIDATE_STATUSES:
        # Ferme (rejete, promu, expire) : jamais rouvert, simplement revu.
        store.touch_candidate(conn, candidate_id, event_ids, at=at)
        return "duplicate_closed", candidate_id

    unchanged = (
        existing["score"] == score
        and existing["source_ids"] == draft["source_ids"]
        and existing["claim_ids"] == draft["claim_ids"]
        and existing["task_ids"] == draft["task_ids"]
        and existing["insight_ids"] == draft["insight_ids"]
    )
    if status == "detected" and existing["status"] == "shortlisted":
        status = "shortlisted"
    store.update_candidate(conn, candidate_id, {
        "project_id": draft["project_id"],
        "title": draft["title"],
        "score": score,
        "score_breakdown": breakdown,
        "evidence": evidence,
        "source_ids": draft["source_ids"],
        "claim_ids": draft["claim_ids"],
        "task_ids": draft["task_ids"],
        "insight_ids": draft["insight_ids"],
        "event_ids": event_ids,
        "status": status,
        "rejection_reason": rejection,
        "closed_at": at if status == "rejected" else None,
    }, at=at)
    if status == "rejected":
        return "rejected", candidate_id
    return ("deduplicated" if unchanged else "updated"), candidate_id


def _expire_weaker(conn, draft, candidate_id, at):
    for row in conn.execute(
        "SELECT candidate_id, evidence FROM mentor_candidates "
        "WHERE candidate_type = ? AND focus_key = ? AND candidate_id <> ? "
        f"AND status IN ({','.join('?' for _ in store.OPEN_CANDIDATE_STATUSES)})",
        (draft["candidate_type"], draft["focus_key"], candidate_id,
         *store.OPEN_CANDIDATE_STATUSES),
    ).fetchall():
        if store.loads(row["evidence"], {}).get("rank", 0) < draft["rank"]:
            store.set_candidate_status(
                conn, row["candidate_id"], "expired",
                reason=f"superseded_by:{candidate_id}", at=at,
            )


def expire_stale_watching(conn, now, days=WATCHING_EXPIRY_DAYS):
    limit = store.now_iso(now - timedelta(days=days))
    expired = [
        row[0]
        for row in conn.execute(
            "SELECT candidate_id FROM mentor_candidates "
            "WHERE status = 'watching' AND last_seen_at < ?",
            (limit,),
        ).fetchall()
    ]
    for candidate_id in expired:
        store.set_candidate_status(
            conn, candidate_id, "expired", reason=f"stale_{days}d", at=store.now_iso(now)
        )
    return expired


def apply_shortlist(conn, limit, *, at):
    """Les meilleurs candidats `detected` passent en `shortlisted` (max `limit`)."""
    promoted = []
    rows = conn.execute(
        "SELECT candidate_id FROM mentor_candidates WHERE status = 'detected' "
        "ORDER BY score DESC, created_at, candidate_id LIMIT ?",
        (max(0, int(limit)),),
    ).fetchall()
    for row in rows:
        store.set_candidate_status(conn, row[0], "shortlisted", at=at)
        promoted.append(row[0])
    return promoted


# ------------------------------------------------------------------ passe

def run_pass(conn, *, now=None, limit=500, shortlist_limit=DEFAULT_SHORTLIST_LIMIT,
             scan=True):
    """
    Passe proactive : (publication) -> evenements en attente -> candidats ->
    score -> dedoublonnage -> shortlist. Chaque evenement est valide
    (commit) separement : une interruption laisse les suivants en attente.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    at = store.now_iso(moment)
    store.ensure_schema(conn)
    published = publish_vault_events(conn, moment) if scan else None

    events = store.pending_events(conn, limit)
    outcomes = Counter()
    by_type = Counter()
    touched = []
    rejected_now = []
    failed = []
    # Les tables du Vault ne changent pas pendant la passe : une seule vue (caches).
    view = VaultView(conn, moment)
    for event in events:
        by_type[event["event_type"]] += 1
        try:
            drafts = detect(view, event)
            results = []
            for draft in drafts:
                outcome, candidate_id = upsert_candidate(
                    conn, draft, event["event_id"], at=at
                )
                outcomes[outcome] += 1
                results.append({"candidate_id": candidate_id, "outcome": outcome})
                if candidate_id not in touched:
                    touched.append(candidate_id)
                if outcome == "rejected" and candidate_id not in rejected_now:
                    rejected_now.append(candidate_id)
            if event["event_type"] == "daily_pass":
                for candidate_id in expire_stale_watching(conn, moment):
                    outcomes["expired_stale"] += 1
                    results.append({"candidate_id": candidate_id, "outcome": "expired"})
            store.mark_event_processed(
                conn, event["event_id"], {"candidates": results}, at=at
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - un evenement ne bloque pas la file
            conn.rollback()
            store.mark_event_failed(conn, event["event_id"], f"{type(exc).__name__}: {exc}", at=at)
            conn.commit()
            failed.append({"event_id": event["event_id"], "error": f"{type(exc).__name__}: {exc}"})

    shortlisted_now = apply_shortlist(conn, shortlist_limit, at=at)
    conn.commit()

    return {
        "at": at,
        "detector_version": DETECTOR_VERSION,
        "published": published,
        "events": {
            "read": len(events),
            "processed": len(events) - len(failed),
            "failed": failed,
            "by_type": dict(sorted(by_type.items())),
        },
        "candidates": dict(sorted(outcomes.items())),
        "shortlisted_now": shortlisted_now,
        "rejected_now": rejected_now,
        "touched": touched,
        "shortlist": store.list_candidates(conn, "shortlisted"),
        "watching": store.list_candidates(conn, "watching"),
        "rejected": [
            c for c in store.list_candidates(conn, "rejected") if c["candidate_id"] in rejected_now
        ],
        "totals": store.candidate_counts(conn),
        "review_stages": [stage.__name__ for stage in REVIEW_STAGES],
    }


# ------------------------------------------------------------------ bases

def open_database(path):
    from vault_db import connect as v2_connect

    return v2_connect(str(path))


def open_dry_run_copy(path):
    """Copie en memoire d'une base existante (lecture seule sur le fichier)."""
    path = Path(path).resolve()
    if not path.exists():
        raise SystemExit(f"Base Vault introuvable : {path}")
    source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    memory = sqlite3.connect(":memory:")
    try:
        source.backup(memory)
    finally:
        source.close()
    memory.row_factory = sqlite3.Row
    memory.execute("PRAGMA foreign_keys = ON")
    return memory


DEMO_PROJECT = "Projet Demo - Agence IA locale"


def build_demo_db(path=":memory:", now=None):
    """
    Scenario fictif : 4 sources recentes, 4 auteurs differents, toutes liees
    au meme projet ; plus 3 sources d'un seul auteur sur un autre sujet
    (convergence non independante -> rejet journalise).
    """
    import mentor_worker
    import vault_intelligence as intelligence

    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    conn = intelligence.connect(path)
    mentor_worker.ensure_schema(conn)
    project = intelligence.upsert_entity(
        conn, entity_type="project", canonical_key="demo:projet-agence-ia",
        name=DEMO_PROJECT,
    )
    other = intelligence.upsert_entity(
        conn, entity_type="method", canonical_key="demo:methode-growth",
        name="Methode growth (demo)",
    )
    demo_sources = [
        ("agent-vocal-pme", "Agent vocal pour PME", "alice.ia", 2, 0.72),
        ("automatiser-devis", "Automatiser les devis avec un LLM local", "bruno.dev", 4, 0.66),
        ("offre-ia-locale", "Vendre une offre IA locale aux artisans", "chloe.biz", 6, None),
        ("rgpd-ia-locale", "IA locale et donnees clients : l'argument confiance", "david.tech", 9, None),
    ]
    for slug, title, author, age, relevance in demo_sources:
        source = intelligence.upsert_source(
            conn, source_url=f"https://example.invalid/demo/{slug}", platform="demo",
            title=title, author=author,
            captured_at=(moment - timedelta(days=age)).date().isoformat(),
        )
        intelligence.link_entity_source(conn, entity_id=project, source_id=source)
        if relevance is not None:
            conn.execute(
                "INSERT INTO intelligence_mentor_insights_local (source_id, insight_type, "
                "observation, relevance_score, status, created_at, updated_at) "
                "VALUES (?, 'opportunity', ?, ?, 'proposed', ?, ?)",
                (source, f"(demo) {title}", relevance,
                 store.now_iso(moment), store.now_iso(moment)),
            )
    for index in range(3):
        source = intelligence.upsert_source(
            conn, source_url=f"https://example.invalid/demo/growth-{index}", platform="demo",
            title=f"Growth hack n{index + 1}", author="growth.guru",
            captured_at=(moment - timedelta(days=3 + index)).date().isoformat(),
        )
        intelligence.link_entity_source(conn, entity_id=other, source_id=source)
    conn.commit()
    return conn


# ------------------------------------------------------------------ rapport CLI

def _fmt_breakdown(candidate_type, breakdown):
    return " + ".join(
        f"{name} {breakdown[name]['value']:.2f}x{breakdown[name]['weight']:.2f}"
        for name in WEIGHTS[candidate_type]
        if name in breakdown
    )


def _fmt_candidate(candidate, index=None):
    head = f"{index:>2}. " if index is not None else "    "
    lines = [
        f"{head}[{candidate['score']:.3f}] {TYPE_LABELS[candidate['candidate_type']]:<16} "
        f"{candidate['candidate_id']}  {candidate['title']}"
    ]
    refs = []
    if candidate.get("project_id") is not None:
        refs.append(f"projet #{candidate['project_id']}")
    for key, label in (("source_ids", "sources"), ("insight_ids", "insights"),
                       ("task_ids", "taches"), ("claim_ids", "claims")):
        if candidate.get(key):
            refs.append(f"{label} {','.join(str(v) for v in candidate[key])}")
    if candidate.get("seen_count", 1) > 1:
        refs.append(f"vu {candidate['seen_count']} fois")
    if refs:
        lines.append("        " + " | ".join(refs))
    lines.append("        score = " + _fmt_breakdown(
        candidate["candidate_type"], candidate["score_breakdown"]
    ))
    if candidate.get("rejection_reason"):
        lines.append(f"        rejet : {candidate['rejection_reason']}")
    elif candidate["evidence"].get("watch_reason"):
        lines.append(f"        observation : {candidate['evidence']['watch_reason']}")
    return lines


def render_report(report, *, mode, db_label, max_watching=8):
    lines = [
        f"Mentor proactif - passe {mode} ({report['detector_version']})",
        f"Base : {db_label}",
        f"Horodatage : {report['at']}",
    ]
    if mode == "dry-run":
        lines.append("Dry-run : copie en memoire, vault.db n'est pas modifie.")
    if mode == "demo":
        lines.append("Demo : donnees FICTIVES dans une base en memoire.")
    lines.append("")
    if report["published"] is not None:
        published = ", ".join(f"{k}={v}" for k, v in report["published"].items())
        lines.append(f"Evenements publies depuis le Vault : {published or 'aucun'}")
    events = report["events"]
    by_type = ", ".join(f"{k} {v}" for k, v in events["by_type"].items())
    lines.append(
        f"Evenements traites : {events['processed']}/{events['read']}"
        + (f" ({by_type})" if by_type else "")
        + f", echecs : {len(events['failed'])}"
    )
    for failure in events["failed"]:
        lines.append(f"    ECHEC {failure['event_id']} : {failure['error']}")
    counts = report["candidates"]
    lines.append(
        "Candidats : "
        + (", ".join(f"{k} {v}" for k, v in counts.items()) if counts else "aucun")
    )
    lines.append("")
    lines.append(
        f"SHORTLIST (score >= {SHORTLIST_THRESHOLD}, ancre projet, "
        f"{len(report['shortlisted_now'])} nouveau(x) cette passe)"
    )
    if report["shortlist"]:
        for index, candidate in enumerate(report["shortlist"], start=1):
            lines.extend(_fmt_candidate(candidate, index))
    else:
        lines.append("    (vide)")
    lines.append("")
    watching = report["watching"]
    lines.append(f"EN OBSERVATION ({len(watching)}, non presentes)")
    for candidate in watching[:max_watching]:
        lines.extend(_fmt_candidate(candidate))
    if len(watching) > max_watching:
        lines.append(f"    ... {len(watching) - max_watching} autre(s)")
    lines.append("")
    lines.append(
        f"REJETES CETTE PASSE ({len(report['rejected'])}, journalises dans vault.db, "
        "jamais projetes)"
    )
    for candidate in report["rejected"]:
        lines.extend(_fmt_candidate(candidate))
    lines.append("")
    lines.append(
        "Phase suivante non branchee : shortlist -> Qwen -> Agency Agents -> "
        "suggestion SUG-xxxx -> decision humaine. Aucun appel LLM, cloud ou Notion."
    )
    return "\n".join(lines)


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Passe proactive du Mentor (phase 1)")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="chemin de vault.db")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true",
                       help="copie en memoire, aucune ecriture (defaut)")
    group.add_argument("--confirm", action="store_true", help="persiste la passe")
    group.add_argument("--demo", action="store_true", help="scenario fictif en memoire")
    group.add_argument("--status", action="store_true", help="etat de la file")
    parser.add_argument("--no-scan", action="store_true",
                        help="ne pas publier d'evenements, traiter la file existante")
    parser.add_argument("--limit", type=int, default=500, help="evenements max par passe")
    parser.add_argument("--shortlist", type=int, default=DEFAULT_SHORTLIST_LIMIT)
    parser.add_argument("--now", help="date de reference ISO (tests, rejeu)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    now = _parse_dt(args.now) if args.now else None

    if args.status:
        path = Path(args.db).resolve()
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        status = {
            "db": str(path),
            "schema_present": store.schema_present(conn),
            "real_vault_unlocked": store.enabled(),
            "events": store.event_counts(conn),
            "candidates": store.candidate_counts(conn),
        }
        conn.close()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0

    if args.demo:
        conn = build_demo_db(now=now)
        mode, label = "demo", "(memoire, donnees fictives)"
    elif args.confirm:
        # Refus AVANT toute ouverture en ecriture du vrai Vault.
        if store.v2_guard.is_real_vault(str(Path(args.db).resolve())) and not store.enabled():
            print(
                f"Refuse : le vrai Vault est verrouille pour la phase proactive "
                f"(fichier {store.FLAG.name} absent). Lance --dry-run.",
                file=sys.stderr,
            )
            return 2
        conn = open_database(args.db)
        mode, label = "confirm", str(Path(args.db).resolve())
    else:
        conn = open_dry_run_copy(args.db)
        mode, label = "dry-run", str(Path(args.db).resolve())

    try:
        report = run_pass(
            conn, now=now, limit=args.limit, shortlist_limit=args.shortlist,
            scan=not args.no_scan,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"mode": mode, "db": label, **report}, ensure_ascii=False,
                         indent=2, default=_json_default))
    else:
        print(render_report(report, mode=mode, db_label=label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
