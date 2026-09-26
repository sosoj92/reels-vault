"""
Tests du connecteur ClickUp, SANS aucun appel reseau : urlopen est remplace par un faux
serveur qui enregistre les requetes. Aucune vraie tache ClickUp ne peut etre creee.

Lancement, depuis le dossier reels-vault :
    .venv\\Scripts\\python.exe -m unittest discover -s tests
"""

import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _isolation import RACINE, verifier

import connectors  # noqa: E402
import vault_db  # noqa: E402
from connectors import clickup_connector as cu  # noqa: E402

FAUX_JETON = "pk_FAUX_JETON_DE_TEST_123"


class FauxClickUp:
    """Remplace urlopen : enregistre chaque requete et repond comme l'API ClickUp."""

    def __init__(self):
        self.requetes = []

    def __call__(self, req, timeout=None):
        corps = json.loads(req.data.decode()) if req.data else None
        self.requetes.append((req.get_method(), req.full_url, dict(req.header_items()), corps))
        if req.get_method() == "POST":
            rep = {"id": "tache123", "url": "https://app.clickup.com/t/tache123"}
        elif req.get_method() == "PUT":
            rep = {"id": req.full_url.rsplit("/", 1)[1], "url": "https://app.clickup.com/t/maj"}
        else:
            rep = {"name": "Liste test", "tasks": [], "last_page": True}
        return mock.MagicMock(__enter__=lambda s: io.BytesIO(json.dumps(rep).encode()), __exit__=lambda *a: False)


class ClickUp(unittest.TestCase):
    def setUp(self):
        verifier()
        self.tmp = Path(tempfile.mkdtemp())
        self.conn = vault_db.connect(self.tmp / "vault.db")
        self.env = mock.patch.dict(os.environ, {"CLICKUP_API_TOKEN": FAUX_JETON, "CLICKUP_LIST_ID": "900"})
        self.env.start()
        self.faux = FauxClickUp()
        self.net = mock.patch.object(cu, "urlopen", self.faux)
        self.net.start()
        cid, _ = vault_db.upsert_content_from_import(self.conn, {
            "source_url": "https://vm.tiktok.com/SRC/", "title": "Contenu source", "platform": "TikTok"})
        self.conn.commit()
        self.action = vault_db.create_action(self.conn, {
            "title": "[TEST] Tester l'outil", "content_id": cid, "description": "Détail",
            "action_type": "tester", "priority": 2, "due_date": "2026-10-01", "status": "approved"})

    def tearDown(self):
        self.net.stop()
        self.env.stop()
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_configuration_detectee(self):
        c = connectors.get("clickup")
        self.assertTrue(c.is_configured())
        self.assertNotIn(FAUX_JETON, json.dumps(c.describe()))

    def test_non_configure_si_variable_vide(self):
        with mock.patch.dict(os.environ, {"CLICKUP_API_TOKEN": ""}):
            self.assertEqual(connectors.get("clickup").missing_config(), ["CLICKUP_API_TOKEN"])
            with self.assertRaises(connectors.NotConfigured):
                connectors.send(self.conn, "action", self.action["id"], "clickup")
        self.assertEqual(self.faux.requetes, [])

    def test_contenu_de_la_tache(self):
        corps = cu.build_task(vault_db.get_action(self.conn, self.action["id"]))
        self.assertEqual(corps["name"], "[TEST] Tester l'outil")
        self.assertEqual(corps["priority"], 2)
        self.assertEqual(corps["due_date"], 1790856000000)  # 2026-10-01 12:00 UTC
        self.assertFalse(corps["due_date_time"])
        d = corps["markdown_description"]
        for attendu in ("Détail", "tester", "https://vm.tiktok.com/SRC/", f"reels-vault:action:{self.action['id']}"):
            self.assertIn(attendu, d)
        self.assertEqual(self.faux.requetes, [], "build_task ne doit faire aucun appel reseau")

    def test_envoi_puis_mise_a_jour_sans_doublon(self):
        r1 = connectors.send(self.conn, "action", self.action["id"], "clickup")
        methode, url, entetes, corps = self.faux.requetes[0]
        self.assertEqual((methode, url), ("POST", "https://api.clickup.com/api/v2/list/900/task"))
        self.assertEqual(entetes["Authorization"], FAUX_JETON)
        a = vault_db.get_action(self.conn, self.action["id"])
        self.assertEqual((a["external_provider"], a["external_task_id"], a["external_url"]),
                         ("clickup", "tache123", "https://app.clickup.com/t/tache123"))
        self.assertFalse(r1["updated"])
        r2 = connectors.send(self.conn, "action", self.action["id"], "clickup")
        self.assertEqual(self.faux.requetes[1][:2], ("PUT", "https://api.clickup.com/api/v2/task/tache123"))
        self.assertTrue(r2["updated"])
        self.assertEqual(sum(1 for r in self.faux.requetes if r[0] == "POST"), 1)

    def test_action_non_validee_jamais_envoyee(self):
        p = vault_db.create_action(self.conn, {"title": "proposée", "proposed_by": "davy"})
        with self.assertRaises(vault_db.VaultError):
            connectors.send(self.conn, "action", p["id"], "clickup")
        self.assertEqual(self.faux.requetes, [])

    def test_erreur_http_sans_jeton(self):
        from urllib.error import HTTPError

        def refus(req, timeout=None):
            raise HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"err":"Token invalid"}'))
        with mock.patch.object(cu, "urlopen", refus):
            with self.assertRaises(connectors.ConnectorError) as e:
                connectors.send(self.conn, "action", self.action["id"], "clickup")
        self.assertIn("401", str(e.exception))
        self.assertNotIn(FAUX_JETON, str(e.exception))
        self.assertEqual(vault_db.sync_status(self.conn)["clickup"]["last_status"], "error")

    def test_dry_run_en_ligne_de_commande(self):
        env = {**os.environ, "REELS_VAULT_DB": str(self.tmp / "vault.db"), "PYTHONIOENCODING": "utf-8"}
        r = subprocess.run([sys.executable, "-m", "connectors.clickup_connector", "dry-run",
                            "--action-id", str(self.action["id"])], cwd=RACINE, env=env,
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY-RUN", r.stdout)
        self.assertIn("[TEST] Tester l'outil", r.stdout)
        self.assertNotIn(FAUX_JETON, r.stdout + r.stderr)
        self.assertIsNone(vault_db.get_action(self.conn, self.action["id"])["external_task_id"])
        self.assertEqual(vault_db.sync_status(self.conn), {})

    def test_envoi_reel_exige_confirm(self):
        env = {**os.environ, "REELS_VAULT_DB": str(self.tmp / "vault.db"), "PYTHONIOENCODING": "utf-8",
               "CLICKUP_LIST_ID": "", "CLICKUP_API_TOKEN": ""}  # meme sans --confirm, aucun vrai jeton
        r = subprocess.run([sys.executable, "-m", "connectors.clickup_connector", "send",
                            "--action-id", str(self.action["id"])], cwd=RACINE, env=env,
                           capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--confirm", r.stderr)


if __name__ == "__main__":
    unittest.main()
