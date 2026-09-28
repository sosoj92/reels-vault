"""
Todoist (NON BRANCHE, FACULTATIF) : gestionnaire de taches optionnel, jamais
utilise par defaut. Son absence n'affecte aucune fonctionnalite du Vault.
"""

from .base import PlannedConnector


class TodoistConnector(PlannedConnector):
    name = "todoist"
    label = "Todoist"
    description = "Connecteur facultatif pour des tâches personnelles."
    groups = ("tasks",)
    task_priority = 5
    capabilities = ("send_action",)
    required_env = ("TODOIST_TOKEN",)
    todo = ("Jeton API Todoist",
            "send_action : créer / mettre à jour la tâche (API REST)")
