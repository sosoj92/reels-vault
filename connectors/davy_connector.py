"""
Davy (NON BRANCHE) : envoi d'une selection de contenus a Davy pour analyse.

Davy est un CONSOMMATEUR du Vault : pour lire, il interroge l'API locale
(voir vault_client.py). Ce connecteur ne sert qu'au sens inverse,
« Envoyer vers Davy » depuis l'interface. Davy repond en ecrivant via l'API
(description_ai, actions au statut 'proposed') ; il n'accede jamais
directement a vault.db.
"""

from .base import PlannedConnector


class DavyConnector(PlannedConnector):
    name = "davy"
    label = "Davy"
    description = "IA personnelle locale : analyse de contenus, propositions d'actions via l'API."
    groups = ("export",)
    display_order = 6
    capabilities = ("send_content", "export_contents")
    required_env = ("DAVY_URL",)
    todo = ("Davy appelle l'API locale via vault_client.py (lecture, description_ai, actions proposées)",
            "send_content / export_contents : envoyer les contenus vers DAVY_URL")
