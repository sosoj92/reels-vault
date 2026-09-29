#!/usr/bin/env python3
"""
vault_transcribe.py - Transcription locale des vocaux (faster-whisper, CPU).

Reutilise le modele Whisper deja present dans le cache local (le meme moteur
que l'ingestion et Jarvis). Aucun telechargement, aucun cloud :
local_files_only=True. Modele : VAULT_WHISPER_MODEL (defaut « small »).
"""

import os
import tempfile
from pathlib import Path

MODEL_NAME = os.environ.get("VAULT_WHISPER_MODEL", "small")
_MODEL = None


class TranscriptionError(RuntimeError):
    pass


def _model():
    global _MODEL
    if _MODEL is None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriptionError("faster-whisper absent") from exc
        try:
            _MODEL = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8",
                                  local_files_only=True)
        except Exception as exc:  # noqa: BLE001 - modele absent du cache
            raise TranscriptionError(f"modele Whisper '{MODEL_NAME}' indisponible localement") \
                from exc
    return _MODEL


def transcribe_bytes(data, suffix=".oga", language="fr"):
    """Audio (ogg/opus Telegram, mp3, wav...) -> texte. Fichier temporaire supprime."""
    handle, name = tempfile.mkstemp(suffix=suffix, prefix="vault-voice-")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        segments, _info = _model().transcribe(name, language=language, vad_filter=True,
                                              beam_size=1)
        text = " ".join(segment.text.strip() for segment in segments).strip()
    finally:
        Path(name).unlink(missing_ok=True)
    if not text:
        raise TranscriptionError("vocal vide ou inaudible")
    return text
