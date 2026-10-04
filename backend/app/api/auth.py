"""
Auth API (Step 9): staff login + account management for the dashboard.

The hospital has one dashboard and three roles (README section 9):

    nurse  -- sees the board, calls patients, marks cases reviewed
    doctor -- everything a nurse can do, plus closing escalated cases
    admin  -- manages patients, staff accounts and the scheduler

There is no patient login and no signup endpoint: the first admin comes from
`ADMIN_USERNAME`/`ADMIN_PASSWORD` in .env (seeded on startup). Additional
accounts are created by an admin via POST /auth/staff. Staff who forgot their
password reset it themselves via POST /auth/reset-password (employee ID + new
password + confirm -- no admin approval, no third party, by demo design).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.security import (
    ROLES,
    AuthContext,
    TokenError,
    create_access_token,
    require_auth,
    require_roles,
    verify_password,
)
from app.db.models import iso_utc
from app.db import service as db_service
from app.services import email_alerts

logger = logging.getLogger("voicecare.auth")

router = APIRouter(prefix="/auth")


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class StaffCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=6, max_length=256)
    role: str = "nurse"
    display_name: str = ""
    hospital: str = ""
    # Enforced as required by the endpoint (not by pydantic) so an empty value
    # gets our own 422 message. A nurse or doctor without a mailbox can never be
    # told about a HIGH-risk call, which is the whole point of the account
    # (4 Oct 2026).
    email: str = Field(default="", max_length=254)


class ResetPasswordRequest(BaseModel):
    """Self-service reset: employee ID + the new password twice.

    Deliberately NO admin approval and NO third-party call (no mail/SMS
    provider): the demo runs on a hospital intranet and the login screen says
    so plainly. Unknown and deactivated accounts get the same generic 404, so
    the form cannot be used to probe which employee IDs exist.
    """

    username: str = Field(min_length=1, max_length=64)
    new_password: str = Field(min_length=6, max_length=256)
    confirm_password: str = Field(min_length=1, max_length=256)


def _staff_to_dict(user) -> dict:
    return {
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "hospital": user.hospital,
        "email": user.email,
        "active": user.active,
        "last_login_at": iso_utc(user.last_login_at),
    }


@router.post("/login")
def login(body: LoginRequest) -> dict:
    """Exchange employee ID + password for a bearer token."""
    settings = get_settings()
    if not settings.signing_secret:
        raise HTTPException(
            status_code=503,
            detail="Auth is not configured: set JWT_SECRET (or CALLS_API_KEY) in backend/.env.",
        )

    user = db_service.get_staff_by_username(body.username.strip())
    # One generic message for both "no such user" and "wrong password" -- the
    # dashboard is on a hospital network but this costs nothing.
    if user is None or not user.active or not verify_password(
        body.password, user.password_hash, user.password_salt
    ):
        logger.warning("Failed login for %r", body.username)
        raise HTTPException(status_code=401, detail="Invalid employee ID or password.")

    try:
        token, expires_in = create_access_token(
            subject=user.username,
            role=user.role,
            display_name=user.display_name,
            hospital=user.hospital,
        )
    except TokenError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    db_service.touch_last_login(user.username)
    logger.info("Login ok: %s (%s)", user.username, user.role)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": expires_in,
        "user": _staff_to_dict(user),
    }


@router.post("/reset-password")
def reset_password(body: ResetPasswordRequest) -> dict:
    """Forgot-password flow: type the employee ID, set a new password.

    Self-service on purpose -- no admin approval and no third-party email/SMS
    service (demo scope, README section 9). The caller is by definition logged
    out, so this endpoint takes no auth; unknown and deactivated accounts are
    answered with one generic 404 (never reveals which IDs exist).
    """
    if body.new_password != body.confirm_password:
        raise HTTPException(
            status_code=422, detail="New password and confirmation do not match."
        )
    user = db_service.get_staff_by_username(body.username.strip())
    if user is None or not user.active:
        logger.warning("Password reset attempted for unknown/inactive %r", body.username)
        raise HTTPException(
            status_code=404, detail="No account found with that employee ID."
        )
    db_service.update_staff_password(user.username, body.new_password)
    logger.info("Password self-reset for %s", user.username)
    return {
        "status": "password_updated",
        "username": user.username,
        "message": "Password updated -- sign in with your new password.",
    }


@router.get("/me")
def me(context: AuthContext = Depends(require_auth)) -> dict:
    """Current session (the dashboard calls this on load to restore login)."""
    if not context.is_staff:
        return {"via": context.via, "role": "service", "username": "", "display_name": "API key"}
    user = db_service.get_staff_by_username(context.subject)
    return {
        "via": context.via,
        "role": context.role,
        "username": context.subject,
        "display_name": context.name,
        "user": _staff_to_dict(user) if user else None,
    }


@router.get("/roles")
def roles() -> dict:
    """The role matrix, so the frontend can hide what a role cannot do."""
    return {
        "roles": list(ROLES),
        "permissions": {
            "view_dashboard": ["nurse", "doctor", "admin"],
            "call_patient": ["nurse", "doctor", "admin"],
            "review_call": ["nurse", "doctor", "admin"],
            "close_case": ["doctor", "admin"],
            "manage_patients": ["admin"],
            "manage_staff": ["admin"],
            "manage_scheduler": ["admin"],
        },
    }


@router.get("/staff")
def list_staff(context: AuthContext = Depends(require_roles("admin"))) -> dict:
    rows = db_service.list_staff()
    return {"count": len(rows), "staff": [_staff_to_dict(u) for u in rows]}


@router.post("/staff", status_code=201)
def create_staff(
    body: StaffCreate, context: AuthContext = Depends(require_roles("admin"))
) -> dict:
    """Create a dashboard login (admin only).

    The email address is mandatory: this account exists to be told about
    HIGH-risk calls, so an account with no mailbox is refused up front rather
    than silently skipped at 3am.
    """
    if body.role not in ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"role must be one of: {', '.join(ROLES)}",
        )
    email = body.email.strip()
    if db_service.get_staff_by_username(body.username.strip()):
        raise HTTPException(status_code=409, detail="That employee ID already exists.")
    if not email_alerts.looks_like_email(email):
        raise HTTPException(
            status_code=422,
            detail="A valid email address is required (this is where the "
                   "HIGH-risk alerts are sent).",
        )
    settings = get_settings()
    user = db_service.create_staff(
        username=body.username.strip(),
        password=body.password,
        role=body.role,
        display_name=body.display_name or body.username,
        hospital=body.hospital or settings.default_hospital,
        email=email,
    )
    logger.info("Staff account created by %s: %s", context.subject or "api-key", user.username)
    return {"status": "created", "staff": _staff_to_dict(user)}
