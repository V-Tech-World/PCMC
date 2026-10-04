"""
Offline tests for Step 1 (telephony connectivity).

These never touch the real Zernio API or the PSTN -- the Zernio HTTP call is
stubbed, and the media stream is simulated over FastAPI's TestClient.
Run:
    .venv/Scripts/python -m pytest tests/ -v
"""

from __future__ import annotations

import base64
import json
import math
import struct
import tempfile
import time
import wave
from pathlib import Path

import audioop
import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

import app.main as main
from app.core import media_auth
from app.services import outbound_call
from app.core.config import get_settings



class _FakeResponse:
    def __init__(self, status_code: int = 200, body: dict | None = None):
        self.status_code = status_code
        self._body = body if body is not None else {"success": True}
        self.text = json.dumps(self._body)

    def json(self) -> dict:
        return self._body


@pytest.fixture
def client() -> TestClient:
    with TestClient(main.app) as c:
        yield c


def _alaw_sine_chunks(n_chunks: int = 3, samples_per_chunk: int = 160, freq: float = 440.0):
    """A-law frames of a 440 Hz sine, shaped like what Zernio streams."""
    chunks = []
    for c in range(n_chunks):
        raw = bytearray()
        for i in range(samples_per_chunk):
            value = int(
                12000 * math.sin(2 * math.pi * freq * ((c * samples_per_chunk + i) / 8000.0))
            )
            raw += int(value).to_bytes(2, "little", signed=True)
        chunks.append(audioop.lin2alaw(bytes(raw), 2))
    return chunks, b"".join(chunks)


# ------------------------------------------------------------------ /health


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["telephony_configured"] is True


def test_health_reports_stt_pipeline_so_a_broken_stt_is_visible_before_dialing(
    client, monkeypatch
):
    """Live 4 Oct 2026: PyAV 19 dropped a kwarg faster-whisper still passes, so
    every call crashed on its first answer -- mid-call, after the greeting, with
    the patient already on the line. /health has to be able to say so."""
    from app.api import health as health_api

    monkeypatch.setattr(health_api, "verify_stt_pipeline", lambda: (False, "TypeError: boom"))
    body = client.get("/health").json()
    assert body["stt_pipeline_ok"] is False
    assert "TypeError" in body["stt_pipeline_error"]

    monkeypatch.setattr(health_api, "verify_stt_pipeline", lambda: (True, "ok"))
    body = client.get("/health").json()
    assert body["stt_pipeline_ok"] is True
    assert body["stt_pipeline_error"] == "ok"


def test_stt_self_test_writes_a_readable_wav_and_cleans_up(monkeypatch):
    """It must exercise the real decode path, so it needs a genuine WAV -- and it
    must not leave the file behind in the temp dir."""
    import wave

    from app.api import health as health_api

    seen = {}

    class _Recorder:
        def transcribe(self, path, *a, **k):
            with wave.open(str(path), "rb") as w:
                seen.update(rate=w.getframerate(), ch=w.getnchannels(), frames=w.getnframes())
            return "anything"

    monkeypatch.setattr("app.services.stt.get_transcriber", lambda: _Recorder())
    ok, detail = health_api.verify_stt_pipeline()
    assert ok, detail
    assert seen == {"rate": 8000, "ch": 1, "frames": 8000}
    assert not (Path(tempfile.gettempdir()) / "voicecare_stt_selftest.wav").exists()


def test_stt_self_test_reports_the_error_instead_of_raising(monkeypatch):
    """A broken STT must be a clear False, never an exception escaping /health."""
    from app.api import health as health_api

    class _Broken:
        def transcribe(self, path, *a, **k):
            raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")

    monkeypatch.setattr("app.services.stt.get_transcriber", lambda: _Broken())
    ok, detail = health_api.verify_stt_pipeline()
    assert ok is False
    assert "metadata_errors" in detail


def test_requirements_pin_av_below_14():
    """The faster-whisper/PyAV clash has no upstream fix, so the ceiling on `av`
    is ours to hold. If someone relaxes it, every live call breaks again."""
    text = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    av_lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("av")]
    assert av_lines, "requirements.txt no longer pins av at all"
    assert any("<14" in ln for ln in av_lines), av_lines


# --------------------------------------------------------------- POST /calls


def test_calls_requires_api_key(client):
    assert client.post("/calls", json={"phone_number": "+94771234567"}).status_code == 401
    wrong = client.post(
        "/calls",
        json={"phone_number": "+94771234567"},
        headers={"X-Api-Key": "wrong"},
    )
    assert wrong.status_code == 401


def test_calls_validates_numbers(client):
    headers = {"X-Api-Key": "test-calls-key"}
    # not E.164
    r = client.post("/calls", json={"phone_number": "0771234567"}, headers=headers)
    assert r.status_code == 422
    assert "E.164" in r.json()["detail"]
    # wrong prefix
    r = client.post("/calls", json={"phone_number": "+15551234567"}, headers=headers)
    assert r.status_code == 422
    assert "prefix" in r.json()["detail"]
    # nothing to dial
    r = client.post("/calls", json={}, headers=headers)
    assert r.status_code == 422
    assert "TEST_TO_NUMBER" in r.json()["detail"]

def test_calls_places_call_with_token(monkeypatch, client):
    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, payload=json)
        assert headers["Authorization"] == "Bearer test-zernio-key"
        # Docs: Idempotency-Key makes retries safe (no double dial/bill).
        assert headers.get("Idempotency-Key")
        return _FakeResponse(
            200,
            {"success": True, "callId": "cc_abc123", "status": "dialing",
             "telnyxCallControlId": "tc_123"},
        )

    monkeypatch.setattr(outbound_call.requests, "post", fake_post)

    resp = client.post(
        "/calls",
        json={"phone_number": "+94771234567"},
        headers={"X-Api-Key": "test-calls-key"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["to"] == "+94771234567"
    assert body["provider_call_id"] == "cc_abc123"
    assert body["provider_status"] == "dialing"
    assert "Test Hospital" in body["greeting"]
    # stream host+path only -- the one-time token must never reach the client
    assert body["stream"] == "example.ngrok-free.dev/media-stream"

    # the exact URL/payload shape sent to Zernio (as proven by the Step 1 probe)
    assert captured["url"] == outbound_call.ZERNIO_CALLS_ENDPOINT
    forward_to = captured["payload"]["forwardTo"]
    assert forward_to.startswith("wss://example.ngrok-free.dev/media-stream?token=")
    token = forward_to.split("token=")[1]
    assert media_auth.validate_token(token)
    assert captured["payload"]["to"] == "+94771234567"
    assert captured["payload"]["fromNumber"] == "+18005551000"
    assert captured["payload"]["greeting"] == body["greeting"]
    # answering-machine detection is OFF by default (see config.py)...
    assert "amd" not in captured["payload"]
    # ...but the per-call override is forwarded: {"amd": true} -> payload amd=true
    resp = client.post(
        "/calls",
        json={"phone_number": "+94771234567", "amd": True},
        headers={"X-Api-Key": "test-calls-key"},
    )
    assert resp.status_code == 201
    assert captured["payload"]["amd"] is True
    # the category rode on the stream token for the WS handler
    assert media_auth.get_call_config(token)["diagnosis_category"] == "general"



def test_calls_amd_env_default_and_override(monkeypatch, client):
    """CALL_AMD sets the default; the per-call body value wins either way.

    This is the knob that blocked a live call: with CALL_AMD=true Zernio may
    defer the bridge until it classifies human vs machine, so the stream never
    connects. Default must stay off; the override must work in both directions.
    """
    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _FakeResponse(200, {"success": True, "callId": "cc_amd1", "status": "dialing"})

    monkeypatch.setattr(outbound_call.requests, "post", fake_post)
    headers = {"X-Api-Key": "test-calls-key"}

    # env default true -> amd=true is sent when the body omits it
    monkeypatch.setenv("CALL_AMD", "true")
    get_settings.cache_clear()
    resp = client.post("/calls", json={"phone_number": "+94771234567"}, headers=headers)
    assert resp.status_code == 201
    assert captured["payload"]["amd"] is True

    # explicit false overrides the env default (payload omits amd entirely)
    resp = client.post(
        "/calls", json={"phone_number": "+94771234567", "amd": False}, headers=headers
    )
    assert resp.status_code == 201
    assert "amd" not in captured["payload"]

    # env default false -> no amd key unless explicitly asked for
    monkeypatch.setenv("CALL_AMD", "false")
    get_settings.cache_clear()
    resp = client.post("/calls", json={"phone_number": "+94771234567"}, headers=headers)
    assert resp.status_code == 201
    assert "amd" not in captured["payload"]


def test_calls_failure_revokes_token(monkeypatch, client):
    monkeypatch.setattr(
        outbound_call.requests,
        "post",
        lambda *a, **k: _FakeResponse(403, {"success": False, "message": "bad key"}),
    )
    resp = client.post(
        "/calls",
        json={"phone_number": "+94771234567"},
        headers={"X-Api-Key": "test-calls-key"},
    )
    assert resp.status_code == 502
    assert "bad key" in resp.json()["detail"]
    assert media_auth.active_token_count() == 0  # token cleaned up on failure


# --------------------------------------------------- call status + diagnostics


def test_call_status_endpoint(monkeypatch, client):
    monkeypatch.setattr(
        outbound_call,
        "get_call_status",
        lambda call_id: {"success": True, "status": "completed", "durationSec": 42},
    )
    ok = client.get("/calls/cc_abc123/status", headers={"X-Api-Key": "test-calls-key"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "completed"
    # auth still applies
    assert client.get("/calls/cc_abc123/status").status_code == 401
    # malformed id rejected before it ever reaches Zernio
    bad = client.get(
        "/calls/..%2Fbad/status", headers={"X-Api-Key": "test-calls-key"}
    )
    assert bad.status_code in (404, 422)  # path-unsafe -> routing or validation


def test_get_call_status_parses_provider_response(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        assert url.endswith("/cc_abc123")
        assert headers["Authorization"] == "Bearer test-zernio-key"
        return _FakeResponse(200, {"success": True, "status": "answered"})

    monkeypatch.setattr(outbound_call.requests, "get", fake_get)
    body = outbound_call.get_call_status("cc_abc123")
    assert body["status"] == "answered"


def test_token_age_seconds_tracked():
    token = media_auth.issue_token()
    age = media_auth.token_age_seconds(token)
    assert age is not None and 0 <= age < 5
    assert media_auth.token_age_seconds("unknown-token") is None



# ------------------------------------------------------- WS /media-stream


def test_media_stream_rejects_missing_token(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/media-stream"):
            pass


def test_media_stream_rejects_unknown_token(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/media-stream?token=not-a-real-token"):
            pass


def test_media_stream_full_call_saves_wav_pcmu(client):
    """Zernio can negotiate PCMU per call (seen live when decoding as PCMA
    produced noise). The handler must decode with the declared codec."""
    recordings_dir = Path(get_settings().recordings_dir)
    import audioop

    pcm16 = struct.pack("<3h", 0, 12000, -12000)
    ulaw = audioop.lin2ulaw(pcm16, 2)

    token = media_auth.issue_token()
    with client.websocket_connect(f"/media-stream?token={token}") as ws:
        ws.send_json(
            {
                "event": "start",
                "start": {
                    "call_control_id": "pcmutest001",
                    "media_format": {"encoding": "PCMU", "sample_rate": 8000, "channels": 1},
                },
            }
        )
        ws.send_json(
            {"event": "media", "media": {"payload": base64.b64encode(ulaw).decode()}}
        )
        ws.send_json({"event": "stop"})

    deadline = time.time() + 5
    wav_files: list[Path] = []
    while time.time() < deadline:
        wav_files = list(recordings_dir.glob("call_pcmutest001.wav"))
        if wav_files:
            break
        time.sleep(0.05)
    assert len(wav_files) == 1, "PCMU call recording was not saved"

    with wave.open(str(wav_files[0]), "rb") as wav:
        restored = struct.unpack("<3h", wav.readframes(3))
    # decoded with mu-law, so the original amplitudes survive (not A-law garbage)
    for original, back in zip((0, 12000, -12000), restored):
        assert abs(back - original) <= max(1200, abs(original) * 0.1)


def test_media_stream_full_call_saves_wav(client):
    recordings_dir = Path(get_settings().recordings_dir)
    chunks, combined = _alaw_sine_chunks()

    token = media_auth.issue_token()
    with client.websocket_connect(f"/media-stream?token={token}") as ws:
        ws.send_json(
            {
                "event": "start",
                "start": {
                    "call_control_id": "abc123def456",
                    "media_format": {"encoding": "PCMA", "sample_rate": 8000, "channels": 1},
                },
            }
        )
        # a garbage frame must not kill the stream
        ws.send_text("this is not json")
        for chunk in chunks:
            ws.send_json(
                {"event": "media", "media": {"payload": base64.b64encode(chunk).decode()}}
            )
        ws.send_json({"event": "stop"})

    # wait briefly for the server-side finally block to finish writing
    deadline = time.time() + 5
    wav_files: list[Path] = []
    while time.time() < deadline:
        wav_files = list(recordings_dir.glob("call_*.wav"))
        if wav_files:
            break
        time.sleep(0.05)
    assert len(wav_files) == 1, f"expected one recording, found {wav_files}"

    with wave.open(str(wav_files[0]), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 8000
        # 1 A-law byte -> 1 PCM-16 sample/frame (2 bytes) in mono
        assert wav.getnframes() == len(combined)
        assert wav.getnframes() * wav.getsampwidth() * wav.getnchannels() == len(combined) * 2

    # one-time token: revoked once the call stream is over
    assert not media_auth.validate_token(token)


