"""
Places outbound calls through the Zernio voice API.

This is the whole telephony client for now: one POST to /api/v1/voice/calls
that dials the patient's number and tells Zernio to forward the live call
audio to our /media-stream WebSocket (exposed through ngrok).

Security notes:
- The Zernio API key stays in `.env` and is only sent as a Bearer header.
- Each call carries a fresh random token on the forwardTo URL which
  /media-stream requires before it accepts the connection.
- Destination numbers must be E.164 and start with one of the configured
  ALLOWED_CALL_PREFIXES, so even a leaked admin key can't dial arbitrary
  international numbers.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import requests

from app.core import media_auth
from app.core.config import get_settings

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger("voicecare.outbound")

ZERNIO_CALLS_ENDPOINT = "https://zernio.com/api/v1/voice/calls"
_ZERNIO_TIMEOUT_SECONDS = 15

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


class OutboundCallError(RuntimeError):
    """Base error for the call placement flow."""


class ConfigError(OutboundCallError):
    """A required .env value is missing or malformed."""


class InvalidNumberError(OutboundCallError):
    """Destination number failed validation."""


@dataclass
class OutboundCallResult:
    to_number: str
    provider_call_id: str | None
    greeting: str
    # Full forwardTo URL (contains the one-time token) -- never returned to clients.
    forward_to: str = field(repr=False)
    provider_status: str | None = None       # e.g. "dialing" at creation time
    telnyx_call_control_id: str | None = None
    provider_response: dict = field(default_factory=dict, repr=False)

    @property
    def forward_to_host(self) -> str:
        """Host + path only, safe to log or return in API responses."""
        without_query = self.forward_to.split("?", 1)[0]
        return without_query.replace("wss://", "")


def _validate_number(raw_number: str, allowed_prefixes: list[str]) -> str:
    number = raw_number.strip().replace(" ", "").replace("-", "")
    if not _E164_RE.match(number):
        raise InvalidNumberError(
            f"'{raw_number}' is not a valid E.164 number (expected e.g. +94771234567)."
        )
    if allowed_prefixes and not number.startswith(tuple(allowed_prefixes)):
        raise InvalidNumberError(
            "Number prefix not allowed (ALLOWED_CALL_PREFIXES="
            + ",".join(allowed_prefixes)
            + ")."
        )
    return number


def _require(value: str, env_var: str, hint: str) -> str:
    if not value:
        raise ConfigError(f"{env_var} is not set in backend/.env. {hint}")
    return value


def place_call(
    to_number: str,
    settings: "Settings | None" = None,
    config: dict | None = None,
    amd: bool | None = None,
) -> OutboundCallResult:
    """Validate the number, reserve a stream token (with optional call
    config such as diagnosis_category), and ask Zernio to dial.

    amd: None = use the CALL_AMD env default; True/False overrides per call
    (answering-machine detection -- see config.py for the trade-off).
    """
    settings = settings or get_settings()

    number = _validate_number(to_number, settings.allowed_prefixes)
    _require(settings.zernio_api_key, "ZERNIO_API_KEY", "Get it from the Zernio dashboard.")
    from_number = _require(
        settings.from_number, "FROM_NUMBER", "The number Zernio calls from, e.g. +1888..."
    )
    public_wss = _require(
        settings.public_wss_url,
        "PUBLIC_WSS_URL",
        "Run ngrok and set the wss://.../media-stream URL it prints.",
    )
    if not public_wss.startswith("wss://"):
        raise ConfigError("PUBLIC_WSS_URL must start with wss://")
    if not public_wss.rstrip("/").endswith("/media-stream"):
        raise ConfigError("PUBLIC_WSS_URL must end with /media-stream")

    token = media_auth.issue_token(config=config)
    forward_to = f"{public_wss}?token={token}"
    greeting = settings.greeting

    payload = {
        "to": number,
        "fromNumber": from_number,
        "forwardTo": forward_to,
        "greeting": greeting,
    }
    use_amd = settings.call_amd if amd is None else amd
    if use_amd:
        # Answering-machine detection: Zernio defers the bridge until it
        # knows a human picked up, so we never run our dialogue into a
        # voicemail box (seen live in testing). NOTE: it can also block the
        # bridge entirely when the callee stays silent after answering --
        # that's why the default is off; test per call with "amd": true.
        payload["amd"] = True

    logger.info(
        "Placing call to %s from %s via %s (greeting %d chars, amd=%s)",
        number,
        from_number,
        public_wss.split("?")[0],
        len(greeting),
        use_amd,
    )
    started = time.monotonic()
    try:
        response = requests.post(
            ZERNIO_CALLS_ENDPOINT,
            headers={
                "Authorization": f"Bearer {settings.zernio_api_key}",
                "Content-Type": "application/json",
                # Docs: same key + same body replays the original response
                # instead of dialing (and billing) a second call on retry.
                "Idempotency-Key": str(uuid.uuid4()),
            },
            json=payload,
            timeout=_ZERNIO_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        media_auth.revoke_token(token)
        raise OutboundCallError(f"Could not reach Zernio: {exc}") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:500]}

    if response.status_code != 200 or not body.get("success"):
        media_auth.revoke_token(token)
        raise OutboundCallError(
            f"Zernio rejected the call (HTTP {response.status_code}): {body}"
        )

    provider_call_id = next(
        (body[key] for key in ("callId", "call_control_id", "callControlId", "id") if body.get(key)),
        None,
    )
    logger.info(
        "Call dialing: to=%s provider_call_id=%s status=%r zernio_ms=%d stream=%s zernio_response=%s",
        number,
        provider_call_id,
        body.get("status"),
        elapsed_ms,
        f"{public_wss}?token=***",
        body,
    )
    return OutboundCallResult(
        to_number=number,
        provider_call_id=provider_call_id,
        greeting=greeting,
        forward_to=forward_to,
        provider_status=body.get("status"),
        telnyx_call_control_id=body.get("telnyxCallControlId"),
        provider_response=body,
    )


def get_call_status(call_id: str) -> dict:
    """Ask Zernio for the call's lifecycle state (docs: the 200 from POST
    /calls only means 'dialing'; track via GET /v1/voice/calls/{id})."""
    settings = get_settings()
    _require(settings.zernio_api_key, "ZERNIO_API_KEY", "Get it from the Zernio dashboard.")
    try:
        response = requests.get(
            f"{ZERNIO_CALLS_ENDPOINT}/{call_id}",
            headers={"Authorization": f"Bearer {settings.zernio_api_key}"},
            timeout=_ZERNIO_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise OutboundCallError(f"Could not reach Zernio: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:500]}
    if response.status_code != 200 or not body.get("success"):
        raise OutboundCallError(
            f"Zernio status lookup failed (HTTP {response.status_code}): {body}"
        )
    logger.info("Call %s status: %r", call_id, body.get("status"))
    return body

