"""
Test d'integration du pipeline Reels Vault, sur un Vault TEMPORAIRE (donnees fictives) :
ingestion -> indexation -> migration -> vault.db -> dashboard -> API (edition, notes,
ressources, actions, bilans, exports, connecteurs).

Le vrai Vault (raw/, index.md, vault.db...) n'est jamais lu ni modifie.
Aucun reseau : l'ingestion utilise ingest.ecrire_fiche avec des metadonnees fictives.

Lancement, depuis le dossier reels-vault (necessite fastapi + httpx, installes dans .venv) :
    .venv\\Scripts\\python.exe -m unittest tests.test_vault_pipeline -v
"""

import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from _isolation import RACINE, TMP, reseau_interdit, verifier

from fastapi.testclient import TestClient  # noqa: E402

import connectors.clickup_connector as _clickup  # noqa: E402
import ingest  # noqa: E402
import vault_api  # noqa: E402

_clickup.urlopen = reseau_interdit

INDEX_INITIAL = """# Index des vidéos sauvegardées

### Premier contenu de test
- lien : https://vm.tiktok.com/TESTAAA/
- plateforme : TikTok
- auteur : auteur.a
- catégorie : IA & Tech
- sous_thèmes : Outils & logiciels
- tags : gratuit, OpenAlternative
- contenu : Résumé IA du premier contenu.
- outils_ressources : OpenAlternative [site], Notion [logiciel] (exemple), « R-Tabel » (transcription incertaine)
"""
ENTREE_NOUVELLE = """
### Deuxième contenu : bons plans voyage
- lien : https://www.instagram.com/reel/TESTBBB/
- plateforme : Instagram
- auteur : voyageuse
- catégorie : Voyage
- sous_thèmes : Hébergement
- tags : bons plans, gratuit
- contenu : Résumé IA du deuxième contenu.
- outils_ressources : Airbnb [service], Méthode du sac unique [méthode]
"""
TAXONOMIE = """# Taxonomie du Vault

## Catégories et sous-thèmes utilisés

### IA & Tech
- Outils & logiciels

## Tags fréquents
- gratuit
"""


def run(*args):
    r = subprocess.run([sys.executable, *map(str, args)], cwd=RACINE, capture_output=True,
                       text=True, encoding="utf-8")
    if r.returncode:
        raise AssertionError(f"{args} -> {r.returncode}\n{r.stdout}\n{r.stderr}")
    return r.stdout


def migrer():
    return run(RACINE / "migrate_to_db.py", "--vault", TMP, "--db", TMP / "vault.db")


def dashboard(depuis_db=True):
    args = [RACINE / "generate_vault.py", "--index", TMP / "index.md", "--taxonomy", TMP / "taxonomy.md",
            "--out", TMP / "vault.html"]
    return run(*args, *(["--from-db", TMP / "vault.db"] if depuis_db else []))


def fiche(nom, lien, auteur, description, transcription, genre="video"):
    """Ingestion simulee : meme ecriture de fiche que ingest.py, sans telechargement."""
    img = TMP / "images" / f"{nom}_1.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0 fausse image")
    ingest.ecrire_fiche(TMP / "raw", TMP, nom, lien, {"uploader": auteur, "description": description,
                        "duration": 30, "title": description[:40]}, transcription, [img], genre)


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        verifier()
        for d in ("raw", "images"):
            (TMP / d).mkdir()
        (TMP / "index.md").write_text(INDEX_INITIAL, encoding="utf-8")
        (TMP / "taxonomy.md").write_text(TAXONOMIE, encoding="utf-8")
        (TMP / "progression.txt").write_text("tiktok_TESTAAA.md\n", encoding="utf-8")
        (TMP / "journal.json").write_text("{}", encoding="utf-8")
        fiche("tiktok_TESTAAA", "https://vm.tiktok.com/TESTAAA/", "auteur.a", "Description originale A",
              "Transcription du contenu A avec Notion.")
        migrer()
        cls.api = TestClient(vault_api.app)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def get(self, path, code=200):
        r = self.api.get(path)
        self.assertEqual(r.status_code, code, r.text)
        return r.json()

    def send(self, method, path, body=None, code=None):
        r = self.api.request(method, path, json=body)
        if code:
            self.assertEqual(r.status_code, code, r.text)
        return r

    def id_par_titre(self, debut):
        """Repere un contenu par son lien source (stable meme si le titre est modifie)."""
        lien = {"Premier": "https://vm.tiktok.com/TESTAAA/", "Deuxième": "https://www.instagram.com/reel/TESTBBB/"}[debut]
        return next(c["id"] for c in self.get("/contents?limit=500")["items"] if c["source_url"] == lien)

    # 1-5 et 18 : ingestion, indexation, migration, vault.db, dashboard depuis la DB, pas de doublon
    def test_01_pipeline_nouveau_contenu(self):
        fiche("insta_TESTBBB", "https://www.instagram.com/reel/TESTBBB/", "voyageuse",
              "Description originale B", "")
        with open(TMP / "index.md", "a", encoding="utf-8") as f:
            f.write(ENTREE_NOUVELLE)
        with open(TMP / "progression.txt", "a", encoding="utf-8") as f:
            f.write("insta_TESTBBB.md\n")
        sortie = migrer()
        self.assertIn("[created  ]", sortie)
        c = self.get("/contents?limit=50")
        self.assertEqual(c["total"], 2)
        b = self.get(f"/contents/{self.id_par_titre('Deuxième')}")
        self.assertEqual((b["platform"], b["category"], b["subthemes"]), ("Instagram", "Voyage", ["Hébergement"]))
        self.assertEqual({r["name"]: r["type"] for r in b["resources"]},
                         {"Airbnb": "service", "Méthode du sac unique": "méthode"})
        self.assertEqual(b["media"][0]["path"], "images/insta_TESTBBB_1.jpg")
        a = self.get(f"/contents/{self.id_par_titre('Premier')}")
        types = {r["name"]: r["type"] for r in a["resources"]}
        self.assertEqual(types["OpenAlternative"], "site")
        self.assertEqual(types["R-Tabel"], "autre")  # pas de type -> autre, jamais devine
        self.assertIn("2 contenu(s)", dashboard())
        self.assertIn("Deuxième contenu", (TMP / "vault.html").read_text(encoding="utf-8"))
        # 18 : relance -> aucun doublon
        self.assertIn("0 cree(s)", migrer())
        self.assertEqual(self.get("/health")["contents"], 2)

    # 6-7 : description manuelle, persistance apres migration + regeneration, retour a l'IA
    def test_02_description_manuelle(self):
        cid = self.id_par_titre("Premier")
        c = self.send("PATCH", f"/contents/{cid}", {"description_manual": "Ma description corrigée"}, 200).json()
        self.assertEqual((c["description"], c["description_source"]), ("Ma description corrigée", "manual"))
        self.send("PATCH", f"/contents/{cid}", {"title": "Titre choisi à la main", "tags": ["gratuit", "perso"]}, 200)
        self.send("PATCH", f"/contents/{cid}", {"description_manual": "piratée", "edited_by": "ai"}, 409)
        self.send("PATCH", f"/contents/{cid}", {"title": "titre IA", "edited_by": "ai"}, 409)
        migrer()
        dashboard()
        c = self.get(f"/contents/{cid}")
        self.assertEqual(c["description_manual"], "Ma description corrigée")
        self.assertEqual(c["title"], "Titre choisi à la main")
        self.assertEqual(c["tags"], ["gratuit", "perso"])
        html = (TMP / "vault.html").read_text(encoding="utf-8")
        self.assertIn("Ma description corrigée", html)
        self.assertIn("Titre choisi à la main", html)
        c = self.send("POST", f"/contents/{cid}/description/revert", code=200).json()
        self.assertEqual((c["description_source"], c["description"]), ("ai", "Résumé IA du premier contenu."))
        self.assertTrue(any(r["field"] == "description_manual" and r["old_value"] == "Ma description corrigée"
                            for r in c["revisions"]), "l'ancienne description reste dans l'historique")

    # 8-9 : notes et ressources
    def test_03_notes_et_ressources(self):
        cid = self.id_par_titre("Deuxième")
        n = self.send("POST", f"/contents/{cid}/notes", {"text": "À garder pour l'été"}, 201).json()
        self.send("PATCH", f"/notes/{n['id']}", {"text": "À garder pour juillet"}, 200)
        r = self.send("POST", f"/contents/{cid}/resources", {"name": "Booking", "type": "site"}, 201).json()
        self.send("POST", f"/contents/{cid}/resources", {"name": "Booking"}, 422)
        self.send("POST", f"/contents/{cid}/resources", {"name": "X", "type": "inconnu"}, 422)
        self.send("PATCH", f"/resources/{r['id']}", {"type": "service"}, 200)
        migrer()  # une ressource modifiee a la main n'est jamais ecrasee
        c = self.get(f"/contents/{cid}")
        self.assertEqual(c["notes"][0]["text"], "À garder pour juillet")
        self.assertEqual({x["name"]: x["type"] for x in c["resources"]}["Booking"], "service")
        cat = self.get("/resources/catalog")
        self.assertEqual({g["name"] for g in cat["items"]} >= {"Airbnb", "Booking", "OpenAlternative"}, True)
        self.assertEqual(self.get("/resources/catalog?type=méthode")["total"], 1)

    # 10-12 : creation, validation, changement de statut ; IA -> proposed ; pas d'envoi auto
    def test_04_actions(self):
        cid = self.id_par_titre("Premier")
        res = next(r for r in self.get(f"/contents/{cid}")["resources"] if r["name"] == "OpenAlternative")
        a = self.send("POST", "/actions", {"title": "Tester OpenAlternative", "resource_id": res["id"],
                                           "action_type": "tester", "priority": 1, "proposed_by": "davy",
                                           "status": "approved"}, 201).json()
        self.assertEqual((a["status"], a["content_id"], a["resource_name"]), ("proposed", cid, "OpenAlternative"))
        self.assertIsNone(a["external_provider"])
        self.send("POST", "/actions", {"title": "x", "action_type": "type-inconnu"}, 422)
        self.send("POST", f"/actions/{a['id']}/send", {"connector": "clickup"}, 409)  # non validee
        self.assertEqual(self.send("PATCH", f"/actions/{a['id']}", {"status": "approved"}, 200).json()["status"], "approved")
        for s in ("todo", "doing", "done"):
            self.assertEqual(self.send("PATCH", f"/actions/{a['id']}", {"status": s}, 200).json()["status"], s)
        self.send("PATCH", f"/actions/{a['id']}", {"status": "nimporte"}, 422)
        cat = self.get("/resources/catalog?q=openalternative")["items"][0]
        self.assertEqual(cat["actions"][0]["title"], "Tester OpenAlternative")
        self.assertEqual(self.get("/stats")["actions_done"], 1)

    # 14-15 : exports Excel et CSV
    def test_05_exports(self):
        x = self.send("POST", "/exports/excel", {}, 200).json()
        self.assertTrue(Path(x["files"][0]).is_file())
        self.assertEqual(x["downloads"], ["/exports/files/reels-vault-export.xlsx"])
        self.assertEqual(self.api.get(x["downloads"][0]).status_code, 200)
        import openpyxl
        wb = openpyxl.load_workbook(x["files"][0])
        self.assertEqual(wb.sheetnames, ["CONTENUS", "RESSOURCES", "ACTIONS", "TAGS", "REVIEWS"])
        c = self.send("POST", "/exports/csv", {}, 200).json()
        self.assertEqual(len(c["files"]), 3)
        one = self.send("POST", "/exports/csv", {"content_ids": [self.id_par_titre("Deuxième")]}, 200).json()
        self.assertEqual(len(one["files"]), 1)
        self.assertEqual(self.api.get("/exports/files/..%2Fvault.db").status_code, 404)
        self.assertIsNotNone(self.get("/sync/status")["excel"]["last_success"])

    # 16-17 : bilan et historique
    def test_06_review(self):
        prev = self.get("/review/since-last")
        self.assertEqual(prev["counts"]["new_contents"], 2)
        self.assertIn("tools_to_test", prev["blocks"])
        r = self.send("POST", "/reviews", {}, 201).json()
        self.assertEqual(r["stats"]["new_contents"], 2)
        self.assertEqual(r["details"]["counts"]["new_contents"], 2)
        hist = self.get("/reviews")["items"]
        self.assertEqual([h["id"] for h in hist], [r["id"]])
        self.assertEqual(self.get("/review/since-last")["counts"]["new_contents"], 0)
        self.assertEqual(self.get(f"/reviews/{r['id']}")["id"], r["id"])

    # 19 : connecteurs non configures ; TASK_PROVIDER interchangeable
    def test_07_connecteurs(self):
        items = {c["name"]: c for c in self.get("/connectors")["items"]}
        attendus = {"excel", "csv", "google_sheets", "google_drive", "notion", "davy",
                    "clickup", "trello", "google_tasks", "todoist"}
        self.assertTrue(attendus <= set(items))
        self.assertTrue(items["csv"]["configured"] and items["excel"]["configured"])
        for n in attendus - {"csv", "excel"}:
            self.assertFalse(items[n]["configured"], n)
        taches = sorted((c for c in items.values() if "tasks" in c["groups"]), key=lambda c: c["task_priority"])
        self.assertEqual([c["name"] for c in taches], ["clickup", "notion", "trello", "google_tasks", "todoist"])
        aid = self.send("POST", "/actions", {"title": "Valide", "status": "approved"}, 201).json()["id"]
        for n in ("clickup", "notion", "trello", "google_tasks", "todoist"):
            r = self.send("POST", f"/actions/{aid}/send", {"connector": n})
            self.assertEqual(r.status_code, 501, r.text)
        self.assertEqual(self.send("POST", f"/actions/{aid}/send", {}).status_code, 502)  # aucun TASK_PROVIDER
        os.environ["TASK_PROVIDER"] = "trello"
        try:
            self.assertEqual(self.get("/connectors")["task_provider"], "trello")
            r = self.send("POST", f"/actions/{aid}/send", {})
            self.assertEqual(r.status_code, 501)
            self.assertIn("Trello", r.json()["detail"])
        finally:
            os.environ["TASK_PROVIDER"] = ""
        a = self.get(f"/actions/{aid}")
        self.assertIsNone(a["external_task_id"])  # rien n'a ete cree
        self.assertEqual(self.get("/sync/status")["clickup"]["last_status"], "not_configured")

    # 20 : pipeline historique (index.md -> vault.html) toujours fonctionnel
    def test_08_pipeline_historique(self):
        self.assertIn("2 contenu(s)", dashboard(depuis_db=False))
        html = (TMP / "vault.html").read_text(encoding="utf-8")
        self.assertIn("Premier contenu de test", html)  # titre de index.md, pas celui modifie en base
        donnees = json.loads(html.split('type="application/json">', 1)[1].split("</script>", 1)[0])
        self.assertEqual(len(donnees["entrees"]), 2)

    def test_09_interface_servie(self):
        r = self.api.get("/app")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Reels Vault", r.text)
        self.assertEqual(self.api.get("/images/insta_TESTBBB_1.jpg").status_code, 200)
        self.assertEqual(self.api.get("/images/../vault.db").status_code, 404)


if __name__ == "__main__":
    unittest.main()
