"""
Alerts (Step 7): HIGH-risk calls notify the care team over WhatsApp.

Delivery rides on the Zernio inbox (same API key as the voice calls), using
the SANDBOX WhatsApp conversation the operator registered:

    POST https://zernio.com/api/v1/inbox/conversations/{conversation_id}/messages
    Authorization: Bearer <ZERNIO_API_KEY>
    {"accountId": "<inbox account id>", "message": "<text>"}

The sandbox credentials live in their own env vars (ZERNIO_INBOX_ACCOUNT_ID /
ZERNIO_ALERT_CONVERSATION_ID) and never touch the real toll-free FROM_NUMBER:
voice calls keep dialing from the toll-free line, alerts go out through the
sandbox WhatsApp thread. If either sandbox var is unset, alerts are skipped
(with a logged reason) -- the call row still records the risk.

Per the README TCs: only HIGH risk triggers an alert, and the message carries
the patient code, risk level, and key symptoms (plus the full answers and
transcripts, so the on-call nurse sees everything without opening the app).
"""

from __future__ import annotations

import logging

import requests

from app.core.config import Settings, get_settings

logger = logging.getLogger("voicecare.alerts")

ZERNIO_INBOX_MESSAGES_ENDPOINT = "https://zernio.com/api/v1/inbox/conversations"
_ALERT_TIMEOUT_SECONDS = 15

#: Only HIGH risk pages the care team (Step 7 TC2: low risk stays silent).
ALERT_RISK_LEVEL = "high"


class AlertError(RuntimeError):
    """The WhatsApp alert could not be delivered."""


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


def maybe_send_alert(record, settings: Settings | None = None) -> str:
    """Alert gate: HIGH risk -> send; anything else -> skipped (Step 7 TC2).

    Returns the final alert_status for the record: 'sent (id)', 'failed: ...',
    'skipped' (low/medium risk), or 'not_configured'.
    """
    settings = settings or get_settings()
    if record.risk_level != ALERT_RISK_LEVEL:
        logger.info(
            "No alert: risk=%s (only %s pages the care team)",
            record.risk_level, ALERT_RISK_LEVEL,
        )
        return "skipped"
    if not settings.alerts_enabled or not alerts_configured(settings):
        logger.warning("HIGH risk detected but alerts are not configured -- not sent")
        return "not_configured"

    message = format_alert_message(record)
    try:
        message_id = send_whatsapp_alert(message, settings)
    except AlertError as exc:
        logger.error("WhatsApp alert FAILED for record id=%s: %s", record.id, exc)
        return f"failed: {exc}"
    return f"sent ({message_id})"