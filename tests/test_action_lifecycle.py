"""
Cycle de vie des actions via l'API : proposee -> validee -> envoyee -> terminee (ou ignoree),
envoi vers le gestionnaire de taches configure (TASK_PROVIDER ou reglage de l'interface),
et tache externe supprimee a la main.

Aucun appel reseau : ClickUp est simule (urlopen remplace), et un gestionnaire fictif
prouve que l'API/l'interface ne dependent d'aucun fournisseur precis.

Lancement, depuis le dossier reels-vault :
    .venv\\Scripts\\python.exe -m unittest discover -s tests
"""

import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from _isolation import verifier
from fastapi.testclient import TestClient

import connectors
import vault_api
import vault_db
from connectors import clickup_connector as cu
from connectors.base import Connector, ExternalNotFound


class FauxGestionnaire(Connector):
    """Gestionnaire de taches fictif : prouve que rien n'est code pour un fournisseur precis."""
    name = "fauxtaches"
    label = "FauxTâches"
    groups = ("tasks",)
    task_priority = 9
    capabilities = ("send_action",)
    supprimees = set()
    compteur = 0

    def send_action(self, action, existing=None):
        if existing:
            if existing["external_id"] in self.supprimees:
                raise ExternalNotFound(f"{existing['external_id']} supprimee")
            return {"external_id": existing["external_id"], "external_url": f"https://faux/{existing['external_id']}"}
        FauxGestionnaire.compteur += 1
        tid = f"F{FauxGestionnaire.compteur}"
        return {"external_id": tid, "external_url": f"https://faux/{tid}"}


class FauxClickUp:
    """Simule l'API ClickUp ; les taches de `supprimees` repondent comme une tache effacee."""

    def __init__(self):
        self.requetes, self.supprimees, self.n = [], set(), 0

    def __call__(self, req, timeout=None):
        m, url = req.get_method(), req.full_url
        self.requetes.append((m, url))
        tid = url.rsplit("/", 1)[1]
        if m == "PUT" and tid in self.supprimees:
            raise HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b'{"err":"Team not authorized","ECODE":"OAUTH_027"}'))
        if m == "POST":
            self.n += 1
            rep = {"id": f"cu{self.n}", "url": f"https://app.clickup.com/t/cu{self.n}"}
        elif m == "PUT":
            rep = {"id": tid, "url": f"https://app.clickup.com/t/{tid}"}
        else:
            rep = {"name": "Liste"}
        return mock.MagicMock(__enter__=lambda s: io.BytesIO(json.dumps(rep).encode()), __exit__=lambda *a: False)


class Cycle(unittest.TestCase):
    def setUp(self):
        verifier()
        self.tmp = Path(tempfile.mkdtemp())
        self.db = mock.patch.object(vault_db, "DB_PATH", self.tmp / "vault.db")
        self.db.start()
        connectors.registry()["fauxtaches"] = FauxGestionnaire()
        FauxGestionnaire.supprimees = set()
        FauxGestionnaire.compteur = 0
        self.api = TestClient(vault_api.app)
        conn = vault_db.connect()
        self.cid, _ = vault_db.upsert_content_from_import(conn, {
            "source_url": "https://vm.tiktok.com/CYCLE/", "title": "Contenu du cycle", "platform": "TikTok"})
        conn.commit()
        conn.close()

    def tearDown(self):
        connectors.registry().pop("fauxtaches", None)
        self.db.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, method, path, body=None, code=200):
        r = self.api.request(method, path, json=body)
        self.assertEqual(r.status_code, code, r.text)
        return r.json()

    def proposer(self, **kw):
        return self.call("POST", "/actions", {"title": "Tester l'outil", "content_id": self.cid,
                                              "action_type": "tester", "priority": 2, "proposed_by": "davy", **kw}, 201)

    def definir(self, nom):
        return self.call("PUT", "/settings/task-provider", {"provider": nom})

    # -- cycle complet ------------------------------------------------------------
    def test_cycle_complet_avec_gestionnaire_generique(self):
        a = self.proposer()
        self.assertEqual((a["status"], a["lifecycle"]), ("proposed", "proposed"))
        self.call("POST", f"/actions/{a['id']}/send", {}, 409)  # non validee : jamais envoyee
        self.assertEqual(self.call("POST", f"/actions/{a['id']}/approve")["lifecycle"], "approved")
        self.call("POST", f"/actions/{a['id']}/send", {}, 502)  # aucun gestionnaire choisi
        info = self.definir("fauxtaches")
        self.assertEqual((info["name"], info["label"], info["source"], info["configured"]),
                         ("fauxtaches", "FauxTâches", "setting", True))
        self.assertEqual(self.call("GET", "/config")["task_provider_info"]["label"], "FauxTâches")
        r = self.call("POST", f"/actions/{a['id']}/send", {})
        self.assertEqual((r["provider"], r["external_id"], r["updated"]), ("fauxtaches", "F1", False))
        self.assertEqual(r["action"]["lifecycle"], "sent")  # affichable immediatement
        self.assertEqual(r["action"]["external_url"], "https://faux/F1")
        sync = self.call("GET", f"/actions/{a['id']}/sync")
        self.assertEqual((sync["external_task_id"], sync["links"][0]["external_id"]), ("F1", "F1"))
        self.assertEqual(sync["log"][0]["status"], "ok")
        r = self.call("POST", f"/actions/{a['id']}/send", {})  # renvoi = mise a jour, pas de doublon
        self.assertEqual((r["external_id"], r["updated"]), ("F1", True))
        self.assertEqual(FauxGestionnaire.compteur, 1)
        self.assertEqual(self.call("POST", f"/actions/{a['id']}/complete")["lifecycle"], "completed")
        fiche = self.call("GET", f"/contents/{self.cid}")
        self.assertEqual(fiche["actions"][0]["lifecycle"], "completed")

    def test_ignorer_restaurer_modifier(self):
        a = self.proposer()
        self.assertEqual(self.call("POST", f"/actions/{a['id']}/ignore")["lifecycle"], "ignored")
        self.assertEqual(self.call("PATCH", f"/actions/{a['id']}", {"status": "proposed"})["lifecycle"], "proposed")
        m = self.call("PATCH", f"/actions/{a['id']}", {"title": "Nouveau titre", "priority": 1, "due_date": "2026-11-02"})
        self.assertEqual((m["title"], m["priority"], m["due_date"]), ("Nouveau titre", 1, "2026-11-02"))
        self.call("POST", "/actions/9999/approve", code=404)

    def test_filtres_par_cycle_de_vie(self):
        self.definir("fauxtaches")
        ids = [self.proposer(title=f"A{i}")["id"] for i in range(5)]
        self.call("POST", f"/actions/{ids[1]}/approve")
        self.call("POST", f"/actions/{ids[2]}/approve")
        self.call("POST", f"/actions/{ids[2]}/send", {})
        self.call("POST", f"/actions/{ids[3]}/complete")
        self.call("POST", f"/actions/{ids[4]}/ignore")
        r = self.call("GET", "/actions")
        self.assertEqual({k: r["counts"][k] for k in ("proposed", "approved", "sent", "completed", "ignored")},
                         {"proposed": 1, "approved": 1, "sent": 1, "completed": 1, "ignored": 1})
        for etat, attendu in (("proposed", ids[0]), ("approved", ids[1]), ("sent", ids[2]),
                              ("completed", ids[3]), ("ignored", ids[4])):
            self.assertEqual([x["id"] for x in self.call("GET", f"/actions?lifecycle={etat}")["items"]], [attendu])
        self.call("GET", "/actions?lifecycle=nimporte", code=422)

    # -- tache externe supprimee a la main ---------------------------------------------
    def test_tache_supprimee_generique(self):
        self.definir("fauxtaches")
        a = self.proposer(status="approved", proposed_by="user")
        self.call("POST", f"/actions/{a['id']}/send", {})
        FauxGestionnaire.supprimees.add("F1")
        err = self.call("POST", f"/actions/{a['id']}/send", {}, 409)
        self.assertEqual(err["error"], "external_missing")
        b = self.call("GET", f"/actions/{a['id']}")
        self.assertEqual((b["lifecycle"], b["external_task_id"]), ("approved", None))  # pas de boucle d'erreurs
        r = self.call("POST", f"/actions/{a['id']}/send", {})  # recreation propre
        self.assertEqual((r["external_id"], r["updated"]), ("F2", False))
        sync = self.call("GET", f"/actions/{a['id']}/sync")
        self.assertEqual([x["external_id"] for x in sync["links"]], ["F2"])
        ops = [x["operation"] for x in sync["log"]]
        self.assertIn("external_missing", ops)
        self.assertEqual(ops[0], "recreated")

    def test_tache_supprimee_clickup_simule(self):
        faux = FauxClickUp()
        with mock.patch.dict(os.environ, {"CLICKUP_API_TOKEN": "pk_FAUX", "CLICKUP_LIST_ID": "900"}), \
                mock.patch.object(cu, "urlopen", faux):
            self.definir("clickup")
            a = self.proposer(status="approved", proposed_by="user")
            r = self.call("POST", f"/actions/{a['id']}/send", {})
            self.assertEqual((r["external_id"], r["action"]["lifecycle"]), ("cu1", "sent"))
            faux.supprimees.add("cu1")
            self.assertEqual(self.call("POST", f"/actions/{a['id']}/send", {}, 409)["error"], "external_missing")
            self.assertIn(("GET", "https://api.clickup.com/api/v2/list/900"), faux.requetes)  # jeton confirme valide
            r = self.call("POST", f"/actions/{a['id']}/send", {})
            self.assertEqual((r["external_id"], r["action"]["external_url"]), ("cu2", "https://app.clickup.com/t/cu2"))
            self.assertEqual(sum(1 for m, _ in faux.requetes if m == "POST"), 2)

    def test_jeton_invalide_n_efface_pas_le_lien(self):
        """Un 401 d'authentification (liste inaccessible aussi) n'est PAS une tache supprimee."""
        def refus(req, timeout=None):
            if req.get_method() == "POST":
                return FauxClickUp()(req)
            raise HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"err":"Token invalid","ECODE":"OAUTH_025"}'))
        with mock.patch.dict(os.environ, {"CLICKUP_API_TOKEN": "pk_FAUX", "CLICKUP_LIST_ID": "900"}), \
                mock.patch.object(cu, "urlopen", refus):
            self.definir("clickup")
            a = self.proposer(status="approved", proposed_by="user")
            self.call("POST", f"/actions/{a['id']}/send", {})
            self.call("POST", f"/actions/{a['id']}/send", {}, 502)
            self.assertEqual(self.call("GET", f"/actions/{a['id']}")["external_task_id"], "cu1")

    # -- reglage du gestionnaire -------------------------------------------------------
    def test_reglage_gestionnaire(self):
        self.call("PUT", "/settings/task-provider", {"provider": "excel"}, 502)  # pas un gestionnaire de taches
        self.call("PUT", "/settings/task-provider", {"provider": "inconnu"}, 502)
        self.definir("fauxtaches")
        with mock.patch.dict(os.environ, {"TASK_PROVIDER": "trello"}):  # la variable est prioritaire
            info = self.call("GET", "/settings/task-provider")
            self.assertEqual((info["name"], info["source"], info["configured"]), ("trello", "env", False))
        self.assertEqual(self.definir(None)["name"], None)


if __name__ == "__main__":
    unittest.main()
