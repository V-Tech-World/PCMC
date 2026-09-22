"""Manual 'call now' endpoint (POST /calls)."""

from __future__ import annotations

import logging
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.config import get_settings
from app.core import rate_limiter
from app.core.security import AuthContext, require_roles
from app.services import outbound_call
from app.services.outbound_call import ConfigError, InvalidNumberError, OutboundCallError

logger = logging.getLogger("voicecare.calls")

router = APIRouter()

_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class TriggerCallRequest(BaseModel):
    """Body of POST /calls. Omit phone_number to dial TEST_TO_NUMBER, or pass
    patient_code to dial that patient's registered number."""

    phone_number: str | None = None
    patient_code: str | None = None   # links the call to the patient record
    diagnosis_category: str = "general"  # general | surgical | cardiac
    amd: bool | None = None  # per-call AMD override; None = use CALL_AMD env


@router.post("/calls", status_code=201)
def trigger_call(
    body: TriggerCallRequest,
    context: AuthContext = Depends(require_roles("nurse", "doctor", "admin")),
) -> dict:
    """Manual 'call now' trigger (dashboard button or API key). The dialogue
    runs over /media-stream."""
    settings = get_settings()

    # Validate the category before dialing (fail fast, no call is placed).
    try:
        from app.services.dialogue import build_script

        build_script(body.diagnosis_category)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Cost rails (Step 5): reject the dial BEFORE touching the provider so a
    # refused request costs nothing. 429 with a Retry-After hint.
    try:
        rate_limiter.check_can_dial()
        rate_limiter.check_stream_slot()
    except rate_limiter.RateLimitExceeded as exc:
        headers = (
            {"Retry-After": str(max(1, int(exc.retry_after_sec)))}
            if exc.retry_after_sec
            else None
        )
        raise HTTPException(
            status_code=429, detail=exc.reason, headers=headers
        ) from exc

    # Step 6: link the call to a patient record. The patient_code (and the
    # dialed number) ride the one-time stream token into /media-stream, where
    # the finished call row is created (and the Step 7 alert references them).
    patient = None
    if body.patient_code:
        from app.db import service as db_service

        patient = db_service.find_patient(patient_code=body.patient_code)
        if patient is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown patient_code {body.patient_code!r} -- seed it via POST /records/patients.",
            )

    # Number resolution AFTER the patient lookup (so a patient_code-only dial
    # -- the dashboard's Call Now button -- never 422s before the patient is
    # read). Priority: explicit phone_number > patient's registered number >
    # TEST_TO_NUMBER. The "nothing to dial" check comes last.
    number = body.phone_number or settings.test_to_number
    if not body.phone_number and patient is not None and patient.phone_number:
        number = patient.phone_number
    if not number:
        raise HTTPException(
            status_code=422,
            detail="Provide 'phone_number' in the body or set TEST_TO_NUMBER in .env.",
        )

    try:
        result = outbound_call.place_call(
            number,
            config={
                "diagnosis_category": body.diagnosis_category,
                "to_number": number,
                "patient_code": body.patient_code or "",
            },
            amd=body.amd,
        )
    except InvalidNumberError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OutboundCallError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # The dial succeeded -- count it against the hourly/daily budgets.
    rate_limiter.record_dial()

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
    context: AuthContext = Depends(require_roles("nurse", "doctor", "admin")),
) -> dict:
    """Provider-side call lifecycle (dialing/answered/ended/failed + duration).

    Use this after POST /calls when the phone didn't ring: it shows what
    Zernio actually saw on the call leg."""
    if not _CALL_ID_RE.match(call_id):
        raise HTTPException(status_code=422, detail="Invalid call id format.")
    try:
        return outbound_call.get_call_status(call_id)
    except OutboundCallError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


