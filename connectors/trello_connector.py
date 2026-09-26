"""
Trello (NON BRANCHE) : option Kanban externe (priorite 3). Une action VALIDEE
pourra devenir une carte Trello ; le Vault conserve external_provider,
external_task_id et external_url.
"""

from .base import PlannedConnector


class TrelloConnector(PlannedConnector):
    name = "trello"
    label = "Trello"
    description = "Kanban externe : une action validée devient une carte."
    groups = ("tasks",)
    task_priority = 3
    capabilities = ("send_action",)
    required_env = ("TRELLO_KEY", "TRELLO_TOKEN", "TRELLO_LIST_ID")
    todo = ("Clé + jeton Trello, liste cible (ou une liste par statut)",
            "send_action : créer / mettre à jour la carte, la déplacer selon le statut")
