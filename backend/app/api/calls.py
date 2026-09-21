"""Manual 'call now' endpoint (POST /calls)."""

from __future__ import annotations

import logging
import re
import secrets

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.core.config import get_settings
from app.services import outbound_call
from app.services.outbound_call import ConfigError, InvalidNumberError, OutboundCallError

logger = logging.getLogger("voicecare.calls")

router = APIRouter()

_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class TriggerCallRequest(BaseModel):
    """Body of POST /calls. Omit phone_number to dial TEST_TO_NUMBER."""

    phone_number: str | None = None
    diagnosis_category: str = "general"  # general | surgical | cardiac
    amd: bool | None = None  # per-call AMD override; None = use CALL_AMD env


@router.post("/calls", status_code=201)
def trigger_call(
    body: TriggerCallRequest,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> dict:
    """Manual 'call now' trigger. The dialogue runs over /media-stream."""
    settings = get_settings()

    # Validate the category before dialing (fail fast, no call is placed).
    try:
        from app.services.dialogue import build_script

        build_script(body.diagnosis_category)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    _require_api_key(x_api_key)

    number = body.phone_number or settings.test_to_number
    if not number:
        raise HTTPException(
            status_code=422,
            detail="Provide 'phone_number' in the body or set TEST_TO_NUMBER in .env.",
        )

    try:
        result = outbound_call.place_call(
            number,
            config={"diagnosis_category": body.diagnosis_category},
            amd=body.amd,
        )
    except InvalidNumberError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OutboundCallError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # The category rides on the one-time stream token (see outbound_call),
    # so the WS handler knows which script to run when Zernio connects.
    return {
        "status": "dialing",
        "to": result.to_number,
        "provider_call_id": result.provider_call_id,
        "provider_status": result.provider_status,
        "stream": result.forward_to_host,
        "greeting": result.greeting,
        "diagnosis_category": body.diagnosis_category,
        # Note: 'dialing' only means Zernio started dialing. The leg may
        # still go unanswered -- check GET /calls/{provider_call_id}/status.
    }


def _require_api_key(x_api_key: str | None) -> None:
    settings = get_settings()
    if not settings.calls_api_key:
        raise HTTPException(
            status_code=503,
            detail="CALLS_API_KEY is not configured in backend/.env -- dialing is disabled.",
        )
    if not x_api_key or not secrets.compare_digest(
        x_api_key.encode("utf-8"), settings.calls_api_key.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Missing or invalid X-Api-Key header.")


@router.get("/calls/{call_id}/status")
def call_status(
    call_id: str,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> dict:
    """Provider-side call lifecycle (dialing/answered/ended/failed + duration).

    Use this after POST /calls when the phone didn't ring: it shows what
    Zernio actually saw on the call leg."""
    _require_api_key(x_api_key)
    if not _CALL_ID_RE.match(call_id):
        raise HTTPException(status_code=422, detail="Invalid call id format.")
    try:
        return outbound_call.get_call_status(call_id)
    except OutboundCallError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


