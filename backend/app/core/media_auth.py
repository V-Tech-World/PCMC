"""
Per-call tokens that guard the /media-stream WebSocket.

The ngrok URL in PUBLIC_WSS_URL is effectively public, so without a check
anyone who discovers it could open the WebSocket and listen to a patient
call. Every outbound call therefore gets a fresh cryptographically random
token appended to the `forwardTo` URL. The stream endpoint rejects any
connection whose token is missing, unknown, or expired.

Tokens live in memory only -- a backend restart simply invalidates them
(all it costs is re-dialing; there is nothing to persist at this step).
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time

logger = logging.getLogger("voicecare.media_auth")

DEFAULT_TOKEN_TTL_SECONDS = 600  # long enough for a short check-in call

_tokens: dict[str, float] = {}  # token -> expiry (unix seconds)
_call_configs: dict[str, dict] = {}  # token -> call config (e.g. diagnosis_category)
_token_issued: dict[str, float] = {}  # token -> creation time (dial-to-answer diagnostics)



def _purge_expired(now: float | None = None) -> None:
    now = time.time() if now is None else now
    for token in [t for t, expiry in _tokens.items() if expiry <= now]:
        _tokens.pop(token, None)
        _call_configs.pop(token, None)
        _token_issued.pop(token, None)



def issue_token(
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS, config: dict | None = None
) -> str:
    """Create a fresh single-call token, optionally carrying call config
    (e.g. diagnosis_category) that the stream endpoint reads on connect."""
    _purge_expired()
    token = secrets.token_urlsafe(32)
    _tokens[token] = time.time() + ttl_seconds
    _token_issued[token] = time.time()
    if config:
        _call_configs[token] = dict(config)

    logger.info("Issued media-stream token (ttl=%ds, active=%d)", ttl_seconds, len(_tokens))
    return token


def get_call_config(token: str | None) -> dict:
    """Config attached to the token by the outbound call (empty if unknown)."""
    if not token:
        return {}
    return dict(_call_configs.get(token) or {})


def token_age_seconds(token: str | None) -> float | None:
    """Seconds since the token (== the call) was issued; None if unknown."""
    if not token or token not in _token_issued:
        return None
    return max(0.0, time.time() - _token_issued[token])




def validate_token(token: str | None) -> bool:
    """True only if the token exists and has not expired (constant-time compare)."""
    if not token:
        return False
    now = time.time()
    for known, expiry in list(_tokens.items()):
        if hmac.compare_digest(token, known) and expiry > now:
            return True
    return False


def revoke_token(token: str | None) -> None:
    if token:
        _tokens.pop(token, None)
        _call_configs.pop(token, None)
        _token_issued.pop(token, None)


def active_token_count() -> int:
    _purge_expired()
    return len(_tokens)


def reset() -> None:
    """Test-only helper: drop all tokens."""
    _tokens.clear()
    _call_configs.clear()
    _token_issued.clear()


