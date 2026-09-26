"""
Google Drive (NON BRANCHE) : stockage de fichiers generes par le Vault.

Usages prevus : sauvegarder les exports (Excel, CSV), stocker des rapports,
archiver les bilans (reviews), partager des documents. Drive ne contient que
des copies : il ne devient jamais la base centrale.
"""

from .base import PlannedConnector


class GoogleDriveConnector(PlannedConnector):
    name = "google_drive"
    label = "Google Drive"
    description = "Sauvegarde des exports, rapports et archives de bilans dans un dossier Drive."
    groups = ("export",)
    display_order = 4
    capabilities = ("export_vault", "export_contents", "upload_file", "send_review")
    required_env = ("GOOGLE_CREDENTIALS_FILE", "GOOGLE_DRIVE_FOLDER_ID")
    todo = ("Authentification OAuth Google",
            "upload_file : téléverser un fichier du dossier exports/ dans GOOGLE_DRIVE_FOLDER_ID",
            "export_vault : générer l'export Excel puis le téléverser",
            "send_review : archiver chaque bilan (document texte / Markdown)")
