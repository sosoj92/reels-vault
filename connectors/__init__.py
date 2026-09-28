"""
connectors - Registre et orchestration des connecteurs du Reels Vault.

Les connecteurs sont decouverts automatiquement : tout fichier
connectors/<nom>_connector.py contenant une sous-classe de Connector est
disponible. C'est ici (et non dans chaque connecteur ni dans l'interface)
que sont appliquees les regles communes :
- vault.db reste la source de verite : les donnees sont lues dans vault.db ;
- une action n'est envoyee qu'apres validation par l'utilisateur, jamais automatiquement ;
- l'identifiant externe est memorise dans vault.db (pas de doublon, mise a jour possible) ;
- chaque export / envoi est journalise (statut de synchronisation).

Gestionnaire de taches par defaut : variable d'environnement TASK_PROVIDER
(clickup, notion, trello, google_tasks, todoist...) ; a defaut, le reglage
choisi dans l'interface (stocke dans vault.db). En changer ne demande aucune
modification du code.

Tache externe supprimee a la main : si le fournisseur signale que l'objet
enregistre n'existe plus (ExternalNotFound), la correspondance est retiree,
l'evenement est journalise et l'erreur "external_missing" est renvoyee une seule
fois : le prochain envoi recree proprement l'objet et remplace l'ancien identifiant.
"""

import importlib
import inspect
import pkgutil

import vault_db
from .base import CAPABILITIES, Connector, ConnectorError, ExternalNotFound, NotConfigured, PlannedConnector, env

_registry = None


def registry():
    global _registry
    if _registry is None:
        _registry = {}
        for mod in pkgutil.iter_modules(__path__):
            if not mod.name.endswith("_connector"):
                continue
            module = importlib.import_module(f"{__name__}.{mod.name}")
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if (issubclass(cls, Connector) and cls not in (Connector, PlannedConnector)
                        and cls.__module__ == module.__name__):
                    _registry[cls.name] = cls()
    return _registry


def get(name, capability=None):
    try:
        c = registry()[name]
    except KeyError:
        raise ConnectorError(f"connecteur inconnu : {name} (disponibles : {', '.join(registry())})")
    if capability and capability not in c.capabilities:
        raise ConnectorError(f"{c.label} ne propose pas '{capability}'")
    return c


def _gestionnaire(nom):
    nom = (nom or "").strip().lower()
    c = registry().get(nom)
    return nom if c and "send_action" in c.capabilities else None


def task_provider_info(conn=None):
    """{"name", "source"} : TASK_PROVIDER (variable) prioritaire, sinon reglage de l'interface."""
    nom = _gestionnaire(env("TASK_PROVIDER"))
    if nom:
        return {"name": nom, "source": "env"}
    if conn is not None:
        nom = _gestionnaire(vault_db.get_setting(conn, "task_provider"))
        if nom:
            return {"name": nom, "source": "setting"}
    return {"name": None, "source": None}


def task_provider(conn=None):
    """Gestionnaire de taches par defaut, s'il existe et sait recevoir des actions."""
    return task_provider_info(conn)["name"]


def set_task_provider(conn, name):
    """Choix du gestionnaire depuis l'interface (sans effet si TASK_PROVIDER est defini)."""
    if name:
        c = get(name, "send_action")
        if "tasks" not in c.groups:
            raise ConnectorError(f"{c.label} n'est pas un gestionnaire de taches")
    vault_db.set_setting(conn, "task_provider", name or "")
    return task_provider_info(conn)


def list_connectors(conn=None):
    statuts = vault_db.sync_status(conn) if conn is not None else {}
    defaut = task_provider(conn)
    items = []
    for c in registry().values():
        d = c.describe()
        d["sync"] = statuts.get(c.name)
        d["is_task_provider"] = c.name == defaut
        items.append(d)
    items.sort(key=lambda d: (d["display_order"], d["task_priority"] or 99, d["label"].lower()))
    return items


def _journalise(conn, connecteur, operation, entity_type, entity_id, fn):
    try:
        res = fn()
    except NotConfigured as e:
        vault_db.log_sync(conn, connecteur.name, operation, "not_configured", entity_type, entity_id, str(e))
        raise
    except Exception as e:
        vault_db.log_sync(conn, connecteur.name, operation, "error", entity_type, entity_id, str(e)[:500])
        raise
    detail = ", ".join(res.get("files", [])) or res.get("external_id")
    vault_db.log_sync(conn, connecteur.name, operation, "ok", entity_type, entity_id, detail)
    return res


def export_vault(conn, name, destination=None):
    c = get(name, "export_vault")
    return _journalise(conn, c, "export_vault", None, None,
                       lambda: c.export_vault(vault_db.dump(conn), destination))


def export_contents(conn, name, content_ids, destination=None):
    c = get(name, "export_contents")
    contents = [vault_db.get_content(conn, cid) for cid in content_ids]
    return _journalise(conn, c, "export_contents", "content", content_ids[0] if len(content_ids) == 1 else None,
                       lambda: c.export_contents(contents, destination))


_LECTEURS = {
    "content": vault_db.get_content,
    "resource": vault_db.get_resource,
    "review": vault_db.get_review,
    "action": vault_db.get_action,
}


def send(conn, entity_type, entity_id, name=None):
    """Envoie un objet du Vault vers un connecteur et memorise l'identifiant externe."""
    objet = _LECTEURS[entity_type](conn, entity_id)
    if entity_type == "action":
        if objet["status"] not in ("approved", "todo", "doing"):
            raise vault_db.VaultError(
                f"action {entity_id} au statut '{objet['status']}' : seule une action validee "
                "(approved, todo, doing) peut etre envoyee vers un outil externe", "not_validated")
        name = name or task_provider(conn)
        if not name:
            raise ConnectorError("aucun gestionnaire de taches choisi (onglet Integrations ou variable TASK_PROVIDER)")
    capacite = f"send_{entity_type}"
    c = get(name, capacite)
    existing = vault_db.get_external_link(conn, entity_type, entity_id, c.name)
    try:
        res = _journalise(conn, c, capacite, entity_type, entity_id,
                          lambda: getattr(c, capacite)(objet, existing))
    except ExternalNotFound as e:
        # Objet supprime chez le fournisseur : on retire le lien perime (pas de boucle d'erreurs) ;
        # le prochain envoi recreera l'objet et enregistrera le nouvel identifiant.
        vault_db.remove_external_link(conn, entity_type, entity_id, c.name)
        vault_db.log_sync(conn, c.name, "external_missing", "error", entity_type, entity_id,
                          f"objet externe {existing['external_id']} introuvable ; lien retire, a recreer ({e})")
        raise vault_db.VaultError(
            f"L'element {existing['external_id']} n'existe plus dans {c.label} (supprime ?). "
            f"Le lien a ete retire : renvoyez l'action pour le recreer.", "external_missing") from None
    vault_db.record_external_link(conn, entity_type, entity_id, c.name,
                                  res["external_id"], res.get("external_url"))
    ancien = vault_db.list_sync_log(conn, entity_type, entity_id, 2)
    if not existing and len(ancien) > 1 and ancien[1]["operation"] == "external_missing":
        vault_db.log_sync(conn, c.name, "recreated", "ok", entity_type, entity_id,
                          f"objet recree : {res['external_id']} remplace l'ancien identifiant")
    return {**res, "provider": c.name, "updated": existing is not None}


def send_content(conn, name, content_id):
    return send(conn, "content", content_id, name)


def send_action(conn, name, action_id):
    """Envoie une action VALIDEE vers un gestionnaire de taches externe."""
    return send(conn, "action", action_id, name)


__all__ = ["Connector", "PlannedConnector", "ConnectorError", "ExternalNotFound", "NotConfigured", "CAPABILITIES",
           "registry", "get", "env", "task_provider", "task_provider_info", "set_task_provider", "list_connectors",
           "export_vault", "export_contents", "send", "send_content", "send_action"]
