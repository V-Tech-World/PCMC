"""Health and meta endpoints."""

from __future__ import annotations

import math
import struct
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from app.core import media_auth
from app.core.config import get_settings

router = APIRouter()

API_VERSION = "0.2.0"


def verify_stt_pipeline() -> tuple[bool, str]:
    """Transcribe a synthetic 1 s WAV to prove the STT stack actually runs.

    Worth its ~2 s of startup: a version clash between faster-whisper and PyAV
    only surfaces on the first real answer (live 4 Oct 2026, av 19 removed the
    `metadata_errors` kwarg), which is far too late -- the patient is already on
    the line and the flow has crashed. Better to fail loudly at boot.
    Only runs the decode/infer path if the model is already warm, so a fresh
    install doesn't turn a health check into a model download.
    """
    path = Path(tempfile.gettempdir()) / "voicecare_stt_selftest.wav"
    try:
        from app.services.stt import get_transcriber

        transcriber = get_transcriber()
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            # A quiet 440 Hz tone: we only care that decode+infer runs, not the text.
            wav.writeframes(
                struct.pack(
                    "<%dh" % 8000,
                    *[int(3000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(8000)],
                )
            )
        transcriber.transcribe(path)
    except Exception as exc:  # noqa: BLE001 - we want the message, whatever it is
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        path.unlink(missing_ok=True)
    return True, "ok"


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    from app.services import scheduler

    stt_ok, stt_error = verify_stt_pipeline()

    return {
        "status": "ok",
        "service": "voicecare-backend",
        "version": API_VERSION,
        "utc_now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "telephony_configured": bool(
            settings.zernio_api_key and settings.from_number and settings.public_wss_url
        ),
        "stt_model": settings.stt_model,
        # Not a cached flag: cheap enough to actually run, and it is the only
        # place a broken faster-whisper/PyAV pair shows up before a live call.
        "stt_pipeline_ok": stt_ok,
        "stt_pipeline_error": stt_error,
        "active_stream_tokens": media_auth.active_token_count(),
        "alerts_configured": bool(
            settings.alerts_enabled and settings.alert_conversation_id
        ),
        "auth_configured": bool(settings.signing_secret),
        "automatic_calls_enabled": bool(settings.schedule_calls_enabled),
        "scheduler_running": scheduler.status(settings)["running"],
        "cors_origins": settings.cors_origins,
    }
