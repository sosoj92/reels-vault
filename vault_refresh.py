#!/usr/bin/env python3
"""
vault_refresh.py - Orchestrateur post-ingestion du Life Intelligence System.

Objectif : transformer les etapes manuelles apres ingest.py en une seule chaine
rapide, idempotente et sans LLM bloquant.

Pipeline :
1. importe raw/index dans vault.db (V2, idempotent) ;
2. promeut contenus + ressources explicites vers Intelligence V3 ;
3. applique uniquement les resolutions d'identite locales marquees safe ;
4. synchronise le working set vers le cockpit Notion si configure.

L'analyse semantique FAST/CAREFUL n'est volontairement PAS lancee ici :
elle peut etre couteuse/lente et ne doit jamais bloquer l'ingestion.

Usage :
    python vault_refresh.py
    python vault_refresh.py --vault . --db .\vault.db
    python vault_refresh.py --skip-notion
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import vault_intelligence as intelligence
import vault_intelligence_actions as intelligence_actions
import vault_intelligence_bridge as bridge
import vault_resolution as resolution
from vault_notion_cockpit import NotionCockpitClient, sync_projection


ROOT = Path(__file__).resolve().parent


class RefreshError(RuntimeError):
    pass


def _run_migration(vault_path, db_path):
    command = [
        sys.executable,
        str(ROOT / "migrate_to_db.py"),
        "--vault",
        str(vault_path),
        "--db",
        str(db_path),
    ]
    result = subprocess.run(command, text=True)
    if result.returncode != 0:
        raise RefreshError(
            f"migrate_to_db.py a echoue avec le code {result.returncode}"
        )


def refresh(vault_path, db_path, *, sync_notion=True):
    vault_path = Path(vault_path).resolve()
    db_path = Path(db_path).resolve()

    _run_migration(vault_path, db_path)

    conn = intelligence.connect(db_path)
    try:
        promoted = bridge.promote_all_contents(conn)

        discovered = resolution.discover_local_candidates(conn)
        resolved = resolution.apply_safe_candidates(conn)

        proposals = intelligence_actions.propose_priority_actions(
            conn,
            max_priority=2,
        )

        cockpit = {
            "status": "skipped",
            "reason": "disabled",
        }
        if sync_notion:
            client = NotionCockpitClient()
            if client.configured():
                cockpit = {
                    "status": "synced",
                    **sync_projection(conn, client),
                }
            else:
                cockpit = {
                    "status": "skipped",
                    "reason": "NOTION_TOKEN missing",
                }

        return {
            "db": str(db_path),
            "promotion": {
                "contents_promoted": promoted["contents_promoted"],
                "resources_promoted": promoted["resources_promoted"],
            },
            "resolution": {
                "proposed_entities": discovered["proposed_entities"],
                "ambiguous_entities": discovered["ambiguous_entities"],
                "unresolved_without_local_candidate": discovered[
                    "unresolved_without_local_candidate"
                ],
                "applied": resolved["applied"],
                "skipped": resolved["skipped"],
            },
            "action_proposals": {
                "created": proposals["created"],
                "existing": proposals["existing"],
                "unsupported": proposals["unsupported"],
                "skipped_priority": proposals["skipped_priority"],
            },
            "cockpit": cockpit,
            "semantic_analysis_started": False,
        }
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Rafraichit vault.db, Intelligence V3 et le cockpit Notion"
    )
    parser.add_argument(
        "--vault",
        default=str(ROOT),
        help="dossier du Vault contenant raw/, index.md, taxonomy.md",
    )
    parser.add_argument(
        "--db",
        default=str(ROOT / "vault.db"),
        help="chemin de vault.db",
    )
    parser.add_argument(
        "--skip-notion",
        action="store_true",
        help="ne pas synchroniser le cockpit Notion",
    )
    args = parser.parse_args()

    result = refresh(
        args.vault,
        args.db,
        sync_notion=not args.skip_notion,
    )
    print("\nLife Intelligence refresh:")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except RefreshError as exc:
        sys.exit(f"Erreur : {exc}")
