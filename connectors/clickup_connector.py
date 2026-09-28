"""
ClickUp : gestionnaire d'actions structure, prefere (priorite 1).

Une action VALIDEE du Vault devient une tache ClickUp dans la liste CLICKUP_LIST_ID.
Le Vault conserve external_provider = "clickup", external_task_id et external_url
(verification "action validee" et enregistrement faits par connectors/__init__.py) :
un second envoi de la meme action MET A JOUR la tache au lieu d'en creer une autre.

Configuration (variables d'environnement Windows, jamais stockees dans vault.db) :
    CLICKUP_API_TOKEN   jeton personnel ClickUp (pk_...)
    CLICKUP_LIST_ID     identifiant de la liste cible

Ligne de commande (depuis le dossier reels-vault) :
    python -m connectors.clickup_connector check                  verifie la config (lecture seule)
    python -m connectors.clickup_connector dry-run --action-id N  montre ce qui serait envoye, sans rien envoyer
    python -m connectors.clickup_connector send --action-id N --confirm   envoi reel d'UNE action

Le jeton n'est jamais affiche ni journalise.
"""

import json
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import Connector, ConnectorError, ExternalNotFound, NotConfigured, env

API = "https://api.clickup.com/api/v2"
MARQUEUR = "reels-vault:action:"  # identifie les taches creees par le Vault


lire_env = env  # lecture commune (processus puis variables Windows), voir base.env


def _ms(date_iso):
    """'AAAA-MM-JJ' -> horodatage ClickUp (millisecondes, midi UTC pour eviter les decalages de jour)."""
    d = datetime.strptime(date_iso, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def build_task(action):
    """Action Vault -> corps de requete ClickUp. Fonction pure : aucun appel reseau."""
    lignes = []
    if action.get("description"):
        lignes += [action["description"], ""]
    lignes += [
        "---",
        f"**Type d'action** : {action.get('action_type') or 'non precise'}",
        f"**Contenu source** : {action.get('content_title') or '-'}",
        f"**Lien source** : {action.get('content_source_url') or '-'}",
    ]
    if action.get("resource_name"):
        lignes.append(f"**Ressource** : {action['resource_name']}")
    lignes += ["", f"_Cree depuis Reels Vault - Vault ID : {MARQUEUR}{action['id']}_"]
    corps = {"name": action["title"], "markdown_description": "\n".join(lignes)}
    if action.get("priority"):
        corps["priority"] = int(action["priority"])  # 1 urgent, 2 haute, 3 normale, 4 basse : identique au Vault
    if action.get("due_date"):
        corps["due_date"] = _ms(action["due_date"])
        corps["due_date_time"] = False
    return corps


class ClickUpConnector(Connector):
    name = "clickup"
    label = "ClickUp"
    description = "Gestion structurée des actions validées (listes, priorités, échéances)."
    groups = ("tasks",)
    task_priority = 1
    capabilities = ("send_action",)
    required_env = ("CLICKUP_API_TOKEN", "CLICKUP_LIST_ID")
    todo = ("Correspondance statuts Vault ↔ statuts de la liste ClickUp (non synchronisés pour l'instant)",)

    def missing_config(self):
        return [v for v in self.required_env if not lire_env(v)]

    # -- HTTP (seul point de sortie reseau) ------------------------------------
    def _request(self, methode, chemin, corps=None):
        self._ensure_configured()
        req = Request(API + chemin, method=methode,
                      data=json.dumps(corps).encode("utf-8") if corps is not None else None,
                      headers={"Authorization": lire_env("CLICKUP_API_TOKEN"),
                               "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            err = ConnectorError(f"ClickUp a refuse la requete ({e.code}) : {detail}")
            err.status = e.code
            raise err from None
        except URLError as e:
            raise ConnectorError(f"ClickUp injoignable : {e.reason}") from None

    # -- Capacite du connecteur --------------------------------------------------
    def send_action(self, action, existing=None):
        corps = build_task(action)
        if existing:  # deja envoyee : mise a jour, jamais de doublon
            try:
                t = self._request("PUT", f"/task/{existing['external_id']}", corps)
            except ConnectorError as e:
                if self._tache_disparue(e):
                    raise ExternalNotFound(f"tache ClickUp {existing['external_id']} introuvable") from None
                raise
        else:
            t = self._request("POST", f"/list/{lire_env('CLICKUP_LIST_ID')}/task", corps)
        return {"external_id": t["id"], "external_url": t.get("url")}

    def _tache_disparue(self, erreur):
        """
        ClickUp repond 404, ou 401 « Team not authorized » (OAUTH_027), pour une tache supprimee.
        Un 401 peut aussi signifier un jeton invalide : on le confirme en verifiant que la liste
        reste accessible avec le meme jeton (lecture seule).
        """
        code = getattr(erreur, "status", None)
        if code == 404:
            return True
        if code in (400, 401) and any(m in str(erreur) for m in ("OAUTH_027", "ITEM_", "not found", "Not Found")):
            try:
                self._request("GET", f"/list/{lire_env('CLICKUP_LIST_ID')}")
                return True
            except ConnectorError:
                return False
        return False

    # -- Lecture seule (verification) --------------------------------------------
    def check(self):
        """Verifie jeton + liste sans rien ecrire ; compte les taches deja creees par le Vault."""
        liste = self._request("GET", f"/list/{lire_env('CLICKUP_LIST_ID')}")
        vault, page = 0, 0
        while True:
            r = self._request("GET", f"/list/{lire_env('CLICKUP_LIST_ID')}/task"
                                     f"?include_closed=true&subtasks=true&page={page}")
            taches = r.get("tasks", [])
            vault += sum(1 for t in taches if MARQUEUR in (t.get("description") or t.get("text_content") or ""))
            if r.get("last_page", True) or not taches:
                break
            page += 1
        return {"list_name": liste.get("name"), "vault_tasks": vault}


# ------------------------------------------------------------------ ligne de commande

def _main():
    import argparse

    sys.stdout.reconfigure(encoding="utf-8")
    import connectors
    import vault_db

    p = argparse.ArgumentParser(prog="python -m connectors.clickup_connector")
    p.add_argument("mode", choices=("check", "dry-run", "send"))
    p.add_argument("--action-id", type=int)
    p.add_argument("--confirm", action="store_true", help="obligatoire pour l'envoi reel")
    a = p.parse_args()
    c = ClickUpConnector()

    print("Configuration :")
    for v in c.required_env:
        print(f"  {v} : {'detecte (valeur masquee)' if lire_env(v) else 'ABSENT'}")
    if a.mode == "check":
        if c.missing_config():
            sys.exit("Configuration incomplete.")
        r = c.check()
        print(f"Liste ClickUp accessible : « {r['list_name']} » - taches creees par le Vault : {r['vault_tasks']}")
        return

    if not a.action_id:
        sys.exit("--action-id est obligatoire")
    conn = vault_db.connect()
    action = vault_db.get_action(conn, a.action_id)
    existing = vault_db.get_external_link(conn, "action", a.action_id, "clickup")
    valide = action["status"] in ("approved", "todo", "doing")
    corps = build_task(action)
    print(f"\nAction Vault #{action['id']} - statut : {action['status']} ({'validee' if valide else 'NON validee : envoi refuse'})")
    print(f"Operation : {'PUT /task/' + existing['external_id'] + ' (mise a jour)' if existing else 'POST /list/<CLICKUP_LIST_ID>/task (creation)'}")
    print("Corps de la requete :")
    print(json.dumps(corps, ensure_ascii=False, indent=2))

    if a.mode == "dry-run":
        print("\nDRY-RUN : aucune requete envoyee a ClickUp, rien n'a ete ecrit dans vault.db.")
        return
    if not a.confirm:
        sys.exit("\nEnvoi reel refuse : ajouter --confirm.")
    res = connectors.send(conn, "action", a.action_id, "clickup")
    print(f"\nEnvoye : tache {res['external_id']} ({'mise a jour' if res['updated'] else 'creee'}) - {res.get('external_url')}")


if __name__ == "__main__":
    try:
        _main()
    except (ConnectorError, NotConfigured) as e:
        sys.exit(f"Erreur : {e}")
