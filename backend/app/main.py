"""
VoiceCare LK backend -- FastAPI application entrypoint.

Step 2 (batch transcription proof) scope on top of Step 1:
- GET  /health        : liveness + config sanity (no secrets exposed)
- POST /calls         : manual "call now" trigger -> places a real call via Zernio
- WS   /media-stream  : live call audio from Zernio, saved as WAV per call
- app/services/stt.py : faster-whisper wrapper + transcribe_call.py CLI

Run:
    .venv/Scripts/python -m uvicorn app.main:app --port 8000
or: .venv/Scripts/python -m app.main
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.api import calls, health
from app.api.media_stream import router as stream_router
from app.core.config import get_settings
from app.core.logging_setup import setup_logging
from app.core.timing import (
    acquire_fine_timer,
    release_fine_timer,
    sleep_resolution_warning,
)


def _mask(secret: str) -> str:
    """Safe representation of a secret for startup logs."""
    if not secret:
        return "<not set>"
    return f"{secret[:6]}...({len(secret)} chars)"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    log = logging.getLogger("voicecare.main")
    log.info("VoiceCare LK backend starting (v%s)", health.API_VERSION)
    # Real-time audio pacing needs a fine OS timer, otherwise our 20 ms frames
    # drift (~31 ms each on Windows) and the patient hears stretched audio.
    acquire_fine_timer()
    log.info("  Sleep/pacing timer: %s", sleep_resolution_warning())
    log.info("  ZERNIO_API_KEY: %s", _mask(settings.zernio_api_key))
    log.info("  CALLS_API_KEY:  %s", _mask(settings.calls_api_key))
    log.info("  FROM_NUMBER:    %s", settings.from_number or "<not set>")
    log.info("  PUBLIC_WSS_URL: %s", (settings.public_wss_url.split("?")[0]) or "<not set>")
    log.info("  Allowed dial prefixes: %s", settings.allowed_prefixes or "<none>")
    log.info("  Media-stream token required: %s", settings.media_stream_require_token)
    log.info(
        "  Audio we send: codec=%s voice=%r bandpass=%s filler=%s",
        settings.speak_codec,
        settings.tts_voice or "<system default>",
        settings.tts_bandpass_enabled,
        settings.filler_enabled,
    )
    log.info(
        "  STT model: %s (device=%s, compute=%s)",
        settings.stt_model,
        settings.stt_device,
        settings.stt_compute_type,
    )
    if not settings.calls_api_key:
        log.warning("CALLS_API_KEY is empty -- POST /calls will refuse to dial (set it in .env)")
    yield
    release_fine_timer()
    log.info("VoiceCare LK backend stopped")


app = FastAPI(title="VoiceCare LK API", version=health.API_VERSION, lifespan=lifespan)
app.include_router(health.router)
app.include_router(calls.router)
app.include_router(stream_router)

if __name__ == "__main__":
    settings = get_settings()
    setup_logging(settings.log_level)
    uvicorn.run(
        "app.main:app",
        host=settings.backend_host,
        port=settings.backend_port,
        log_config=None,
    )
