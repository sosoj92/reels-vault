"""Export CSV (bibliotheque standard uniquement). Encodage UTF-8 avec BOM et ';' pour Excel FR."""

import csv
from datetime import datetime
from pathlib import Path

from .base import EXPORT_DIR, Connector

CHAMPS_CONTENUS = ["id", "title", "platform", "author", "category", "subthemes", "tags",
                   "description", "description_source", "source_url", "source_status", "created_at"]


def _ligne(c):
    return {k: (", ".join(c[k]) if isinstance(c.get(k), list) else c.get(k)) for k in CHAMPS_CONTENUS}


def _ecrire(chemin, champs, lignes):
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with open(chemin, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=champs, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(lignes)
    return str(chemin)


class CsvConnector(Connector):
    name = "csv"
    label = "CSV"
    description = "Fichiers CSV (UTF-8, separateur ;) lisibles par Excel, LibreOffice, Google Sheets."
    groups = ("export",)
    display_order = 2
    capabilities = ("export_vault", "export_contents")

    def export_vault(self, data, destination=None):
        dossier = Path(destination or EXPORT_DIR)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        fichiers = [
            _ecrire(dossier / f"reels-vault-contenus-{stamp}.csv", CHAMPS_CONTENUS,
                    [_ligne(c) for c in data["contents"]]),
            _ecrire(dossier / f"reels-vault-ressources-{stamp}.csv",
                    ["id", "name", "type", "description", "url", "content_id", "content_title"],
                    data["resources"]),
            _ecrire(dossier / f"reels-vault-actions-{stamp}.csv",
                    ["id", "title", "action_type", "status", "priority", "due_date",
                     "external_provider", "external_task_id", "content_id", "content_title"],
                    data["actions"]),
        ]
        return {"files": fichiers}

    def export_contents(self, contents, destination=None):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        chemin = Path(destination or EXPORT_DIR) / f"reels-vault-selection-{stamp}.csv"
        return {"files": [_ecrire(chemin, CHAMPS_CONTENUS, [_ligne(c) for c in contents])]}
