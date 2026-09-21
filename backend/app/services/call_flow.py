"""
Call flow driver (Step 3): speaks questions, captures each answer turn,
transcribes it, and advances the dialogue -- one question at a time.

Runs inside the /media-stream WebSocket handler. The socket is used by two
coroutines: the receiver loop (api layer) only *reads* and feeds an inbox
queue; this driver only *sends*. Media frames arriving while the system is
speaking are dropped (they are echo of our own voice through the patient's
speaker).

Latency trick from README section 4: right after the patient stops
talking, a cached filler ("One moment, please.") is sent instantly, and
the slower STT runs in a worker thread in the meantime.

Per-turn recordings go to recordings/call_<id>/turn_<n>_<question>.wav
(Step 3 TC2: each turn captured separately; the whole-call WAV is also
kept, minus the parts while we were speaking).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time

from fastapi import WebSocketDisconnect

from app.core.config import Settings, get_settings
from app.services import recordings as recordings_service
from app.services import stt as stt_service
from app.services import tts as tts_service
from app.services.dialogue import FINAL_QUESTION_ID, CallDialogue
from app.services.turn_detector import TurnConfig, TurnDetector

logger = logging.getLogger("voicecare.call_flow")

# 20 ms of 8 kHz mono audio: 160 samples = 320 bytes of PCM16 = 160 codec bytes.
_PCM_BYTES_PER_20MS = 320
_SEND_FRAME_SECONDS = 0.02
_ECHO_SETTLE_SECONDS = 0.35  # ignore mic input briefly after we finish speaking


class CallEnded(RuntimeError):
    """Raised when the call flow finishes (normally or via disconnect)."""


def resolve_tx_codec(session, settings: Settings) -> str:
    """Codec used for audio WE send (agent -> patient).

    Two different codecs exist in this protocol and conflating them distorts
    speech:
    - ``session.encoding`` is the INBOUND track's codec from the `start` event
      (what the patient's audio arrives as).
    - The provider's bidirectional (outbound) media codec is a separate,
      documented setting: Zernio's AI-agents docs fix it to PCMU (G.711 u-law)
      at 8 kHz in both directions.

    Encoding our questions with the inbound codec is what made them sound like
    "a radio off its frequency" on a live call. ``SPEAK_CODEC=auto`` mirrors the
    inbound codec instead (kept for A/B testing against the provider).
    """
    preference = (settings.speak_codec or "pcmu").strip().lower()
    if preference == "auto":
        return session.encoding
    if preference in ("pcma", "alaw", "a-law"):
        return "PCMA"
    if preference in ("pcmu", "ulaw", "u-law"):
        return "PCMU"
    logger.warning(
        "Unknown SPEAK_CODEC=%r -- using PCMU (provider's bidirectional codec)",
        settings.speak_codec,
    )
    return "PCMU"


async def run_call(
    websocket,
    inbox: asyncio.Queue,
    session,
    category: str,
    settings: Settings | None = None,
) -> None:
    """Run the structured dialogue over the live media stream.

    Raises CallEnded when the flow completes or the stream dies.
    """
    settings = settings or get_settings()

    if not settings.dialogue_enabled:
        # Step 1/2 passthrough mode: the receiver keeps buffering; just wait
        # for the stream to end and let the api layer save the recording.
        await _drain_until_closed(inbox)
        return

    speaker = tts_service.get_speaker()
    transcriber = stt_service.get_transcriber()
    dialogue = CallDialogue(diagnosis_category=category)

    # Wait for the provider's `start` event so we know the call id/codec.
    deadline = time.monotonic() + 10
    while session.call_id == "unknown" and time.monotonic() < deadline:
        await asyncio.sleep(0.05)

    # Inbound codec (patient -> us) vs outbound codec (us -> patient). These are
    # NOT the same setting in this protocol; see resolve_tx_codec.
    tx_codec = resolve_tx_codec(session, settings)
    logger.info(
        "Audio codecs: inbound=%s outbound=%s (SPEAK_CODEC=%s)",
        session.encoding,
        tx_codec,
        settings.speak_codec,
    )

    question = dialogue.start()
    turn_dir = settings.recordings_dir / f"call_{session.call_id}"
    turn_no = 0

    try:
        while question is not None:
            turn_no += 1
            is_final = question.id == FINAL_QUESTION_ID
            await speak(websocket, question.text, session, speaker, tx_codec)
            if is_final:
                # Final open question: fixed window instead of the silence
                # detector. Patients ramble and think mid-sentence, and the
                # question itself tells them to hang up when done -- so we
                # record for FINAL_ANSWER_SEC and close the call either way.
                pcm, had_speech = await capture_final_answer(
                    inbox, session, settings
                )
            else:
                pcm, had_speech = await capture_turn(inbox, session, settings)

            if not had_speech and not is_final and not session.stop_received:
                # One polite repeat before moving on (still one question at a time).
                logger.info("turn=%d: no speech detected, repeating question=%s",
                            turn_no, question.id)
                await speak(
                    websocket,
                    f"Sorry, I did not hear that. {question.text}",
                    session,
                    speaker,
                    tx_codec,
                )
                pcm, had_speech = await capture_turn(inbox, session, settings)

            if not had_speech and session.stop_received:
                # Patient hung up without answering this turn. Earlier answers
                # (if any) are still scored and logged before the call ends.
                _log_assessment(dialogue.assess_risk())
                logger.info("Call ended by patient during turn=%d (no answer captured)", turn_no)
                raise CallEnded("call ended by patient mid-turn")

            transcript = ""
            if had_speech:
                if settings.filler_enabled:
                    # Instant filler while STT runs (README section 4). Off by
                    # default: with the base model STT takes <1 s, so it would
                    # just delay the next question.
                    await send_pcm(
                        websocket, speaker.filler(), tx_codec, session, pace=False
                    )
                transcript = await _transcribe_turn(
                    pcm, turn_no, question.id, turn_dir, transcriber
                )
                if session.stop_received:
                    # Patient hung up while/after answering: keep the captured
                    # data, then stop the call instead of asking the next question.
                    dialogue.record_answer(transcript)
                    _log_assessment(dialogue.assess_risk())
                    logger.info(
                        "Call ended by patient after turn=%d (%d answers captured)",
                        turn_no, len(dialogue.answers),
                    )
                    raise CallEnded("call ended by patient after answer")

            if turn_no >= 2 and session.media_frames == 0 and not pcm:
                # Stream connected but literally no patient audio arrived in
                # two full turns: likely an unanswered call routed somewhere
                # silent. Don't keep talking to nobody.
                logger.warning(
                    "No patient audio after %d turns (frames=%d) -- aborting dialogue "
                    "(likely unanswered call). Check GET /calls/<provider_call_id>/status.",
                    turn_no, session.media_frames,
                )
                raise CallEnded("no patient audio -- likely unanswered call")

            question = dialogue.record_answer(transcript)

            # Step 5 TC1: score the answer the moment it is transcribed.
            # The rule-based NLP is sub-millisecond, so this adds no dead air
            # -- the next question starts right after STT, as before.
            assessment = dialogue.assess_risk()
            logger.info(
                "Running risk after turn=%d: level=%s score=%.0f",
                turn_no, assessment.risk_level, assessment.score,
            )

            # Step 5 TC2: the risk level changes the next spoken response --
            # a red-flag answer is acknowledged once, before the next question
            # (and the closing becomes urgent, see dialogue.closing_text).
            ack = dialogue.pop_urgent_acknowledgment()
            if question is not None and ack:
                await speak(websocket, ack, session, speaker, tx_codec)

        if not session.stop_received:
            await speak(websocket, dialogue.closing_text, session, speaker, tx_codec)

        # Step 4: run the NLP engine + risk scorer over the full conversation.
        assessment = dialogue.assess_risk()
        logger.info(
            "Risk assessment: level=%s score=%.0f",
            assessment.risk_level, assessment.score,
        )
        for reason in assessment.reasons:
            logger.info("  risk: %s", reason)

        logger.info("Call complete: %d answers, category=%s",
                    len(dialogue.answers), dialogue.diagnosis_category)
        for entry in dialogue.summary():
            logger.info("  %s = %r (interpretation=%r)",
                        entry["question_id"], entry["transcript"], entry["interpretation"])
        raise CallEnded("dialogue finished")
    except CallEnded:
        raise
    except asyncio.CancelledError:
        raise


# ------------------------------------------------------------------ helpers


async def _transcribe_turn(
    pcm: bytes, turn_no: int, question_id: str, turn_dir, transcriber
) -> str:
    """Save one turn's audio separately (Step 3 TC2) and transcribe it."""
    wav_path = turn_dir / f"turn_{turn_no:02d}_{question_id}.wav"
    recordings_service.write_wav(wav_path, bytes(pcm))
    result = await asyncio.to_thread(transcriber.transcribe, wav_path)
    logger.info("turn=%d question=%s transcript=%r", turn_no, question_id, result.text)
    return result.text


async def _drain_until_closed(inbox: asyncio.Queue) -> None:
    """Dialogue disabled: consume queue items until the stream closes."""
    while True:
        kind, _item = await inbox.get()
        if kind == "closed":
            return


async def speak(
    websocket, text: str, session, speaker, tx_codec: str | None = None
) -> None:
    """Synthesize and stream a sentence to the call at real-time pace."""
    codec = tx_codec or session.encoding
    pcm = await asyncio.to_thread(speaker.synthesize, text)
    logger.info(
        "Speaking %d chars as %.1fs of %s audio: %r",
        len(text),
        len(pcm) / 2 / 8000,
        codec,
        text if len(text) <= 60 else text[:57] + "...",
    )
    await send_pcm(websocket, pcm, codec, session, pace=True)


async def send_pcm(
    websocket, pcm16: bytes, encoding: str, session, pace: bool = True
) -> None:
    """Send PCM16 audio back to the provider as base64 media frames.

    Pacing is clock-based: frame n is scheduled at start + n*20 ms, so if a
    send takes longer than 20 ms the next frames catch up in a burst instead
    of drifting slower and slower (drift stretches the audio -- a second
    contributor to the 'underwater' sound heard live).

    Aborts promptly with CallEnded if the patient hangs up mid-sentence.
    """
    if len(pcm16) % 2:  # codec helpers require whole 16-bit frames
        pcm16 = pcm16[:-1]
    session.speaking = True
    started = time.monotonic()
    frame = 0
    try:
        for offset in range(0, len(pcm16), _PCM_BYTES_PER_20MS):
            if session.ended:
                raise CallEnded("stream closed while speaking")
            piece = pcm16[offset:offset + _PCM_BYTES_PER_20MS]
            compressed = recordings_service.pcm16_to_codec(piece, encoding)
            # Keep a copy of exactly what we transmitted so a bad-sounding call
            # can be diagnosed offline (recordings/call_<id>/agent_audio.wav).
            tx_buffer = getattr(session, "agent_pcm", None)
            if tx_buffer is not None:
                tx_buffer.extend(piece)
            try:
                await websocket.send_json({
                    "event": "media",
                    "media": {"payload": base64.b64encode(compressed).decode()},
                })
            except (WebSocketDisconnect, RuntimeError) as exc:
                # A dead socket can surface as either; both mean hangup.
                raise CallEnded(f"stream closed while speaking: {exc}") from exc
            frame += 1
            if pace:
                delay = started + frame * _SEND_FRAME_SECONDS - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
    finally:
        session.speaking = False
        if not session.ended:
            # Echo-settle window: drop mic input briefly so the tail of our
            # own voice isn't captured as the start of the patient's turn.
            session.settle_until = time.monotonic() + _ECHO_SETTLE_SECONDS



async def capture_turn(
    inbox: asyncio.Queue, session, settings: Settings
) -> tuple[bytes, bool]:
    """Collect one answer turn from the inbox. Returns (pcm16, had_speech)."""
    detector = TurnDetector(TurnConfig(
        silence_duration_sec=settings.turn_silence_sec,
        max_turn_sec=settings.turn_max_sec,
    ))
    pcm = bytearray()
    last_frame = time.monotonic()

    while True:
        remaining_gap = settings.turn_gap_sec - (time.monotonic() - last_frame)
        try:
            kind, item = await asyncio.wait_for(
                inbox.get(), timeout=max(0.05, remaining_gap)
            )
        except asyncio.TimeoutError:
            logger.debug("capture_turn: no frames for %.1fs -> turn over",
                         settings.turn_gap_sec)
            break

        last_frame = time.monotonic()
        if kind == "closed":
            raise CallEnded("stream closed during a turn")
        if kind == "event":  # 'stop' -> the provider ended the call
            break
        if session.speaking:
            continue  # our own voice; not patient audio

        pcm.extend(recordings_service.decode_chunk(session.encoding, item))
        state = detector.feed(bytes(pcm[-_PCM_BYTES_PER_20MS * 2:]))
        if state == "ended":
            break

    return bytes(pcm), detector.has_speech



async def capture_final_answer(
    inbox: asyncio.Queue, session, settings: Settings
) -> tuple[bytes, bool]:
    """Collect the final open-ended answer in a fixed window.

    Unlike capture_turn, this uses FINAL_ANSWER_SEC (not the silence
    detector) so a patient who pauses mid-sentence is not cut off. The
    call is closed after the window either way.
    """
    deadline = time.monotonic() + settings.final_answer_sec
    pcm = bytearray()
    last_frame = time.monotonic()
    had_speech = False

    while time.monotonic() < deadline:
        remaining_gap = settings.turn_gap_sec - (time.monotonic() - last_frame)
        try:
            kind, item = await asyncio.wait_for(
                inbox.get(), timeout=max(0.05, remaining_gap)
            )
        except asyncio.TimeoutError:
            break

        last_frame = time.monotonic()
        if kind == "closed":
            raise CallEnded("stream closed during final answer")
        if kind == "event":  # 'stop' -> the provider ended the call
            break
        if session.speaking:
            continue  # our own voice; not patient audio

        chunk = recordings_service.decode_chunk(session.encoding, item)
        if chunk:
            pcm.extend(chunk)
            had_speech = True

    return bytes(pcm), had_speech
