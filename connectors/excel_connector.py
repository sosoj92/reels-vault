"""
Export Excel (.xlsx) avec les onglets CONTENUS, RESSOURCES, ACTIONS, TAGS, REVIEWS.

Necessite le paquet optionnel openpyxl (pip install openpyxl). Sans lui, le
connecteur apparait comme non configure : le reste du Vault fonctionne.
"""

from datetime import datetime
from pathlib import Path

from .base import EXPORT_DIR, Connector, NotConfigured

try:
    import openpyxl
    from openpyxl.styles import Font
except ImportError:  # dependance optionnelle
    openpyxl = None


def _feuille(wb, titre, entetes, lignes):
    ws = wb.create_sheet(titre)
    ws.append(entetes)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for l in lignes:
        ws.append([", ".join(v) if isinstance(v, list) else v for v in l])
    ws.freeze_panes = "A2"
    for col in ws.columns:
        largeur = max(len(str(c.value or "")) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(12, largeur + 2), 60)


class ExcelConnector(Connector):
    name = "excel"
    label = "Excel"
    description = "Classeur .xlsx : CONTENUS, RESSOURCES, ACTIONS, TAGS, REVIEWS."
    groups = ("export",)
    display_order = 1
    capabilities = ("export_vault", "export_contents")

    def missing_config(self):
        return [] if openpyxl else ["paquet python openpyxl"]

    def _classeur(self, contents, data=None):
        if not openpyxl:
            raise NotConfigured("Export Excel indisponible : installer openpyxl (pip install openpyxl)")
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        _feuille(wb, "CONTENUS",
                 ["id", "titre", "plateforme", "auteur", "catégorie", "sous-thèmes", "tags",
                  "description", "source", "statut source", "date"],
                 [[c["id"], c["title"], c["platform"], c["author"], c["category"], c["subthemes"],
                   c["tags"], c["description"], c["source_url"], c["source_status"],
                   c.get("ingested_at") or c["created_at"]] for c in contents])
        if data is None:
            return wb
        _feuille(wb, "RESSOURCES", ["ressource", "type", "utilité", "url", "contenu source"],
                 [[r["name"], r["type"], r["description"], r["url"], r["content_title"]]
                  for r in data["resources"]])
        _feuille(wb, "ACTIONS",
                 ["action", "type", "statut", "priorité", "échéance", "envoyée vers", "id externe",
                  "contenu source"],
                 [[a["title"], a["action_type"], a["status"], a["priority"], a["due_date"],
                   a["external_provider"], a["external_task_id"], a["content_title"]]
                  for a in data["actions"]])
        tax = data["taxonomy"]
        lignes = []
        for cat in tax["categories"]:
            lignes.append(["catégorie", cat["name"], "", cat["count"]])
            lignes += [["sous-thème", s["name"], cat["name"], s["count"]] for s in cat["subthemes"]]
        lignes += [["tag", t["name"], "", t["count"]] for t in tax["tags"]]
        _feuille(wb, "TAGS", ["niveau", "nom", "catégorie parente", "nb contenus"], lignes)
        _feuille(wb, "REVIEWS", ["début", "fin", "résumé"],
                 [[r["started_at"], r["ended_at"], r["summary"]] for r in data["reviews"]])
        return wb

    def export_vault(self, data, destination=None):
        chemin = Path(destination or EXPORT_DIR / "reels-vault-export.xlsx")
        chemin.parent.mkdir(parents=True, exist_ok=True)
        self._classeur(data["contents"], data).save(chemin)
        return {"files": [str(chemin)]}

    def export_contents(self, contents, destination=None):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        chemin = Path(destination or EXPORT_DIR / f"reels-vault-selection-{stamp}.xlsx")
        chemin.parent.mkdir(parents=True, exist_ok=True)
        self._classeur(contents).save(chemin)
        return {"files": [str(chemin)]}
