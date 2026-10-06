"""
VoiceCare backend -- FastAPI application entrypoint.

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
from app.services import alerts as alerts_service
from app.services import email_alerts as email_alerts_service
from app.api.health import verify_stt_pipeline


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
    log.info("VoiceCare backend starting (v%s)", health.API_VERSION)
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
    if settings.stt_verify_on_start:
        # A broken STT stack used to be discovered by the first patient who
        # answered, mid-call, with the flow already crashed (live 4 Oct 2026).
        stt_ok, stt_detail = verify_stt_pipeline()
        if stt_ok:
            log.info("  STT self-test: OK (model loaded and inference ran)")
        else:
            log.error(
                "  STT self-test FAILED: %s -- calls will crash on the first "
                "answer. Check the faster-whisper / PyAV versions (requirements.txt "
                "pins av<14) before dialling anyone.",
                stt_detail,
            )
    if not settings.calls_api_key:
        log.warning("CALLS_API_KEY is empty -- POST /calls will refuse to dial (set it in .env)")
    # Step 6: make sure the schema exists before anything runs.
    init_db(settings.database_url)
    log.info("  Database: %s", (settings.database_url or "sqlite (default voicecare.db)"))
    log.info(
        "  Alerts: %s (delivery=%s, conversation=%s)",
        "enabled" if settings.alerts_enabled else "disabled",
        alerts_service.delivery_mode(settings),
        settings.alert_conversation_id or "<not configured -- run get-info.py>",
    )
    if settings.alerts_enabled and alerts_service.delivery_mode(
        settings
    ) == alerts_service.DELIVERY_WHATSAPP and not alerts_service.alerts_configured(
        settings
    ):
        log.warning(
            "  ALERT_DELIVERY=whatsapp but the conversation is not configured -- "
            "HIGH-risk messages will be stored on the call row, not sent"
        )

    # Step 9: seed the first dashboard admin so the demo has a login.
    seeded = db_service.ensure_default_admin(
        username=settings.admin_username,
        password=settings.admin_password,
        display_name=settings.admin_display_name,
        hospital=settings.default_hospital,
        email=settings.admin_email,
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

    # Staff email alerts (4 Oct 2026). Two things happen here, once, at boot:
    #   1. report whether the mailbox is usable, because a silently
    #      unconfigured sender is exactly how an escalation goes missing;
    #   2. drop accounts that can never be alerted (no email address), keeping
    #      super-admins -- they are never assigned to a patient.
    if settings.email_alerts_enabled and email_alerts_service.email_alerts_configured(
        settings
    ):
        log.info(
            "  Staff email alerts: ON (from=%s, routed per patient's assigned care team)",
            settings.sender_email,
        )
    else:
        log.warning(
            "  Staff email alerts: NOT SENDING -- set SENDER_EMAIL and "
            "GOOGLE_APP_PASSWORD in .env (alerts are still stored on the call row)"
        )
    removed_staff = db_service.purge_staff_without_email()
    if removed_staff:
        log.warning(
            "  Removed %d staff account(s) with no email address: %s",
            len(removed_staff),
            ", ".join(removed_staff),
        )

    # Step 8: the follow-up cron. The master switch defaults to
    # SCHEDULE_CALLS_ENABLED in .env, but an admin can flip it from the
    # dashboard (POST /schedule/enabled) -- that choice is persisted in the DB
    # and re-applied here. This is the only place the app can dial without a
    # human pressing a button.
    scheduler.apply_saved_switch(settings)
    scheduler.start(settings)
    yield
    scheduler.stop()
    release_fine_timer()
    log.info("VoiceCare backend stopped")


app = FastAPI(title="VoiceCare API", version=health.API_VERSION, lifespan=lifespan)

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
