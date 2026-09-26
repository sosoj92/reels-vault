"""
connectors/base.py - Contrat commun a tous les connecteurs du Reels Vault.

Un connecteur fait sortir des donnees de vault.db vers un outil externe
(fichier Excel/CSV, Google Sheets, Notion, ClickUp, Davy...). Il ne devient
JAMAIS la source de verite : il recoit des donnees deja lues dans vault.db et
renvoie l'identifiant externe cree, que le Vault memorise lui-meme.

Ajouter un connecteur = deposer un fichier <nom>_connector.py dans ce dossier
avec une sous-classe de Connector. Le retirer = supprimer le fichier.
Aucune modification du coeur (vault_db.py, vault_api.py, interface) n'est necessaire :
l'interface affiche ce que decrit describe().
"""

import os
import sys
from pathlib import Path

EXPORT_DIR = Path(os.environ.get("REELS_VAULT_EXPORTS") or Path(__file__).resolve().parent.parent / "exports")

CAPABILITIES = (
    "export_vault",     # exporter tout le Vault (fichier)
    "export_contents",  # exporter une selection de contenus
    "send_content",     # envoyer un contenu (page Notion, ligne Sheets, analyse Davy...)
    "send_resource",    # envoyer une ressource
    "send_action",      # envoyer une action VALIDEE (tache ClickUp, carte Trello...)
    "send_review",      # envoyer / archiver un bilan
    "upload_file",      # deposer un fichier genere (export, rapport)
)


def env(nom):
    """
    Variable d'environnement du processus ; a defaut, variable Windows (utilisateur puis
    machine), pour les variables definies apres l'ouverture du terminal. Une variable
    presente dans le processus, meme vide, est prioritaire (les tests la vident).
    """
    if nom in os.environ:
        return os.environ[nom] or None
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    for racine, chemin in ((winreg.HKEY_CURRENT_USER, "Environment"),
                           (winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(racine, chemin) as cle:
                return winreg.QueryValueEx(cle, nom)[0] or None
        except OSError:
            continue
    return None


class ConnectorError(Exception):
    """Erreur renvoyee par un connecteur (refus du service, donnee invalide...)."""


class ExternalNotFound(ConnectorError):
    """L'objet externe enregistre (external_id) n'existe plus chez le fournisseur (supprime a la main)."""


class NotConfigured(ConnectorError):
    """Le connecteur existe mais n'est pas encore configure / branche."""


class Connector:
    # Identifiant court, utilise dans l'API et stocke dans external_provider.
    name = "base"
    label = "Connecteur"
    description = ""
    # Rubriques de l'onglet INTEGRATIONS : "export" (consultation / export), "tasks" (gestion des actions).
    groups = ()
    # Ordre de preference parmi les gestionnaires de taches (1 = prefere), None sinon.
    task_priority = None
    # Ordre d'affichage dans l'interface (rubrique consultation / export).
    display_order = 50
    capabilities = ()
    # Variables d'environnement requises (jetons d'API...). Jamais stockees dans vault.db.
    required_env = ()
    # Etapes restantes pour brancher reellement le service.
    todo = ()
    # False tant que les appels reels au service ne sont pas ecrits.
    implemented = True

    def missing_config(self):
        return [v for v in self.required_env if not env(v)]

    def is_configured(self):
        return self.implemented and not self.missing_config()

    def describe(self):
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "groups": list(self.groups),
            "task_priority": self.task_priority,
            "display_order": self.display_order,
            "capabilities": list(self.capabilities),
            "implemented": self.implemented,
            "configured": self.is_configured(),
            "local": not self.required_env,
            "missing_config": self.missing_config(),
            "todo": list(self.todo),
        }

    def _ensure_configured(self):
        if not self.implemented:
            raise NotConfigured(f"{self.label} : connecteur prevu mais pas encore branche")
        manquant = self.missing_config()
        if manquant:
            raise NotConfigured(f"{self.label} non configure : definir {', '.join(manquant)}")

    def _non_supporte(self, quoi):
        raise ConnectorError(f"{self.label} ne sait pas {quoi}")

    # -- Exports de fichiers : retournent {"files": [...]} ----------------------
    def export_vault(self, data, destination=None):
        """data = vault_db.dump(conn)."""
        self._non_supporte("exporter le Vault")

    def export_contents(self, contents, destination=None):
        self._non_supporte("exporter une selection de contenus")

    # -- Envois : retournent {"external_id": ..., "external_url": ...} ----------
    # existing = lien externe deja enregistre (mise a jour au lieu d'un doublon), ou None.
    def send_content(self, content, existing=None):
        self._non_supporte("recevoir de contenu")

    def send_resource(self, resource, existing=None):
        self._non_supporte("recevoir de ressource")

    def send_action(self, action, existing=None):
        self._non_supporte("recevoir d'action")

    def send_review(self, review, existing=None):
        self._non_supporte("recevoir de bilan")

    def upload_file(self, path):
        self._non_supporte("stocker de fichier")


class PlannedConnector(Connector):
    """Connecteur declare (visible dans l'interface) dont les appels reels restent a ecrire."""
    implemented = False

    def _prevu(self, *args, **kwargs):
        self._ensure_configured()
        raise NotConfigured(f"{self.label} : connecteur prevu mais pas encore branche")

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Chaque capacite declaree repond "non configure" tant qu'elle n'est pas implementee.
        for cap in cls.capabilities:
            if cap not in cls.__dict__:
                setattr(cls, cap, PlannedConnector._prevu)
