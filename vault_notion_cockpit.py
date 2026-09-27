#!/usr/bin/env python3
"""
vault_notion_cockpit.py - Projection selective du Life Intelligence System vers Notion.

Notion est un miroir operationnel, jamais la source de verite.

Le cockpit synchronise uniquement :
- les entites attention_status='now' ;
- les signaux proactifs de priorite 1 ou 2 ;
- les actions issues de ces signaux (meme Vault ref, donc pas de doublon).

Aucun LLM n'est utilise. Aucun claim n'est requalifie. Les ecritures Notion
sont explicites et necessitent --confirm.

Configuration locale Windows (variables d'environnement, jamais dans vault.db) :
    NOTION_TOKEN
    NOTION_COCKPIT_DATA_SOURCE_ID

Data source actuelle du cockpit :
    3dd87f7b-6c92-4b8f-8609-b1c5a85095d4

Commandes :
    python vault_notion_cockpit.py --db vault.db dry-run
    python vault_notion_cockpit.py --db vault.db sync --confirm
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import vault_db
import vault_intelligence as intelligence
import vault_intelligence_actions as intelligence_actions
import vault_intelligence_query as intelligence_query
import vault_intelligence_signals as intelligence_signals
from connectors.base import env as read_env

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
DEFAULT_DATA_SOURCE_ID = "3dd87f7b-6c92-4b8f-8609-b1c5a85095d4"

SYNC_SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_cockpit_links (
    vault_ref       TEXT PRIMARY KEY,
    notion_page_id  TEXT NOT NULL,
    notion_url      TEXT,
    payload_hash    TEXT NOT NULL,
    last_synced_at  TEXT NOT NULL
);
"""


class CockpitError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(conn):
    intelligence.ensure_schema(conn)
    conn.executescript(SYNC_SCHEMA)
    conn.commit()
    return conn


def _verification_label(epistemic):
    counts = (epistemic or {}).get("verification_counts") or {}
    present = [
        status
        for status in ("contested", "verified", "asserted", "unverified")
        if counts.get(status, 0)
    ]
    if not present:
        return "n/a"
    if len(present) == 1:
        return present[0]
    return "mixed"


def _attention_label(status):
    return {
        "now": "🔥 Maintenant",
        "later": "🕒 Plus tard",
        "in_place": "✅ En place",
        "dismissed": "⛔ Écarté",
    }[status]


def _signal_title(signal):
    subject = (
        signal.get("entity_name")
        or (f"Source #{signal['source_id']}" if signal.get("source_id") else None)
        or signal["kind"]
    )
    labels = {
        "contested_claim": "Claim contesté",
        "sensitive_claim_review": "Claim sensible à vérifier",
        "identity_review": "Identité à confirmer",
        "source_analysis_needed": "Source sensible à analyser",
        "now_without_action": "Aucune prochaine action",
    }
    return f"⚠️ {labels.get(signal['kind'], signal['kind'])} — {subject}"


def build_projection(conn):
    """
    Retourne le working set Notion, sans ecriture locale ni distante.
    """
    ensure_schema(conn)

    rows = []

    entities = intelligence_query.list_entities(
        conn,
        attention_status="now",
        limit=1000,
    )["items"]

    for entity in entities:
        full = intelligence_query.get_entity(conn, entity["id"])
        rows.append(
            {
                "vault_ref": f"entity:{entity['id']}",
                "item": entity["name"],
                "kind": "Entity",
                "attention": _attention_label(entity["attention_status"]),
                "priority": 2,
                "why": entity.get("summary") or "Marqué Maintenant dans le Vault.",
                "next_step": "Décider ou poursuivre la prochaine action utile.",
                "verification": _verification_label(full.get("epistemic")),
                "source": entity.get("url"),
            }
        )

    mapped_actions = intelligence_actions.list_signal_actions(conn)["items"]
    mapped_by_signal = {item["signal_key"]: item for item in mapped_actions}

    signal_queue = intelligence_signals.collect_signals(conn, limit=500)
    signals_by_key = {item["key"]: item for item in signal_queue["items"]}

    for signal in signal_queue["items"]:
        if signal["priority"] > 2 or signal["key"] in mapped_by_signal:
            continue
        rows.append(
            {
                "vault_ref": f"signal:{signal['key']}",
                "item": _signal_title(signal),
                "kind": "Signal",
                "attention": "🔥 Maintenant",
                "priority": signal["priority"],
                "why": signal["reason"],
                "next_step": signal["suggested_action"],
                "verification": signal.get("verification_status") or "n/a",
                "source": signal.get("source_url"),
            }
        )

    for action in mapped_actions:
        signal = signals_by_key.get(action["signal_key"])
        status = action["status"]
        attention = {
            "done": "✅ En place",
            "dismissed": "⛔ Écarté",
            "archived": "⛔ Écarté",
        }.get(status, "🔥 Maintenant")
        next_step = {
            "proposed": "Valider ou ignorer cette proposition.",
            "approved": "Action validée : préparer son exécution.",
            "todo": "Action validée : à exécuter.",
            "doing": "Action en cours.",
            "done": "Action terminée.",
            "dismissed": "Proposition ignorée.",
            "archived": "Action archivée.",
        }.get(status, "Examiner cette action.")

        rows.append(
            {
                # Meme ref que le signal d'origine : Notion met a jour la ligne.
                "vault_ref": f"signal:{action['signal_key']}",
                "item": f"🧭 {action['title']}",
                "kind": "Action",
                "attention": attention,
                "priority": action.get("priority") or 3,
                "why": action.get("description") or "Proposition du Life Intelligence System.",
                "next_step": next_step,
                "verification": (
                    signal.get("verification_status")
                    if signal and signal.get("verification_status")
                    else "n/a"
                ),
                "source": (
                    signal.get("source_url")
                    if signal
                    else action.get("content_source_url")
                ),
            }
        )

    rows.sort(
        key=lambda row: (
            row["priority"],
            {"Action": 0, "Signal": 1, "Entity": 2}.get(row["kind"], 3),
            row["item"].lower(),
        )
    )
    return rows


def _payload(row):
    properties = {
        "Item": {
            "title": [
                {"type": "text", "text": {"content": row["item"][:2000]}}
            ]
        },
        "Kind": {"select": {"name": row["kind"]}},
        "Attention": {"select": {"name": row["attention"]}},
        "Priority": {"number": row["priority"]},
        "Why": {
            "rich_text": [
                {"type": "text", "text": {"content": row["why"][:2000]}}
            ]
        },
        "Next step": {
            "rich_text": [
                {"type": "text", "text": {"content": row["next_step"][:2000]}}
            ]
        },
        "Verification": {"select": {"name": row["verification"]}},
        "Vault ref": {
            "rich_text": [
                {"type": "text", "text": {"content": row["vault_ref"]}}
            ]
        },
        "Vault managed": {"checkbox": True},
        "Synced at": {"date": {"start": now()}},
    }
    if row.get("source"):
        properties["Source"] = {"url": row["source"]}
    else:
        properties["Source"] = {"url": None}
    return properties


def _payload_hash(properties):
    stable = dict(properties)
    stable.pop("Synced at", None)
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class NotionCockpitClient:
    def __init__(self, token=None, data_source_id=None, timeout=20):
        self.token = token or read_env("NOTION_TOKEN")
        self.data_source_id = (
            data_source_id
            or read_env("NOTION_COCKPIT_DATA_SOURCE_ID")
            or DEFAULT_DATA_SOURCE_ID
        )
        self.timeout = timeout

    def configured(self):
        return bool(self.token and self.data_source_id)

    def _request(self, method, path, body=None):
        if not self.configured():
            raise CockpitError(
                "Notion cockpit non configure : NOTION_TOKEN est requis."
            )
        request = Request(
            NOTION_API + path,
            method=method,
            data=(
                json.dumps(body).encode("utf-8")
                if body is not None
                else None
            ),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            err = CockpitError(
                f"Notion a refusé la requête ({exc.code}) : {detail}"
            )
            err.status = exc.code
            raise err from None
        except URLError as exc:
            raise CockpitError(
                f"Notion injoignable : {exc.reason}"
            ) from None

    def create_page(self, properties):
        return self._request(
            "POST",
            "/pages",
            {
                "parent": {
                    "type": "data_source_id",
                    "data_source_id": self.data_source_id,
                },
                "properties": properties,
            },
        )

    def update_page(self, page_id, properties):
        return self._request(
            "PATCH",
            f"/pages/{page_id}",
            {"properties": properties},
        )


def _existing_link(conn, vault_ref):
    row = conn.execute(
        """
        SELECT *
          FROM intelligence_cockpit_links
         WHERE vault_ref = ?
        """,
        (vault_ref,),
    ).fetchone()
    return dict(row) if row else None


def _record_link(conn, vault_ref, page, payload_hash):
    conn.execute(
        """
        INSERT INTO intelligence_cockpit_links(
            vault_ref, notion_page_id, notion_url, payload_hash, last_synced_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(vault_ref) DO UPDATE SET
            notion_page_id = excluded.notion_page_id,
            notion_url = excluded.notion_url,
            payload_hash = excluded.payload_hash,
            last_synced_at = excluded.last_synced_at
        """,
        (
            vault_ref,
            page["id"],
            page.get("url"),
            payload_hash,
            now(),
        ),
    )
    conn.commit()


def sync_projection(conn, client):
    """
    Upsert du working set. Pas de suppression/archivage automatique en V1 :
    retirer un item du cockpit est une mutation externe importante et sera
    ajoute plus tard avec une politique explicite.
    """
    rows = build_projection(conn)
    result = {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "items": len(rows),
    }

    for row in rows:
        properties = _payload(row)
        fingerprint = _payload_hash(properties)
        existing = _existing_link(conn, row["vault_ref"])

        if existing and existing["payload_hash"] == fingerprint:
            result["unchanged"] += 1
            continue

        if existing:
            try:
                page = client.update_page(
                    existing["notion_page_id"],
                    properties,
                )
            except CockpitError as exc:
                if getattr(exc, "status", None) != 404:
                    raise
                page = client.create_page(properties)
                result["created"] += 1
            else:
                result["updated"] += 1
        else:
            page = client.create_page(properties)
            result["created"] += 1

        _record_link(
            conn,
            row["vault_ref"],
            page,
            fingerprint,
        )

    return result


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("dry-run", "sync"))
    parser.add_argument("--db", default=None)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    import vault_intelligence

    conn = vault_intelligence.connect(args.db)
    rows = build_projection(conn)

    if args.mode == "dry-run":
        print(
            json.dumps(
                {
                    "items": rows,
                    "writes_performed": False,
                    "notion_data_source_id": (
                        read_env("NOTION_COCKPIT_DATA_SOURCE_ID")
                        or DEFAULT_DATA_SOURCE_ID
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if not args.confirm:
        raise SystemExit(
            "Synchronisation refusée : ajouter --confirm pour écrire dans Notion."
        )

    client = NotionCockpitClient()
    result = sync_projection(conn, client)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except CockpitError as exc:
        sys.exit(f"Erreur : {exc}")
