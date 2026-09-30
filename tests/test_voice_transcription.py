import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "voice_sourcing.oga"
ROOT = Path(__file__).resolve().parent.parent

# Transcription reelle executee dans un processus dedie : Whisper (plusieurs centaines de Mo
# de memoire native) est entierement rendu au systeme a la fin du processus, au lieu de
# rester charge pour le reste de la suite.
WORKER = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import vault_transcribe
out = {}
out["text"] = vault_transcribe.transcribe_bytes(open(sys.argv[2], "rb").read())
try:
    vault_transcribe.transcribe_bytes(b"pas un fichier audio")
    out["garbage"] = "accepted"
except Exception as exc:
    out["garbage"] = type(exc).__name__
print(json.dumps(out, ensure_ascii=False))
"""


def _model_cached():
    """Presence du modele dans le cache local, SANS le charger en memoire."""
    try:
        from faster_whisper.utils import download_model

        download_model(os.environ.get("VAULT_WHISPER_MODEL", "small"), local_files_only=True)
        return True
    except Exception:  # noqa: BLE001 - modele absent : test ignore
        return False


@unittest.skipUnless(_model_cached(), "modele Whisper local absent du cache")
class RealTranscriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = dict(os.environ, HF_HUB_OFFLINE="1", PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, "-c", WORKER, str(ROOT), str(FIXTURE)],
                                capture_output=True, text=True, timeout=300, env=env,
                                encoding="utf-8")
        cls.returncode, cls.stderr = result.returncode, result.stderr[-2000:]
        lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
        cls.output = json.loads(lines[-1]) if lines else {}

    def test_telegram_ogg_opus_voice_is_transcribed_locally(self):
        # Vocal synthetique (voix Windows) au format des vocaux Telegram (OGG/Opus).
        self.assertEqual(self.returncode, 0, self.stderr)
        folded = self.output["text"].lower()
        self.assertIn("pour moi", folded)
        self.assertIn("sourcing", folded)

    def test_silence_is_rejected(self):
        self.assertEqual(self.returncode, 0, self.stderr)
        self.assertNotEqual(self.output["garbage"], "accepted")


if __name__ == "__main__":
    unittest.main()
