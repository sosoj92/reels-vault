import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import unittest
from pathlib import Path

import vault_transcribe

FIXTURE = Path(__file__).parent / "fixtures" / "voice_sourcing.oga"


def _model_cached():
    try:
        vault_transcribe._model()
        return True
    except vault_transcribe.TranscriptionError:
        return False


@unittest.skipUnless(_model_cached(), "modele Whisper local absent du cache")
class RealTranscriptionTests(unittest.TestCase):
    def test_telegram_ogg_opus_voice_is_transcribed_locally(self):
        # Vocal synthetique (voix Windows) au format des vocaux Telegram (OGG/Opus).
        text = vault_transcribe.transcribe_bytes(FIXTURE.read_bytes())
        folded = text.lower()
        self.assertIn("pour moi", folded)
        self.assertIn("sourcing", folded)

    def test_silence_is_rejected(self):
        with self.assertRaises(Exception):
            vault_transcribe.transcribe_bytes(b"pas un fichier audio")


if __name__ == "__main__":
    unittest.main()
