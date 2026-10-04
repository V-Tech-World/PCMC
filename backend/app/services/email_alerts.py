"""
Staff email alerts (4 Oct 2026): a HIGH-risk call is emailed to the care team.

EMAIL IS A SECOND CHANNEL, NOT A REPLACEMENT
--------------------------------------------
The WhatsApp alert in app/services/alerts.py still goes out exactly as before.
This module adds a targeted email, so the alert reaches named people in their
own mailbox instead of only a shared sandbox thread.

WHO GETS IT -- RISK SCORE, NOT ROLE ALONE
-----------------------------------------
    score <  ALERT_DOCTOR_SCORE_THRESHOLD  -> active nurses
    score >= ALERT_DOCTOR_SCORE_THRESHOLD  -> active nurses AND active doctors

"a nurse should call this patient back" and "a doctor needs to know now" are
different decisions, so they get different thresholds. Admins are never
score-routed -- they already watch the whole board.

DELIVERY
--------
Gmail SMTP + an App Password, the same transport as backend/mail.py
(SENDER_EMAIL / GOOGLE_APP_PASSWORD). One message per recipient -- no CC/BCC --
so a single mistyped address cannot hide the others, and every person's outcome
is recorded on the call row for the Calls / Dashboard screens.

Nothing here raises into the call flow: an unconfigured mailbox or a failed
send still leaves the alert text and the per-recipient reason on the row, and
the WhatsApp path is untouched.
"""

from __future__ import annotations

import logging
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape

from app.core.config import Settings, get_settings

logger = logging.getLogger("voicecare.email_alerts")

#: Only HIGH risk is emailed (mirrors app/services/alerts.py).
ALERT_RISK_LEVEL = "high"

#: Per-recipient status recorded on the call row instead of a bare boolean, so
#: the dashboard can say *why* someone was not reached.
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_NO_EMAIL = "no_email"
STATUS_DISABLED = "disabled"
STATUS_NOT_CONFIGURED = "not_configured"

#: A stalled SMTP handshake must not hold the call-flow thread open.
_SMTP_TIMEOUT_SECONDS = 20

#: Deliberately permissive. The only thing this must catch is an address that
#: can never receive mail (no "@", no dotted domain) -- an over-strict pattern
#: would reject real hospital addresses (plus-addressing, subdomains, long TLDs)
#: and lock a nurse out of the alert list.
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")


def looks_like_email(value: str | None) -> bool:
    """True when `value` is shaped like a deliverable email address."""
    return bool(_EMAIL_SHAPE.match((value or "").strip()))


class EmailAlertError(RuntimeError):
    """The alert email could not be delivered to one recipient."""


@dataclass(frozen=True)
class Recipient:
    """One person the alert was routed to, and how their delivery went."""

    username: str
    display_name: str
    role: str
    email: str
    status: str

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "email": self.email,
            "status": self.status,
        }


def email_alerts_configured(settings: Settings | None = None) -> bool:
    """True when there is both a From: address and a Gmail App Password."""
    settings = settings or get_settings()
    return bool(
        (settings.sender_email or "").strip()
        and (settings.google_app_password or "").strip()
    )


def routed_roles(
    risk_score: float, settings: Settings | None = None
) -> tuple[str, ...]:
    """Which staff roles a HIGH-risk call of this score should reach.

    The whole routing policy in one place, so a test can pin it: below the
    threshold the nurse on call owns the case; AT or above it a doctor is
    pulled in as well.
    """
    settings = settings or get_settings()
    try:
        score = float(risk_score or 0.0)
        threshold = float(settings.alert_doctor_score_threshold)
    except (TypeError, ValueError):
        # A malformed score must not silently escalate -- treat it as a nurse call.
        logger.warning("Unusable risk score %r -- routing to nurses only", risk_score)
        return ("nurse",)
    if score >= threshold:
        return ("nurse", "doctor")
    return ("nurse",)


def _staff_snapshot() -> list:
    """Every staff row, for routing.

    Wrapped because a database hiccup must never break the call flow -- the
    alert text and the WhatsApp path are independent of this lookup.
    """
    try:
        from app.db import service as db_service

        return db_service.list_staff()
    except Exception:
        logger.exception(
            "Could not load staff for alert routing -- nobody will be emailed"
        )
        return []


def select_recipients(
    risk_score: float,
    settings: Settings | None = None,
    staff: list | None = None,
) -> list:
    """The active staff a HIGH-risk call of this score is emailed to."""
    settings = settings or get_settings()
    roles = routed_roles(risk_score, settings)
    people = _staff_snapshot() if staff is None else list(staff)
    chosen = [
        user
        for user in people
        if getattr(user, "active", False)
        and (getattr(user, "role", "") or "") in roles
    ]
    # Doctors first, then nurses, then by employee ID: a deterministic order
    # makes the call row and the logs read the same way every run.
    chosen.sort(key=lambda u: (u.role != "doctor", (u.username or "")))
    return chosen


def format_email_subject(record) -> str:
    """Mailbox-friendly subject so the alert sorts to the top of an inbox."""
    patient = record.patient_code or "UNKNOWN"
    score = float(getattr(record, "risk_score", 0.0) or 0.0)
    return f"[VoiceCare URGENT] High-risk follow-up {patient} (score {score:g})"


def _symptom_lines(record) -> list[str]:
    lines: list[str] = []
    for finding in record.get_findings() if hasattr(record, "get_findings") else []:
        label = finding.get("label") or finding.get("id") or "symptom"
        severity = finding.get("severity") or ""
        flag = " (red flag)" if finding.get("red_flag") else ""
        lines.append(f"{label} - {severity}{flag}".strip(" -"))
    return lines


def _answer_lines(record) -> list[str]:
    lines: list[str] = []
    for answer in record.get_answers() if hasattr(record, "get_answers") else []:
        question = answer.get("question_id") or "question"
        said = answer.get("transcript") or answer.get("interpretation")
        lines.append(f"{question}: {said if said not in (None, '') else '-'}")
    return lines


def _detail_rows(record) -> list[tuple[str, str]]:
    score = float(getattr(record, "risk_score", 0.0) or 0.0)
    return [
        ("Patient code", record.patient_code or "UNKNOWN"),
        ("Phone", record.phone_number or "-"),
        ("Care category", str(record.diagnosis_category or "-")),
        ("Risk", f"{str(record.risk_level or 'unknown').upper()} (score {score:g})"),
        (
            "Call started",
            record.started_at.strftime("%Y-%m-%d %H:%M UTC")
            if record.started_at
            else "-",
        ),
        ("Call length", f"{float(record.duration_sec or 0.0):.0f}s"),
    ]


def _plain_text(
    record, patient_name: str, greeting: str, details, reasons, symptoms, answers
) -> str:
    score = float(getattr(record, "risk_score", 0.0) or 0.0)
    patient_code = record.patient_code or "UNKNOWN"

    lines = ["URGENT post-discharge check-in alert", ""]
    if greeting:
        lines += [f"Hello {greeting},", ""]
    lines += [
        f"{patient_code} has just been flagged HIGH risk (score {score:g}).",
        "Please review the case and call the patient back.",
        "",
    ]
    lines += [f"{key}: {value}" for key, value in details]
    if reasons:
        lines += ["", "Why it is HIGH risk:"]
        lines += [f"- {reason}" for reason in reasons]
    if symptoms:
        lines += ["", "Key symptoms:"]
        lines += [f"- {line}" for line in symptoms]
    if answers:
        lines += ["", "What the patient said:"]
        lines += [f"- {line}" for line in answers]
    lines += ["", "Open the VoiceCare dashboard for the full transcript."]
    return "\n".join(lines)


def _html_rows(items: list[tuple[str, str]]) -> str:
    return "".join(
        "<tr>"
        '<td style="padding:4px 12px 4px 0;color:#6b7280;white-space:nowrap;">'
        f"{escape(str(key))}</td>"
        f'<td style="padding:4px 0;font-weight:600;">{escape(str(value))}</td>'
        "</tr>"
        for key, value in items
    )


def _html_bullets(items: list[str], colour: str = "#1f2937") -> str:
    return "".join(
        f'<li style="margin:2px 0;color:{colour};">{escape(str(item))}</li>'
        for item in items
    )


def _html(body_content: str) -> str:
    """One inline-styled shell, so the alert reads the same in every client."""
    return (
        "<!doctype html>"
        '<html><body style="margin:0;padding:0;background:#f4f5f7;'
        'font-family:Segoe UI,Arial,sans-serif;color:#1f2937;">'
        '<div style="max-width:640px;margin:0 auto;padding:24px;">'
        '<div style="background:#b91c1c;color:#ffffff;border-radius:10px 10px 0 0;'
        'padding:16px 20px;">'
        '<div style="font-size:12px;letter-spacing:.12em;text-transform:uppercase;'
        'opacity:.85;">VoiceCare &middot; Urgent</div>'
        '<div style="font-size:20px;font-weight:600;margin-top:4px;">'
        "High-risk post-discharge call</div></div>"
        '<div style="background:#ffffff;border:1px solid #e5e7eb;border-top:none;'
        'border-radius:0 0 10px 10px;padding:20px;">'
        f"{body_content}"
        "</div></div></body></html>"
    )


def format_email_body(record, patient=None, recipient=None) -> tuple[str, str]:
    """(plain text, HTML) for one recipient's alert email.

    Both parts carry the same facts: the HTML is what a desktop mail client
    shows, and the plain-text part is what a phone on a poor hospital
    connection actually renders -- neither is a stub.
    """
    score = float(getattr(record, "risk_score", 0.0) or 0.0)
    patient_code = record.patient_code or "UNKNOWN"
    patient_name = (getattr(patient, "name", "") or "").strip()
    greeting = ""
    if recipient is not None:
        greeting = (
            getattr(recipient, "display_name", "")
            or getattr(recipient, "username", "")
            or ""
        ).strip()

    symptoms = _symptom_lines(record)
    answers = _answer_lines(record)
    reasons = record.get_risk_reasons() if hasattr(record, "get_risk_reasons") else []
    details = _detail_rows(record)
    if patient_name:
        details.insert(1, ("Patient name", patient_name))

    text_body = _plain_text(
        record, patient_name, greeting, details, reasons, symptoms, answers
    )

    blocks = ""
    if reasons:
        blocks += (
            '<p style="margin:18px 0 6px;font-weight:600;">Why it is HIGH risk</p>'
            f'<ul style="margin:0;padding-left:20px;">{_html_bullets(reasons)}</ul>'
        )
    if symptoms:
        blocks += (
            '<p style="margin:18px 0 6px;font-weight:600;">Key symptoms</p>'
            '<ul style="margin:0;padding-left:20px;">'
            f"{_html_bullets(symptoms, '#b91c1c')}</ul>"
        )
    if answers:
        blocks += (
            '<p style="margin:18px 0 6px;font-weight:600;">What the patient said</p>'
            f'<ul style="margin:0;padding-left:20px;">{_html_bullets(answers)}</ul>'
        )

    hello = (
        f'<p style="margin:0 0 12px;">Hello {escape(greeting)},</p>' if greeting else ""
    )
    html_body = _html(
        f"{hello}"
        '<p style="margin:0 0 16px;"><b>'
        f"{escape(patient_code)}</b> has just been flagged <b>HIGH</b> risk "
        f"(score {score:g}). Please review the case and call the patient back.</p>"
        f'<table style="border-collapse:collapse;font-size:14px;">'
        f"{_html_rows(details)}</table>"
        f"{blocks}"
        '<p style="margin:20px 0 0;font-size:13px;color:#6b7280;">'
        "Open the VoiceCare dashboard for the full transcript and case tools.</p>"
    )
    return text_body, html_body


def send_email(
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str,
    settings: Settings | None = None,
) -> None:
    """Send one alert email over Gmail SMTP.

    Raises EmailAlertError on any failure, so the caller can record *which*
    recipient was missed rather than just "the alert failed".
    """
    settings = settings or get_settings()
    sender = (settings.sender_email or "").strip()
    password = (settings.google_app_password or "").strip()
    if not sender or not password:
        raise EmailAlertError("SENDER_EMAIL / GOOGLE_APP_PASSWORD are not configured")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to_email
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=_SMTP_TIMEOUT_SECONDS
        ) as server:
            server.starttls()
            server.login(sender, password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        # Never log the App Password or the body -- these mailboxes are shared.
        raise EmailAlertError(f"SMTP delivery to {to_email} failed: {exc}") from exc


def _record(user, status: str) -> dict:
    return Recipient(
        username=getattr(user, "username", "") or "",
        display_name=getattr(user, "display_name", "") or "",
        role=getattr(user, "role", "") or "",
        email=(getattr(user, "email", "") or "").strip(),
        status=status,
    ).to_dict()


def no_route_note() -> dict:
    """A visible placeholder when nobody could be routed.

    Returning [] here is what makes the dashboard "quietly" show nothing while
    WhatsApp shows `sent (wamid...)`. A named row -- with an empty address and
    a status explaining why -- makes "nobody heard about it" unmissable and
    tells the admin exactly what to do (Staff screen -> valid emails).
    """
    return Recipient(
        username="",
        display_name="No routable staff",
        role="",
        email="",
        status=(
            "no_route: no active nurse/doctor account with a valid email "
            "-- add one on the Staff screen"
        ),
    ).to_dict()


def deliver_alert_emails(
    record,
    patient=None,
    settings: Settings | None = None,
    staff: list | None = None,
) -> list[dict]:
    """Email a finished HIGH-risk call to the score-routed staff.

    Always returns the recipient list (each with a status) for the call row --
    including runs where nothing was actually sent, because "who should have
    heard and did not" is the thing that matters after a missed escalation.
    Never raises.
    """
    settings = settings or get_settings()

    if getattr(record, "risk_level", "") != ALERT_RISK_LEVEL:
        return []

    recipients = select_recipients(
        getattr(record, "risk_score", 0.0), settings, staff=staff
    )
    if not recipients:
        logger.warning(
            "HIGH-risk call %s has no routable nurse/doctor account with an email "
            "-- create staff on the Staff screen",
            getattr(record, "id", "?"),
        )
        return [no_route_note()]

    if not settings.email_alerts_enabled:
        logger.info(
            "EMAIL_ALERTS_ENABLED=false -- recording %d recipient(s), sending nothing",
            len(recipients),
        )
        return [_record(user, STATUS_DISABLED) for user in recipients]

    if not email_alerts_configured(settings):
        logger.warning(
            "Email alerts selected but SENDER_EMAIL/GOOGLE_APP_PASSWORD are unset "
            "-- %d recipient(s) recorded, nothing sent",
            len(recipients),
        )
        return [_record(user, STATUS_NOT_CONFIGURED) for user in recipients]

    subject = format_email_subject(record)
    delivered: list[dict] = []
    for user in recipients:
        address = (getattr(user, "email", "") or "").strip()
        if not looks_like_email(address):
            # An account without a usable mailbox can never be alerted -- say so
            # on the row instead of dropping the person silently.
            logger.error(
                "Alert email skipped: staff %r has no usable email address (%r)",
                user.username,
                address,
            )
            delivered.append(_record(user, STATUS_NO_EMAIL))
            continue
        text_body, html_body = format_email_body(record, patient, user)
        try:
            send_email(address, subject, text_body, html_body, settings)
        except EmailAlertError as exc:
            # One bad address must not stop the rest of the care team.
            logger.error("Alert email to %s FAILED: %s", user.username, exc)
            delivered.append(_record(user, f"{STATUS_FAILED}: {exc}"))
        else:
            logger.info("Alert email sent to %s <%s>", user.username, address)
            delivered.append(_record(user, STATUS_SENT))
    return delivered

