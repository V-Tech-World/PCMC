"""
Staff authentication (Step 9): HS256 JWTs + PBKDF2 password hashing.

Deliberately small -- one shared dashboard, three roles (nurse | doctor |
admin), no patient-facing accounts (README section 9).

Two things matter here:

1. `require_auth` accepts EITHER the machine `X-Api-Key` header (scripts, the
   live-test plan) OR a staff bearer token (the dashboard). The browser never
   needs the backend API key, and existing callers keep working unchanged.
2. Role checks are declarative (`require_roles("admin")`) so each endpoint
   states its own rule in one line, matching the README's role table.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import jwt
from fastapi import Header, HTTPException

from app.core.config import get_settings

logger = logging.getLogger("voicecare.security")

ROLES: tuple[str, ...] = ("nurse", "doctor", "admin")

# PBKDF2-HMAC-SHA256. High enough for a demo, fast enough for a login request.
_PBKDF2_ROUNDS = 200_000
_SALT_BYTES = 16


class TokenError(RuntimeError):
    """A bearer token was missing, malformed, expired or badly signed."""


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Return ``(hash_hex, salt_hex)`` for a password (PBKDF2-HMAC-SHA256)."""
    salt_hex = salt or secrets.token_hex(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        _PBKDF2_ROUNDS,
    )
    return digest.hex(), salt_hex


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    """Constant-time password check."""
    if not hash_hex or not salt_hex:
        return False
    try:
        candidate, _ = hash_password(password, salt_hex)
    except ValueError:  # malformed salt in the row
        return False
    return hmac.compare_digest(candidate, hash_hex)


def create_access_token(
    *,
    subject: str,
    role: str,
    display_name: str = "",
    hospital: str = "",
    expires_minutes: int | None = None,
) -> tuple[str, int]:
    """Sign a staff JWT. Returns ``(token, expires_in_seconds)``."""
    settings = get_settings()
    secret = settings.signing_secret
    if not secret:
        raise TokenError(
            "JWT_SECRET (or CALLS_API_KEY) is not configured -- cannot sign tokens."
        )
    minutes = expires_minutes or settings.jwt_expires_minutes
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "name": display_name,
        "hospital": hospital,
        "iss": settings.jwt_issuer,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    return token, int(minutes * 60)


def decode_token(token: str) -> dict:
    """Verify a staff JWT and return its claims."""
    settings = get_settings()
    secret = settings.signing_secret
    if not secret:
        raise TokenError("Signing secret is not configured.")
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired -- please log in again.") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Invalid token.") from exc


# ---------------------------------------------------------------------------
# Request authentication
# ---------------------------------------------------------------------------

class AuthContext:
    """Who is calling an operational endpoint, and how they proved it."""

    def __init__(self, via: str, role: str = "", subject: str = "", name: str = ""):
        self.via = via          # "api_key" | "staff"
        self.role = role        # "" for api_key (server-to-server)
        self.subject = subject  # username / employee id
        self.name = name

    @property
    def is_staff(self) -> bool:
        return self.via == "staff"

    def allows(self, roles: Iterable[str]) -> bool:
        """API-key callers are unrestricted (scripts/ops, not people)."""
        roles = tuple(roles)
        if not roles or not self.is_staff:
            return True
        return self.role in roles


def _api_key_ok(header_value: str | None) -> bool:
    settings = get_settings()
    if not settings.calls_api_key or not header_value:
        return False
    return secrets.compare_digest(
        header_value.encode("utf-8"), settings.calls_api_key.encode("utf-8")
    )


def staff_from_token(authorization: str | None) -> AuthContext | None:
    """Resolve an ``Authorization: Bearer <jwt>`` header, or None if absent."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    claims = decode_token(parts[1].strip())
    return AuthContext(
        via="staff",
        role=str(claims.get("role", "")),
        subject=str(claims.get("sub", "")),
        name=str(claims.get("name", "")),
    )


def require_auth(
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    authorization: str | None = Header(default=None),
) -> AuthContext:
    """FastAPI dependency: machine API key OR a valid staff bearer token."""
    settings = get_settings()
    if not settings.calls_api_key and not settings.signing_secret:
        raise HTTPException(
            status_code=503,
            detail="CALLS_API_KEY (and JWT_SECRET) are not configured in backend/.env.",
        )
    if _api_key_ok(x_api_key):
        return AuthContext(via="api_key")
    if authorization:
        try:
            context = staff_from_token(authorization)
        except TokenError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        if context is not None:
            return context
    raise HTTPException(
        status_code=401,
        detail="Provide a valid X-Api-Key header or a staff bearer token.",
    )


def require_roles(*roles: str):
    """Build a dependency that additionally enforces the README role table."""

    def _check(
        x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
        authorization: str | None = Header(default=None),
    ) -> AuthContext:
        context = require_auth(x_api_key=x_api_key, authorization=authorization)
        if not context.allows(roles):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"This action requires one of: {', '.join(roles)} "
                    f"(your role: {context.role or 'unknown'})."
                ),
            )
        return context

    return _check
