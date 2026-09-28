import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import importlib.util
import subprocess
import sys
import types
import unittest
from unittest import mock

from _isolation import RACINE


def _load_tool():
    """Charge integrations/jarvis/life_intelligence.py hors runtime Jarvis."""
    core = types.ModuleType("core")
    config = types.ModuleType("core.config")
    config.reglage = lambda chemin, defaut=None: defaut
    registre = types.ModuleType("core.registre")
    registre.outil = lambda **kwargs: (lambda func: func)
    stubs = {"core": core, "core.config": config, "core.registre": registre}
    with mock.patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            "life_intelligence_under_test",
            RACINE / "integrations" / "jarvis" / "life_intelligence.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


tool = _load_tool()


class RouterTests(unittest.TestCase):
    def route(self, phrase):
        result = tool.router_demande_agents(phrase)
        return result and result[0]

    def test_personal_recall_goes_to_ask(self):
        for phrase in (
            "Qu'est-ce que j'avais sauvegardé sur les outils pour économiser des tokens ?",
            "Retrouve ce que j'avais trouvé sur la prospection LinkedIn",
            "Tu te souviens de ce que j'avais gardé sur RTK ?",
            "Qu'est-ce que j'ai vu sur le TDAH dans mes vidéos ?",
            "Retrouve mes vidéos sur la cybersécurité",
            "Qu'est-ce que j'avais enregistré à propos de Claude Code ?",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.route(phrase), "ask_life_intelligence")

    def test_explicit_vault_goes_to_agents(self):
        self.assertEqual(
            self.route("Consulte mon Vault sur le TDAH"), "consulter_agents_vault",
        )
        self.assertEqual(
            self.route("Demande au Knowledge Graph Engineer ce qu'il en pense"),
            "consulter_agents_vault",
        )

    def test_out_of_vault_is_not_captured(self):
        for phrase in (
            "Quel temps fait-il demain à Paris ?",
            "Allume la lumière du salon",
            "J'ai trouvé mes clés, merci",
            "J'ai gardé le reçu du restaurant",
            "J'ai trouvé une source d'eau dans le jardin",
            "Qu'est-ce que j'ai trouvé comme restaurant hier ?",
            "Mets une vidéo YouTube de musique relaxante",
            "Est-ce que j'ai des mails non lus ?",
            "",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNone(self.route(phrase))

    def test_command_uses_retrieval_first_bridge(self):
        with mock.patch.object(tool, "_vault_root", return_value=RACINE):
            if not (RACINE / ".venv" / "Scripts" / "python.exe").exists():
                self.skipTest("venv Windows du Vault absent")
            cmd = tool._commande("question", "", "consult")
        self.assertIn("--ask", cmd)
        self.assertEqual(cmd[cmd.index("--mode") + 1], "consult")
        self.assertNotIn("--prompt", cmd)

    def test_bridge_failure_is_reported_without_exception(self):
        fake = subprocess.CompletedProcess([], 1, stdout="", stderr="Traceback\nBoom")
        with mock.patch.object(tool, "_commande", return_value=["x"]), \
                mock.patch.object(tool.subprocess, "run", return_value=fake):
            self.assertEqual(
                tool.ask_life_intelligence("Qu'est-ce que j'avais sauvegardé sur X ?"),
                "Life Intelligence a echoue : Boom",
            )

    def test_bridge_timeout_is_reported(self):
        with mock.patch.object(tool, "_commande", return_value=["x"]), \
                mock.patch.object(
                    tool.subprocess, "run",
                    side_effect=subprocess.TimeoutExpired("x", 1),
                ):
            self.assertIn("trop de temps", tool.ask_life_intelligence("sur X ?"))


if __name__ == "__main__":
    unittest.main()
