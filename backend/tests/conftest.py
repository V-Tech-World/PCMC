"""Shared pytest setup: make the backend package importable and pin settings."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core import media_auth  # noqa: E402
from app.core.config import get_settings  # noqa: E402


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    """Deterministic settings + clean token state for every test."""
    monkeypatch.setenv("ZERNIO_API_KEY", "test-zernio-key")
    monkeypatch.setenv("FROM_NUMBER", "+18005551000")
    monkeypatch.setenv("PUBLIC_WSS_URL", "wss://example.ngrok-free.dev/media-stream")
    monkeypatch.setenv("CALLS_API_KEY", "test-calls-key")
    monkeypatch.setenv("ALLOWED_CALL_PREFIXES", "+94")
    monkeypatch.setenv("MEDIA_STREAM_REQUIRE_TOKEN", "true")
    monkeypatch.setenv("TEST_TO_NUMBER", "")
    monkeypatch.setenv("HOSPITAL_NAME", "Test Hospital")
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("STT_MODEL", "base")
    # Step 1/2 transport tests run with the dialogue off; Step 3 tests that
    # need it turn it back on explicitly with monkeypatch.setenv.
    monkeypatch.setenv("DIALOGUE_ENABLED", "false")
    monkeypatch.setenv("TTS_BACKEND", "pyttsx3")
    # Step 6/7 tests must never touch the real voicecare.db file: a shared
    # in-memory SQLite (StaticPool) keeps the suite hermetic and fast.
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    # Alerts must never actually fire from tests.
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    get_settings.cache_clear()
    media_auth.reset()
    yield
    get_settings.cache_clear()
    media_auth.reset()


