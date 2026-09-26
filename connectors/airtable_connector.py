"""Airtable (NON BRANCHE, conserve de la phase 1) : miroir optionnel des contenus et des actions."""

from .base import PlannedConnector


class AirtableConnector(PlannedConnector):
    name = "airtable"
    label = "Airtable"
    description = "Miroir tabulaire optionnel (conservé de la phase 1)."
    groups = ("export",)
    display_order = 7
    capabilities = ("send_content", "send_action")
    required_env = ("AIRTABLE_TOKEN", "AIRTABLE_BASE_ID")
    todo = ("Jeton + base Airtable", "Créer / mettre à jour les enregistrements")
