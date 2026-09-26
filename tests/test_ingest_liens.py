"""
Tests de ingest.extraire_liens (sans reseau, sans yt-dlp ni Whisper).

Lancement, depuis le dossier reels-vault :
    python -m unittest discover tests
"""

import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest import extraire_liens  # noqa: E402


def liens_de(texte):
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "inbox.txt"
        f.write_text(texte, encoding="utf-8")
        return extraire_liens(f)


class ExtraireLiens(unittest.TestCase):
    def test_liens_colles_sont_separes(self):
        texte = ("https://vm.tiktok.com/ZN8MqKKtw/https://vm.tiktok.com/ZN8MbkCHp/"
                 "https://vm.tiktok.com/ZN8MbU47u/https://vm.tiktok.com/ZN8Mbu3um/")
        self.assertEqual(liens_de(texte), [
            "https://vm.tiktok.com/ZN8MqKKtw/",
            "https://vm.tiktok.com/ZN8MbkCHp/",
            "https://vm.tiktok.com/ZN8MbU47u/",
            "https://vm.tiktok.com/ZN8Mbu3um/",
        ])

    def test_liens_colles_tiktok_et_instagram(self):
        texte = "https://vm.tiktok.com/AAA/https://www.instagram.com/reel/BBB/?igsh=x"
        self.assertEqual(liens_de(texte), [
            "https://vm.tiktok.com/AAA/",
            "https://www.instagram.com/reel/BBB/?igsh=x",
        ])

    def test_liens_colles_www_vm_vt(self):
        texte = ("https://www.tiktok.com/@a/video/111https://vm.tiktok.com/BBB/"
                 "https://vt.tiktok.com/CCC/")
        self.assertEqual(liens_de(texte), [
            "https://www.tiktok.com/@a/video/111",
            "https://vm.tiktok.com/BBB/",
            "https://vt.tiktok.com/CCC/",
        ])

    def test_liens_colles_apres_query_string(self):
        texte = "https://www.tiktok.com/@a/video/111?_r=1&_t=ZN-9https://vm.tiktok.com/BBB/"
        self.assertEqual(liens_de(texte), [
            "https://www.tiktok.com/@a/video/111?_r=1&_t=ZN-9",
            "https://vm.tiktok.com/BBB/",
        ])

    def test_doublons_dans_liens_colles(self):
        texte = ("https://vm.tiktok.com/AAA/https://vm.tiktok.com/BBB/"
                 "https://vm.tiktok.com/AAA/\nhttps://vm.tiktok.com/BBB/\n")
        self.assertEqual(liens_de(texte), ["https://vm.tiktok.com/AAA/", "https://vm.tiktok.com/BBB/"])

    def test_lignes_et_liens_colles_melanges(self):
        texte = ("https://www.instagram.com/p/III/\n"
                 "https://vm.tiktok.com/AAA/https://www.instagram.com/reel/JJJ/\n"
                 "https://vt.tiktok.com/CCC/\n")
        self.assertEqual(liens_de(texte), [
            "https://www.instagram.com/p/III/",
            "https://vm.tiktok.com/AAA/",
            "https://www.instagram.com/reel/JJJ/",
            "https://vt.tiktok.com/CCC/",
        ])

    def test_aucun_lien_ne_contient_deux_https(self):
        texte = "https://vm.tiktok.com/A/https://vm.tiktok.com/B/https://vm.tiktok.com/C/"
        liens = liens_de(texte)
        self.assertEqual(len(liens), 3)
        self.assertTrue(all(l.count("://") == 1 for l in liens))

    def test_instagram_sans_www_parametres_colles(self):
        texte = ("https://instagram.com/reel/AAA/?igsh=abc123https://www.instagram.com/p/BBB/?utm_source=ig"
                 "https://vm.tiktok.com/CCC/")
        self.assertEqual(liens_de(texte), [
            "https://instagram.com/reel/AAA/?igsh=abc123",
            "https://www.instagram.com/p/BBB/?utm_source=ig",
            "https://vm.tiktok.com/CCC/",
        ])

    def test_vt_avec_parametres_colle(self):
        texte = "https://vt.tiktok.com/ZSabc/?k=1https://vt.tiktok.com/ZSdef/"
        self.assertEqual(liens_de(texte), ["https://vt.tiktok.com/ZSabc/?k=1", "https://vt.tiktok.com/ZSdef/"])

    def test_http_et_https_colles(self):
        texte = "http://vm.tiktok.com/AAA/https://vm.tiktok.com/BBB/http://www.instagram.com/p/CCC/"
        self.assertEqual(liens_de(texte), [
            "http://vm.tiktok.com/AAA/", "https://vm.tiktok.com/BBB/", "http://www.instagram.com/p/CCC/"])

    def test_separes_par_espaces_toutes_plateformes(self):
        texte = ("https://vm.tiktok.com/AAA/ https://vt.tiktok.com/BBB/  "
                 "https://www.tiktok.com/@x/video/1?lang=fr\thttps://www.instagram.com/reel/CCC/?igsh=z")
        self.assertEqual(liens_de(texte), [
            "https://vm.tiktok.com/AAA/", "https://vt.tiktok.com/BBB/",
            "https://www.tiktok.com/@x/video/1?lang=fr", "https://www.instagram.com/reel/CCC/?igsh=z"])

    def test_inbox_macrodroid_reelle(self):
        """Forme exacte observee dans mobile-inbox/inbox.txt : liens colles puis lignes normales (CRLF)."""
        texte = ("https://vm.tiktok.com/ZN8MqKKtw/https://vm.tiktok.com/ZN8MbkCHp/https://vm.tiktok.com/ZN8MbU47u/"
                 "https://vm.tiktok.com/ZN8Mbu3um/\r\nhttps://vm.tiktok.com/ZN8MWKQtX/\r\n"
                 "https://vm.tiktok.com/ZN8MqKKtw/\r\n")
        self.assertEqual(liens_de(texte), [
            "https://vm.tiktok.com/ZN8MqKKtw/", "https://vm.tiktok.com/ZN8MbkCHp/",
            "https://vm.tiktok.com/ZN8MbU47u/", "https://vm.tiktok.com/ZN8Mbu3um/",
            "https://vm.tiktok.com/ZN8MWKQtX/",
        ])

    # --- comportements existants inchanges ---------------------------------
    def test_un_lien_par_ligne(self):
        texte = "https://vm.tiktok.com/AAA/\nhttps://www.instagram.com/p/BBB/\n"
        self.assertEqual(liens_de(texte), ["https://vm.tiktok.com/AAA/", "https://www.instagram.com/p/BBB/"])

    def test_lien_long_avec_parametres(self):
        lien = "https://www.tiktok.com/@abdelhadi.abl/video/7688298300478344481?_r=1&_t=ZN-99xJJzEupR0"
        self.assertEqual(liens_de(f"regarde ça : {lien}"), [lien])

    def test_doublons_supprimes_ordre_conserve(self):
        texte = "https://vm.tiktok.com/B/ https://vm.tiktok.com/A/ https://vm.tiktok.com/B/"
        self.assertEqual(liens_de(texte), ["https://vm.tiktok.com/B/", "https://vm.tiktok.com/A/"])

    def test_ponctuation_finale_retiree(self):
        self.assertEqual(liens_de("vu sur https://vm.tiktok.com/AAA/."), ["https://vm.tiktok.com/AAA/"])

    def test_export_json(self):
        texte = '[{"link": "https://www.instagram.com/reel/XYZ/"}, {"link": "https://vm.tiktok.com/AAA/"}]'
        self.assertEqual(liens_de(texte), ["https://www.instagram.com/reel/XYZ/", "https://vm.tiktok.com/AAA/"])

    def test_autres_sites_ignores(self):
        self.assertEqual(liens_de("https://youtube.com/watch?v=1 https://example.com/"), [])


if __name__ == "__main__":
    unittest.main()
