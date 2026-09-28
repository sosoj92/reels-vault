"""
Google Sheets (NON BRANCHE) : interface tabulaire distante, miroir de vault.db.

Onglets synchronises : CONTENUS, RESSOURCES, ACTIONS, REVIEWS. Chaque ligne
porte les identifiants Vault (vault_id, content_id, action_id, review_id) :
une synchronisation met a jour la ligne portant le meme identifiant au lieu
d'en ajouter une (pas de doublon). La mise en lignes est deja ecrite (rows) ;
restent l'authentification et l'ecriture dans la feuille.
"""

from .base import PlannedConnector

ONGLETS = {
    "CONTENUS": ["vault_id", "content_id", "titre", "plateforme", "auteur", "catégorie", "sous-thèmes",
                 "tags", "description", "source", "statut source", "mis à jour"],
    "RESSOURCES": ["vault_id", "resource_id", "content_id", "ressource", "type", "description", "url"],
    "ACTIONS": ["vault_id", "action_id", "content_id", "action", "type", "statut", "priorité",
                "échéance", "provider", "id externe"],
    "REVIEWS": ["vault_id", "review_id", "début", "fin", "résumé"],
}


def _j(v):
    return ", ".join(v) if isinstance(v, list) else v


def rows(data):
    """data = vault_db.dump(conn) -> {onglet: [lignes]} ; vault_id = cle unique et stable de la ligne."""
    return {
        "CONTENUS": [[f"content:{c['id']}", c["id"], c["title"], c["platform"], c["author"], c["category"],
                      _j(c["subthemes"]), _j(c["tags"]), c["description"], c["source_url"], c["source_status"],
                      c["updated_at"]] for c in data["contents"]],
        "RESSOURCES": [[f"resource:{r['id']}", r["id"], r["content_id"], r["name"], r["type"], r["description"],
                        r["url"]] for r in data["resources"]],
        "ACTIONS": [[f"action:{a['id']}", a["id"], a["content_id"], a["title"], a["action_type"], a["status"],
                     a["priority"], a["due_date"], a["external_provider"], a["external_task_id"]]
                    for a in data["actions"]],
        "REVIEWS": [[f"review:{r['id']}", r["id"], r["started_at"], r["ended_at"], r["summary"]]
                    for r in data["reviews"]],
    }


class GoogleSheetsConnector(PlannedConnector):
    name = "google_sheets"
    label = "Google Sheets"
    description = "Tableur distant synchronisé (CONTENUS, RESSOURCES, ACTIONS, REVIEWS), lignes identifiées par vault_id."
    groups = ("export",)
    display_order = 3
    capabilities = ("export_vault", "export_contents", "send_content", "send_action", "send_review")
    required_env = ("GOOGLE_CREDENTIALS_FILE", "GOOGLE_SHEETS_ID")
    todo = ("Authentification OAuth Google (compte de service ou OAuth utilisateur)",
            "Créer les onglets et en-têtes (ONGLETS)",
            "Synchroniser : mettre à jour la ligne de même vault_id, sinon l'ajouter")
