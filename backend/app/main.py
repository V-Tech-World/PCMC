"""
VoiceCare LK backend -- FastAPI application entrypoint.

Endpoints (Steps 1-9):
- GET  /health              : liveness + config sanity (no secrets exposed)
- POST /calls               : manual "call now" -> places a real call via Zernio
- WS   /media-stream        : live call audio from Zernio, saved as WAV per call
- GET/PATCH /records/*      : persisted calls, patients, review workflow
- POST /auth/login          : staff login (JWT) for the dashboard
- GET  /schedule/*          : follow-up check-in plan (+ manual tick)
- GET  /dashboard/summary   : one payload for the dashboard landing screen

Run:
    .venv/Scripts/python -m uvicorn app.main:app --port 8000
or: .venv/Scripts/python -m app.main
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth as auth_api
from app.api import calls, health
from app.api import dashboard as dashboard_api
from app.api import records as records_api
from app.api import schedule as schedule_api
from app.api.media_stream import router as stream_router
from app.core.config import get_settings
from app.core.logging_setup import setup_logging
from app.core.timing import (
    acquire_fine_timer,
    release_fine_timer,
    sleep_resolution_warning,
)
from app.db import service as db_service
from app.db.engine import init_db
from app.services import scheduler


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
    # Step 6: make sure the schema exists before anything runs.
    init_db(settings.database_url)
    log.info("  Database: %s", (settings.database_url or "sqlite (default voicecare.db)"))
    log.info(
        "  WhatsApp alerts: %s (sandbox conversation %s)",
        "enabled" if settings.alerts_enabled else "disabled",
        settings.alert_conversation_id or "<not configured>",
    )

    # Step 9: seed the first dashboard admin so the demo has a login.
    seeded = db_service.ensure_default_admin(
        username=settings.admin_username,
        password=settings.admin_password,
        display_name=settings.admin_display_name,
        hospital=settings.default_hospital,
    )
    if seeded == "created":
        log.info("  Dashboard login seeded: %s (role=admin)", settings.admin_username)
    elif seeded == "skipped":
        log.warning(
            "  No dashboard login seeded: set ADMIN_USERNAME/ADMIN_PASSWORD in .env "
            "(or POST /auth/staff with an API key)."
        )
    else:
        log.info("  Dashboard logins already present.")

    # Step 8: the follow-up cron. Dormant unless SCHEDULE_CALLS_ENABLED=true --
    # this is the only place the app can dial without a human pressing a button.
    scheduler.start(settings)
    yield
    scheduler.stop()
    release_fine_timer()
    log.info("VoiceCare LK backend stopped")


app = FastAPI(title="VoiceCare LK API", version=health.API_VERSION, lifespan=lifespan)

# Step 9: the dashboard runs on Vite (5173 by default) -- allow those origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth_api.router)
app.include_router(calls.router)
app.include_router(records_api.router)
app.include_router(schedule_api.router)
app.include_router(dashboard_api.router)
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
