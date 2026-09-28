"""
Notion (NON BRANCHE) : base de connaissances externe, interface visuelle,
suivi et gestion d'actions / projets (priorite 2 parmi les gestionnaires).

Le Vault pourra envoyer contenus, ressources, actions et bilans vers des bases
Notion. Notion reste un miroir : aucune donnee n'y existe sans etre dans vault.db.
"""

from .base import PlannedConnector


class NotionConnector(PlannedConnector):
    name = "notion"
    label = "Notion"
    description = "Base de connaissances et suivi d'actions : contenus, ressources, actions, bilans."
    groups = ("export", "tasks")
    display_order = 5
    task_priority = 2
    capabilities = ("send_content", "send_resource", "send_action", "send_review")
    required_env = ("NOTION_TOKEN", "NOTION_DATABASE_ID")
    todo = ("Intégration Notion (jeton) partagée avec les bases cibles",
            "Une base par type (contenus, ressources, actions, bilans) ou une base + propriété Type",
            "Créer ou mettre à jour la page (existing → page_id), retourner son URL")
