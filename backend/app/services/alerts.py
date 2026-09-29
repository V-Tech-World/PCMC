"""
Alerts (Step 7, reworked 29 Sep 2026): HIGH-risk calls get an alert message
PREPARED; whether it is also delivered is a config choice.

Why it changed: WhatsApp delivery rode on the Zernio inbox sandbox thread,
which is a test surface, not a hospital channel -- and the demo must not depend
on a message actually leaving the backend. So the call flow now always builds
the full alert text for a HIGH-risk call and stores it on the call row, where
the dashboard shows it (call detail -> "Alert message" + Copy button):

    ALERT_DELIVERY=ready     (default)  prepare + store, send nothing
    ALERT_DELIVERY=whatsapp             also POST it to the sandbox thread

The WhatsApp transport (unchanged from the original Step 7) is:

    POST https://zernio.com/api/v1/inbox/conversations/{conversation_id}/messages
    Authorization: Bearer <ZERNIO_API_KEY>
    {"accountId": "<inbox account id>", "message": "<text>"}

The sandbox credentials live in their own env vars
(ZERNIO_INBOX_ACCOUNT_ID / ZERNIO_ALERT_CONVERSATION_ID) and never touch the
real toll-free FROM_NUMBER: voice calls keep dialing from the toll-free line.
If the transport is selected but unconfigured, the alert is still prepared and
stored, and the row records why it was not sent.

Per the README TCs: only HIGH risk produces an alert, and the message carries
the patient code, risk level, and key symptoms (plus the full answers and
transcripts, so the on-call nurse sees everything without opening the app).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from app.core.config import Settings, get_settings

logger = logging.getLogger("voicecare.alerts")

ZERNIO_INBOX_MESSAGES_ENDPOINT = "https://zernio.com/api/v1/inbox/conversations"
_ALERT_TIMEOUT_SECONDS = 15

#: Only HIGH risk produces an alert (Step 7 TC2: low risk stays silent).
ALERT_RISK_LEVEL = "high"

#: Delivery modes (ALERT_DELIVERY).
DELIVERY_READY = "ready"        # build + store the message, send nothing
DELIVERY_WHATSAPP = "whatsapp"  # build + store + POST to the sandbox thread

#: alert_status values written to the call row.
STATUS_SKIPPED = "skipped"              # not a HIGH-risk call
STATUS_READY = "ready"                  # message prepared, waiting for a human/channel
STATUS_NOT_CONFIGURED = "not_configured"  # transport selected but unusable


class AlertError(RuntimeError):
    """The WhatsApp alert could not be delivered."""


@dataclass(frozen=True)
class AlertOutcome:
    """What happened to one call's alert, ready to store on the row."""

    status: str
    detail: str = ""
    message: str = ""


def alerts_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.alert_conversation_id and settings.inbox_account_id)


def format_alert_message(record, patient=None) -> str:
    """Human-readable WhatsApp text with every detail of the call."""
    answers = record.get_answers() if hasattr(record, "get_answers") else []
    findings = record.get_findings() if hasattr(record, "get_findings") else []

    lines: list[str] = [
        "\U0001F6A8 URGENT post-discharge check-in alert",
        f"Patient code: {record.patient_code or 'UNKNOWN'}",
    ]
    if patient is not None and patient.name:
        lines.append(f"Patient name: {patient.name}")
    if record.phone_number:
        lines.append(f"Phone: {record.phone_number}")
    lines.append(f"Diagnosis category: {record.diagnosis_category}")
    lines.append(f"Risk: {record.risk_level.upper()} (score {record.risk_score})")
    lines.append(f"Call ended: {record.ended_reason or 'finished'}")

    if findings:
        lines.append("Key symptoms:")
        for f in findings:
            severity = f.get("severity") or "unspecified"
            flag = " [RED FLAG]" if f.get("red_flag") else ""
            lines.append(f"  - {f.get('label', f.get('id'))} ({severity}){flag}")
    else:
        lines.append("Key symptoms: none detected")

    if answers:
        lines.append("Answers:")
        for a in answers:
            interpretation = a.get("interpretation")
            interp = str(interpretation) if interpretation is not None else "unclear"
            transcript = (a.get("transcript") or "").strip() or "(no speech)"
            lines.append(
                f"  - {a.get('question_id')}: {interp} -- \"{transcript}\""
            )

    if record.provider_call_id:
        lines.append(f"Provider call id: {record.provider_call_id}")
    finished = record.finished_at or record.created_at
    lines.append(f"Recorded at: {finished.isoformat()}")
    return "\n".join(lines)


def send_whatsapp_alert(
    message: str,
    settings: Settings | None = None,
) -> str:
    """Send one WhatsApp text through the sandbox conversation.

    Returns the provider message id. Raises AlertError on failure.
    """
    settings = settings or get_settings()
    if not settings.alerts_enabled:
        raise AlertError("ALERTS_ENABLED is false -- alerts are switched off")
    if not settings.alert_conversation_id or not settings.inbox_account_id:
        raise AlertError(
            "Sandbox WhatsApp is not configured (set ZERNIO_ALERT_CONVERSATION_ID "
            "and ZERNIO_INBOX_ACCOUNT_ID in backend/.env)"
        )
    if not settings.zernio_api_key:
        raise AlertError("ZERNIO_API_KEY is not set -- cannot authenticate alerts")

    url = (
        f"{ZERNIO_INBOX_MESSAGES_ENDPOINT}/{settings.alert_conversation_id}/messages"
    )
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {settings.zernio_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "accountId": settings.inbox_account_id,
            "message": message,
        },
        timeout=_ALERT_TIMEOUT_SECONDS,
    )
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:300]}

    if response.status_code >= 400:
        raise AlertError(
            f"Zernio rejected the WhatsApp alert (HTTP {response.status_code}): {body}"
        )
    message_id = body.get("id") or body.get("messageId") or "unknown"
    logger.info("WhatsApp alert delivered: id=%s", message_id)
    return str(message_id)


def delivery_mode(settings: Settings | None = None) -> str:
    """Normalised ALERT_DELIVERY value ('ready' | 'whatsapp')."""
    settings = settings or get_settings()
    mode = (settings.alert_delivery or DELIVERY_READY).strip().lower()
    if mode not in (DELIVERY_READY, DELIVERY_WHATSAPP):
        logger.warning(
            "Unknown ALERT_DELIVERY=%r -- falling back to %r (prepare only)",
            settings.alert_delivery, DELIVERY_READY,
        )
        return DELIVERY_READY
    return mode


def prepare_alert(
    record,
    patient=None,
    settings: Settings | None = None,
) -> AlertOutcome:
    """Build the alert for one finished call and (optionally) deliver it.

    Always runs at the end of a call:
    - not HIGH risk          -> status 'skipped', nothing prepared (Step 7 TC2)
    - HIGH + ready (default) -> status 'ready', full message stored on the row
    - HIGH + whatsapp        -> status 'sent (<id>)' / 'failed: ...' /
                                'not_configured', message stored either way
    """
    settings = settings or get_settings()

    if record.risk_level != ALERT_RISK_LEVEL:
        logger.info(
            "No alert: risk=%s (only %s gets one)", record.risk_level,
            ALERT_RISK_LEVEL,
        )
        return AlertOutcome(STATUS_SKIPPED, f"risk={record.risk_level}")

    message = format_alert_message(record, patient)

    if delivery_mode(settings) == DELIVERY_READY:
        logger.info(
            "Alert PREPARED for record id=%s (%d chars) -- delivery is 'ready'",
            record.id, len(message),
        )
        return AlertOutcome(
            STATUS_READY,
            "prepared; ALERT_DELIVERY=ready so nothing was sent",
            message,
        )

    if not settings.alerts_enabled or not alerts_configured(settings):
        logger.warning(
            "HIGH risk but the WhatsApp transport is not configured -- "
            "the message is still stored on the row"
        )
        return AlertOutcome(
            STATUS_NOT_CONFIGURED,
            "WhatsApp delivery selected but not configured "
            "(set ZERNIO_INBOX_ACCOUNT_ID / ZERNIO_ALERT_CONVERSATION_ID "
            "and keep ALERTS_ENABLED=true)",
            message,
        )

    try:
        message_id = send_whatsapp_alert(message, settings)
    except AlertError as exc:
        logger.error("WhatsApp alert FAILED for record id=%s: %s", record.id, exc)
        return AlertOutcome(f"failed: {exc}", str(exc), message)
    return AlertOutcome(
        f"sent ({message_id})", f"provider message id {message_id}", message
    )


def maybe_send_alert(record, settings: Settings | None = None) -> str:
    """Compatibility wrapper: just the alert_status of `prepare_alert`."""
    return prepare_alert(record, settings=settings).status