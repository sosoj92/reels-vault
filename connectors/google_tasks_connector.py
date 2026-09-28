"""
Google Tasks (NON BRANCHE) : gestionnaire de taches SIMPLE (rappels, petites
actions rapides). Ce n'est pas la couche structurante principale : pas de
projets, statuts limites (a faire / termine).
"""

from .base import PlannedConnector


class GoogleTasksConnector(PlannedConnector):
    name = "google_tasks"
    label = "Google Tasks"
    description = "Tâches simples et rappels ; statuts limités à « à faire / terminé »."
    groups = ("tasks",)
    task_priority = 4
    capabilities = ("send_action",)
    required_env = ("GOOGLE_CREDENTIALS_FILE", "GOOGLE_TASKS_LIST_ID")
    todo = ("Authentification OAuth Google",
            "send_action : créer / mettre à jour une tâche (titre, notes + lien source, échéance)")
