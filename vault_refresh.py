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
from vault_notion_cockpit import (
    NotionCockpitClient,
    apply_notion_decisions,
    sync_projection,
)


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


def refresh(vault_path, db_path, *, sync_notion=True, index_semantic=True):
    vault_path = Path(vault_path).resolve()
    db_path = Path(db_path).resolve()

    _run_migration(vault_path, db_path)

    conn = intelligence.connect(db_path)
    try:
        promoted = bridge.promote_all_contents(conn)

        discovered = resolution.discover_local_candidates(conn)
        resolved = resolution.apply_safe_candidates(conn)

        notion_decisions = {
            "status": "skipped",
            "reason": "disabled",
        }
        client = None
        if sync_notion:
            client = NotionCockpitClient()
            if client.configured():
                notion_decisions = {
                    "status": "checked",
                    **apply_notion_decisions(conn, client),
                }
            else:
                notion_decisions = {
                    "status": "skipped",
                    "reason": "NOTION_TOKEN missing",
                }

        proposals = intelligence_actions.propose_priority_actions(
            conn,
            max_priority=2,
        )

        cockpit = {
            "status": "skipped",
            "reason": "disabled",
        }
        if sync_notion:
            if client and client.configured():
                cockpit = {
                    "status": "synced",
                    **sync_projection(conn, client),
                }
            else:
                cockpit = {
                    "status": "skipped",
                    "reason": "NOTION_TOKEN missing",
                }

        # V2 : index semantique incremental (modele local, quelques
        # centaines de ms par nouvelle source). Jamais bloquant : une erreur
        # laisse simplement la recherche en mode lexical.
        semantic_index = {"status": "skipped", "reason": "disabled"}
        import v2_guard

        if index_semantic and not v2_guard.v2_writes_allowed(conn):
            # Verrou V2 : pas d'ecriture V2 sur le vrai Vault avant validation.
            semantic_index = {"status": "skipped", "reason": "v2_not_validated"}
        elif index_semantic:
            try:
                import vault_embeddings

                backend = vault_embeddings.default_backend()
                if backend is None:
                    semantic_index = {"status": "skipped", "reason": "model_unavailable"}
                else:
                    semantic_index = {
                        "status": "indexed",
                        **vault_embeddings.index_sources(conn, backend),
                    }
            except Exception as exc:  # noqa: BLE001 - etape optionnelle
                semantic_index = {"status": "error", "reason": type(exc).__name__}

        return {
            "db": str(db_path),
            "semantic_index": semantic_index,
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
            "notion_decisions": notion_decisions,
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
    parser.add_argument(
        "--skip-semantic-index",
        action="store_true",
        help="ne pas mettre a jour l'index semantique local",
    )
    args = parser.parse_args()

    result = refresh(
        args.vault,
        args.db,
        sync_notion=not args.skip_notion,
        index_semantic=not args.skip_semantic_index,
    )
    print("\nLife Intelligence refresh:")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except RefreshError as exc:
        sys.exit(f"Erreur : {exc}")
