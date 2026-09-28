#!/usr/bin/env python3
"""
vault_client.py - Client Python de l'API locale du Reels Vault (bibliotheque standard).

C'est l'interface que Davy (ou tout autre programme) doit utiliser pour lire
et enrichir le Vault, au lieu d'ouvrir directement les fichiers ou vault.db.
L'API doit etre lancee (python vault_api.py).

Exemple :
    from vault_client import VaultClient
    v = VaultClient()
    v.get_recent_contents(5)
    v.search_vault("CRM")
    v.propose_action(3, "Tester l'orchestrateur", action_type="tester", proposed_by="davy")
"""

import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class VaultAPIError(Exception):
    def __init__(self, status, payload):
        super().__init__(f"{status} : {payload}")
        self.status, self.payload = status, payload


class VaultClient:
    def __init__(self, base_url="http://127.0.0.1:8765", agent="davy", timeout=30):
        self.base_url = base_url.rstrip("/")
        self.agent = agent  # nom inscrit dans proposed_by pour les actions proposees
        self.timeout = timeout

    def _call(self, methode, chemin, params=None, body=None):
        url = self.base_url + chemin
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        req = Request(url, data=data, method=methode, headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            raise VaultAPIError(e.code, e.read().decode("utf-8", "replace")) from None

    # -- lecture
    def health(self):
        return self._call("GET", "/health")

    def get_recent_contents(self, limit=5):
        return self._call("GET", "/contents/recent", {"limit": limit})["items"]

    def search_vault(self, query, limit=50):
        return self._call("GET", "/search", {"q": query, "limit": limit})["items"]

    def get_unreviewed_contents(self):
        return self._call("GET", "/contents/unreviewed")["items"]

    def get_content(self, content_id):
        return self._call("GET", f"/contents/{content_id}")

    def get_resources(self, type=None, content_id=None, query=None):
        return self._call("GET", "/resources", {"type": type, "content_id": content_id, "q": query})["items"]

    def get_actions(self, status=None, content_id=None):
        return self._call("GET", "/actions", {"status": status, "content_id": content_id})["items"]

    def get_actions_since_last_review(self):
        return self._call("GET", "/actions/since-last-review")["items"]

    def get_review_since_last(self):
        return self._call("GET", "/review/since-last")

    # -- ecriture (une IA ne peut jamais ecraser une correction humaine)
    def update_description(self, content_id, text):
        """Ecrit description_ai ; description_manual (humaine) reste prioritaire et intacte."""
        return self._call("PATCH", f"/contents/{content_id}", body={"description_ai": text, "edited_by": "ai"})

    def propose_action(self, content_id, title, description=None, action_type=None, priority=None, due_date=None):
        """Cree une action au statut 'proposed' : rien n'est envoye vers un outil externe."""
        return self._call("POST", "/actions", body={
            "content_id": content_id, "title": title, "description": description,
            "action_type": action_type, "priority": priority, "due_date": due_date,
            "proposed_by": self.agent,
        })

    def add_note(self, content_id, text):
        return self._call("POST", f"/contents/{content_id}/notes", body={"text": text})
