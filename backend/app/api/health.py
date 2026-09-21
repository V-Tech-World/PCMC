"""Health and meta endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from app.core import media_auth
from app.core.config import get_settings

router = APIRouter()

API_VERSION = "0.2.0"


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "service": "voicecare-backend",
        "version": API_VERSION,
        "utc_now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "telephony_configured": bool(
            settings.zernio_api_key and settings.from_number and settings.public_wss_url
        ),
        "stt_model": settings.stt_model,
        "active_stream_tokens": media_auth.active_token_count(),
    }
