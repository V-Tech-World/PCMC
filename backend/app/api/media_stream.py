"""
WebSocket endpoint that receives the live call audio from Zernio and runs
the Step 3 structured dialogue over it.

Contract (confirmed live):
- Zernio connects to  wss://<host>/media-stream?token=<per-call token>
- Frames (JSON text):
    {"version": "1.0.0", "event": "connected"}
    {"event": "start", "start": {"call_control_id": ..., "media_format": {...}}}
    {"event": "media", "media": {"payload": "<base64 PCMA/PCMU bytes>"}}
    {"event": "stop"}
- Audio is 8000 Hz mono; the codec (PCMA or PCMU) is declared per call.

Architecture: the receiver loop below is the ONLY reader of the socket; it
feeds an asyncio inbox consumed by the call-flow driver
(app/services/call_flow.py), which is the only sender (questions/filler
audio). This keeps concurrent send/receive safe.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core import media_auth
from app.core.config import get_settings
from app.services import call_flow
from app.services import recordings as recordings_service

logger = logging.getLogger("voicecare.stream")

router = APIRouter()

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_-]")
_BYTES_PER_A_LAW_SECOND = 8000  # 1 byte per sample at 8 kHz


@dataclass
class MediaSession:
    """Per-connection state: buffers audio and logs lifecycle events."""

    call_id: str = "unknown"
    audio_chunks: list[bytes] = field(default_factory=list)
    agent_pcm: bytearray = field(default_factory=bytearray)  # audio WE sent (PCM16)
    media_frames: int = 0
    track_skipped: int = 0
    frames_while_speaking: int = 0
    started_at: float | None = None
    media_format: dict | None = None
    stop_received: bool = False
    ended: bool = False          # stream over (patient hung up or provider stop)
    encoding: str = "PCMA"
    speaking: bool = False  # True while call_flow is sending question audio
    settle_until: float = 0.0  # monotonic time until which mic input is echo
    last_frame_at: float | None = None
    max_frame_gap_sec: float = 0.0

    # -- event handling ------------------------------------------------------

    def handle_connected(self, data: dict) -> None:
        logger.info("event=connected version=%s", data.get("version", "?"))

    def handle_start(self, start: dict) -> None:
        raw_id = str(start.get("call_control_id") or "unknown")
        # Provider-supplied id ends up in a filename -- keep only safe chars.
        self.call_id = _SAFE_FILENAME_RE.sub("", raw_id)[-12:] or "unknown"
        self.started_at = time.monotonic()
        self.media_format = start.get("media_format")
        logger.info(
            "event=start call_id=...%s media_format=%s", self.call_id, self.media_format
        )
        self.encoding = self._read_media_format(self.media_format)

    @staticmethod
    def _read_media_format(fmt: dict | None) -> str:
        """Validate the declared format; return the codec we must decode."""
        if not isinstance(fmt, dict):
            logger.warning("event=start had no media_format object -- assuming PCMA")
            return "PCMA"
        encoding = str(fmt.get("encoding") or fmt.get("format") or "PCMA").upper()
        rate = fmt.get("sample_rate") or fmt.get("sampling_rate") or 0
        channels = fmt.get("channels", 0)
        if rate == 8000 and channels == 1 and encoding in ("PCMA", "PCMU"):
            logger.info("media format OK: %s / 8000 Hz / mono", encoding)
        else:
            logger.warning(
                "unexpected media format (expected PCMA|PCMU/8000/1): encoding=%r rate=%r channels=%r",
                encoding,
                rate,
                channels,
            )
        if encoding not in ("PCMA", "PCMU"):
            logger.warning("unknown codec %r -- falling back to PCMA", encoding)
            encoding = "PCMA"
        return encoding

    def accept_media(self, media: dict) -> bytes | None:
        """Validate + buffer one media frame; returns the compressed bytes
        for the inbox (None = frame should not be treated as patient audio)."""
        payload_b64 = media.get("payload") or ""
        if not payload_b64:
            return None
        try:
            chunk = base64.b64decode(payload_b64)
        except (ValueError, TypeError):
            logger.warning("dropping media frame with invalid base64 payload")
            return None

        now = time.monotonic()
        if self.last_frame_at is not None:
            gap = now - self.last_frame_at
            self.max_frame_gap_sec = max(self.max_frame_gap_sec, gap)
        self.last_frame_at = now

        # Only the patient's side of the call is recorded. If the provider
        # labels tracks, keep 'inbound' only (patient -> us).
        track = media.get("track")
        if track and track != "inbound":
            self.track_skipped += 1
            return None

        if self.speaking or time.monotonic() < self.settle_until:
            # Echo of our own question through the patient's speaker
            # (during speech, plus a short settle window right after).
            self.frames_while_speaking += 1
            return None

        self.audio_chunks.append(chunk)
        self.media_frames += 1
        return chunk

    def handle_stop(self) -> None:
        self.stop_received = True
        self.ended = True
        duration = 0.0
        if self.started_at is not None:
            duration = time.monotonic() - self.started_at
        total = self.total_audio_bytes
        logger.info(
            "event=stop call_id=...%s codec=%s media_frames=%d audio_bytes=%d "
            "(~%.1fs of audio, %.1fs wall, max frame gap %.2fs, echo_frames=%d)",
            self.call_id,
            self.encoding,
            self.media_frames,
            total,
            total / _BYTES_PER_A_LAW_SECOND,
            duration,
            self.max_frame_gap_sec,
            self.frames_while_speaking,
        )

    @property
    def total_audio_bytes(self) -> int:
        return sum(len(c) for c in self.audio_chunks)

    # -- recording --------------------------------------------------------------

    def save_recording(self, recordings_dir: Path) -> Path | None:
        """Decode buffered audio with the negotiated codec, write as WAV."""
        if not self.audio_chunks:
            logger.info("No audio received -- nothing to save.")
            return None
        return recordings_service.write_call_recording(
            recordings_dir,
            self.call_id,
            b"".join(self.audio_chunks),
            encoding=self.encoding,
        )

    def save_agent_recording(self, recordings_dir: Path) -> Path | None:
        """Save what the patient actually heard from us: agent_audio.wav.

        Comparing this against the live call tells apart "our audio was bad"
        (file sounds bad too) from "the call path distorted it" (file sounds
        fine). Same audio that the offline preview tool produces.
        """
        if not self.agent_pcm:
            return None
        path = Path(recordings_dir) / f"call_{self.call_id}" / "agent_audio.wav"
        return recordings_service.write_wav(path, bytes(self.agent_pcm))

@router.websocket("/media-stream")
async def media_stream(websocket: WebSocket) -> None:
    """Authenticate, wire the receiver + call-flow driver, and clean up."""
    settings = get_settings()
    token = websocket.query_params.get("token")

    if settings.media_stream_require_token and not media_auth.validate_token(token):
        logger.warning(
            "Rejected media-stream connection: missing/invalid/expired token from %s",
            websocket.client,
        )
        await websocket.close(code=1008, reason="invalid media stream token")
        return

    await websocket.accept()
    logger.info("Media stream connected (token accepted)")

    # Diagnostics: how long between POST /calls and the stream connecting.
    # ~30s = answered normally; 40s+ often means ring-no-answer routed to a
    # voicemail/announcement (seen live). Check the call status endpoint.
    stream_age = media_auth.token_age_seconds(token)
    if stream_age is not None:
        logger.info("Media stream connected %.1fs after the call was placed", stream_age)
        if stream_age > 60:
            logger.warning(
                "Long ring time (%.0fs) before the stream connected -- often means "
                "ring-no-answer routed to voicemail/announcement. Verify with "
                "GET /calls/<provider_call_id>/status and the Zernio dashboard.",
                stream_age,
            )

    session = MediaSession()

    inbox: asyncio.Queue = asyncio.Queue()
    receiver = asyncio.create_task(
        _receive_loop(websocket, session, inbox), name="media-receiver"
    )
    try:
        category = (media_auth.get_call_config(token) or {}).get(
            "diagnosis_category", "general"
        )
        await call_flow.run_call(
            websocket=websocket,
            inbox=inbox,
            session=session,
            category=category,
            settings=settings,
        )
    except call_flow.CallEnded as exc:
        logger.info("Call flow finished: %s", exc)
    except Exception:  # pragma: no cover - defensive: never leave the socket hanging
        logger.exception("Call flow crashed")
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
    finally:
        receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await receiver
        path = session.save_recording(settings.recordings_dir)
        agent_path = session.save_agent_recording(settings.recordings_dir)
        if token:
            media_auth.revoke_token(token)
        logger.info(
            "Media stream closed: frames=%d skipped_frames=%d recording=%s agent_audio=%s",
            session.media_frames,
            session.track_skipped,
            path.name if path else "none",
            agent_path.name if agent_path else "none",
        )


async def _receive_loop(websocket: WebSocket, session: MediaSession, inbox: asyncio.Queue) -> None:
    """The only reader of the socket. Queues ('event', data), ('media', bytes),
    ('closed', None) items for the call-flow driver."""
    try:
        while True:
            message = await websocket.receive()
            # Starlette versions differ: the disconnect may arrive as a
            # returned message (older) or as a raised WebSocketDisconnect
            # (newer). Handle both so the loop always terminates.
            if message["type"] == "websocket.disconnect":
                logger.info("Receiver: client disconnected (stop_received=%s)", session.stop_received)
                session.ended = True  # unblock the driver mid-sentence
                break

            raw = message.get("text")
            if raw is None and message.get("bytes") is not None:
                raw = message["bytes"].decode("utf-8", errors="replace")
            if not raw:
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Non-JSON frame (len=%d) ignored", len(raw))
                continue

            event = data.get("event")
            if event == "connected":
                session.handle_connected(data)
            elif event == "start":
                session.handle_start(data.get("start") or {})
            elif event == "media":
                chunk = session.accept_media(data.get("media") or {})
                if chunk:
                    await inbox.put(("media", chunk))
            elif event == "stop":
                session.handle_stop()
                await inbox.put(("event", data))
            else:
                logger.info("Unknown event=%r data=%s", event, data)
    except WebSocketDisconnect:
        logger.info("Receiver: client disconnected (stop_received=%s)", session.stop_received)
        session.ended = True
    finally:
        session.ended = True
        await inbox.put(("closed", None))


