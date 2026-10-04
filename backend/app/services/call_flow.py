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
from datetime import datetime, timezone

from fastapi import WebSocketDisconnect

from app.core.config import Settings, get_settings
from app.db import service as db_service
from app.services import alerts as alerts_service
from app.services import email_alerts as email_alerts_service
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
# A capture window open at least this long, with less audio than the minimum
# below, means the media leg delivered nothing (live 4 Oct 2026).
_DEAD_WINDOW_SEC = 3.0
_DEAD_WINDOW_AUDIO_SEC = 0.25


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
    to_number: str = "",
    patient_code: str | None = None,
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

    call_started_at = datetime.now(timezone.utc)

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
    # Cost rail (Step 5): hard wall-clock cap. Every answered minute is a
    # billed provider minute, so the dialogue must never run past this even
    # if the patient never hangs up and the turns keep flowing.
    # 0 (or negative) disables the cap.
    cap = settings.max_call_duration_sec
    call_deadline = time.monotonic() + cap if cap > 0 else float("inf")

    try:
        silent_attempts = 0
        while question is not None:
            turn_no += 1
            is_final = question.id == FINAL_QUESTION_ID
            if time.monotonic() >= call_deadline:
                # Cost rail: the call hit MAX_CALL_DURATION_SEC. Score and log
                # what was captured so far, then end the call -- we never keep
                # a billed line open past the cap.
                logger.warning(
                    "Max call duration (%.0fs) reached at turn=%d -- ending call",
                    settings.max_call_duration_sec, turn_no,
                )
                _log_assessment(dialogue.assess_risk())
                raise CallEnded("max call duration reached")
            await speak(
                websocket, question.text, session, speaker, tx_codec,
                what=f"question {turn_no} ({question.id})",
            )
            if is_final:
                # The question promises "speak clearly after the beep", so the
                # beep has to exist -- and it is played BEFORE the capture
                # window opens, so the patient hears the cue and then starts.
                await beep(websocket, session, speaker, tx_codec, settings)
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
                # One polite repeat, then stop. If they still cannot be heard,
                # do NOT keep walking down the script -- live bug, 2 Oct 2026: a
                # patient we could not hear was getting medication -> pain ->
                # category -> final in a few seconds. Close the call politely
                # instead, counting every SILENT CAPTURE (the repeat counts
                # too, otherwise the cap could never be reached).
                max_attempts = max(1, settings.max_silent_attempts)
                silent_attempts += 1
                if silent_attempts < max_attempts:
                    logger.info("turn=%d: no speech detected, repeating question=%s",
                                turn_no, question.id)
                    await speak(
                        websocket,
                        f"Sorry, I did not hear that. {question.text}",
                        session,
                        speaker,
                        tx_codec,
                        what="the repeated question",
                    )
                    pcm, had_speech = await capture_turn(inbox, session, settings)
                    if not had_speech:
                        silent_attempts += 1
                if silent_attempts >= max_attempts:
                    logger.warning(
                        "No patient speech after %d attempt(s) on %s (frames=%d) "
                        "-- closing politely instead of asking the rest of the "
                        "script",
                        silent_attempts, question.id, session.media_frames,
                    )
                    await speak(
                        websocket, NO_SPEECH_CLOSING_TEXT, session, speaker,
                        tx_codec, what="the no-audio goodbye",
                    )
                    raise CallEnded(
                        f"no patient audio after {silent_attempts} attempts"
                    )

            if not had_speech and session.stop_received:
                # Patient hung up without answering this turn. Earlier answers
                # (if any) are still scored and logged before the call ends.
                _log_assessment(dialogue.assess_risk())
                logger.info("Call ended by patient during turn=%d (no answer captured)", turn_no)
                raise CallEnded("call ended by patient mid-turn")

            transcript = ""
            leg_died: CallEnded | None = None
            if had_speech:
                silent_attempts = 0
                if settings.filler_enabled:
                    # Instant filler while STT runs (README section 4). On by
                    # default: live testing showed the patient sitting in dead
                    # air for a second or two after answering, which reads as
                    # "did it hear me?" -- so we cover the gap with a voice.
                    #
                    # A dead leg must NOT cost us the answer we already have
                    # (live 2 Oct 2026: ~11 s of a patient's final answer --
                    # "severe headache" -- was captured, then thrown away
                    # because the provider closed the leg while we were saying
                    # "One moment, please."). The filler is a courtesy; the
                    # transcript is the product. Transcribe and record first,
                    # then end the call.
                    try:
                        await send_pcm(
                            websocket, speaker.filler(), tx_codec, session,
                            pace=False, what="the filler",
                        )
                    except CallEnded as exc:
                        leg_died = exc
                        logger.warning(
                            "Leg closed while sending the filler (turn=%d) -- "
                            "keeping the captured answer", turn_no,
                        )
                transcript = await _transcribe_turn(
                    pcm, turn_no, question.id, turn_dir, transcriber
                )
                if session.stop_received and leg_died is None:
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

            if leg_died is not None:
                # The answer is transcribed, recorded and scored (and the row +
                # alert are about to be persisted by the CallEnded handler) --
                # only now do we stop talking.
                raise leg_died

            # Step 5 TC2: the risk level changes the next spoken response --
            # a red-flag answer is acknowledged once, before the next question
            # (and the closing becomes urgent, see dialogue.closing_text).
            ack = dialogue.pop_urgent_acknowledgment()
            if question is not None and ack:
                await speak(
                    websocket, ack, session, speaker, tx_codec,
                    what="the urgent acknowledgment",
                )

        closing_reason = "dialogue finished"
        if not session.stop_received:
            await speak(
                websocket, dialogue.closing_text, session, speaker, tx_codec,
                what="the closing line",
            )
            # Say nothing more: the patient ends their own call. We only take
            # the line back if they are still on it after HANGUP_GRACE_SEC.
            closing_reason = f"dialogue finished -- {await wait_for_hangup(session, settings)}"

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
        raise CallEnded(closing_reason)
    except CallEnded as exc:
        # Step 6 + 7: persist the row and prepare the HIGH-risk alert message
        # on EVERY exit path -- normal finish, hangup, max-duration cap.
        await _persist_and_alert(
            dialogue, assessment=None, reason=str(exc), session=session,
            settings=settings, to_number=to_number, patient_code=patient_code,
            started_at=call_started_at,
        )
        raise
    except asyncio.CancelledError:
        raise


async def _persist_and_alert(
    dialogue,
    *,
    assessment,
    reason: str,
    session,
    settings: Settings,
    to_number: str,
    patient_code: str | None,
    started_at: datetime,
) -> None:
    """Save the CallRecord row and prepare the HIGH-risk alert (Step 7).

    Step 7 rework: the alert text is always built for a HIGH-risk call and
    stored on the row (status 'ready' unless ALERT_DELIVERY=whatsapp), so the
    care team's message is ready in the dashboard even with no delivery channel
    configured. Never raises: a persistence/alert problem must not mask the
    call flow's own CallEnded, and the risk decision is also in the logs.
    """
    try:
        assessment = assessment or dialogue.assess_risk()
        duration_sec = (datetime.now(timezone.utc) - started_at).total_seconds()
        record = await asyncio.to_thread(
            db_service.record_call,
            dialogue=dialogue,
            assessment=assessment,
            provider_call_id=getattr(session, "provider_call_id", "") or session.call_id,
            to_number=to_number or getattr(session, "dialed_number", ""),
            patient_code=patient_code,
            ended_reason=reason,
            started_at=started_at,
            duration_sec=duration_sec,
            database_url=settings.database_url,
        )
        if record is None:
            return
        outcome = await asyncio.to_thread(
            alerts_service.prepare_alert, record, None, settings
        )
        # Second channel (4 Oct 2026): a targeted email to the score-routed
        # staff. Runs regardless of ALERT_DELIVERY -- that switch only governs
        # the WhatsApp transport -- and never raises, so a dead mailbox cannot
        # hide the alert text or stop the WhatsApp path.
        recipients = await asyncio.to_thread(
            email_alerts_service.deliver_alert_emails, record, None, settings
        )
        await asyncio.to_thread(
            db_service.attach_alert,
            record.id,
            outcome.status,
            detail=outcome.detail,
            message=outcome.message,
            recipients=recipients,
            database_url=settings.database_url,
        )
        logger.info(
            "Alert for record %d: status=%s (%s) -- %d-char message stored, "
            "%d email recipient(s)",
            record.id,
            outcome.status,
            outcome.detail or "no detail",
            len(outcome.message),
            len(recipients),
        )
    except Exception:
        logger.exception("Persist/alert step failed (risk decision is in the logs)")


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


def _log_assessment(assessment) -> None:
    """Log a risk assessment consistently (final + hangup/timeout paths)."""
    logger.info(
        "Risk assessment: level=%s score=%.0f",
        assessment.risk_level, assessment.score,
    )
    for reason in assessment.reasons:
        logger.info("  risk: %s", reason)


async def _drain_until_closed(inbox: asyncio.Queue) -> None:
    """Dialogue disabled: consume queue items until the stream closes."""
    while True:
        kind, _item = await inbox.get()
        if kind == "closed":
            return


async def speak(
    websocket, text: str, session, speaker, tx_codec: str | None = None,
    what: str = "a question",
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
    await send_pcm(websocket, pcm, codec, session, pace=True, what=what)


#: Said when we still cannot hear the patient after MAX_SILENT_ATTEMPTS
#: attempts at one question. Closing politely beats walking them down the rest
#: of the script while they are clearly not there (live bug, 2 Oct 2026).
NO_SPEECH_CLOSING_TEXT = (
    "Sorry, I cannot hear you. I will end this call now. Goodbye."
)


async def beep(
    websocket, session, speaker, tx_codec: str | None = None,
    settings: Settings | None = None,
) -> bool:
    """Play the "speak now" tone that the final question promises.

    Two short 1 kHz beeps with a gap, faded at both ends. Goes through
    `send_pcm`, so it is paced like speech, marks the session as speaking (the
    echo of our own tone is discarded), lands in agent_audio.wav for offline
    diagnosis, and aborts cleanly if the patient hangs up mid-tone.

    Returns True if a tone was actually sent.
    """
    settings = settings or get_settings()
    if not settings.beep_enabled or settings.beep_duration_ms <= 0:
        return False
    codec = tx_codec or session.encoding
    pcm = recordings_service.beep_pcm16(
        frequency_hz=settings.beep_frequency_hz,
        duration_ms=settings.beep_duration_ms,
    )
    if not pcm:
        return False
    logger.info(
        "Playing %.0f Hz beep (%.2fs of audio) -- the patient's cue to speak",
        settings.beep_frequency_hz, len(pcm) / 2 / 8000,
    )
    await send_pcm(websocket, pcm, codec, session, pace=True, what="the beep")
    return True


async def send_pcm(
    websocket, pcm16: bytes, encoding: str, session, pace: bool = True,
    what: str = "audio",
) -> None:
    """Send PCM16 audio back to the provider as base64 media frames.

    Pacing is clock-based: frame n is scheduled at start + n*20 ms, so if a
    send takes longer than 20 ms the next frames catch up in a burst instead
    of drifting slower and slower (drift stretches the audio -- a second
    contributor to the 'underwater' sound heard live).

    Aborts promptly with CallEnded if the patient hangs up mid-sentence. `what`
    names the audio so `ended_reason` says where the leg died ("stream closed
    while speaking the final question" vs "... while playing the beep") --
    without it, a call that dies 1 s after the last question is undiagnosable
    from the database alone.
    """
    if len(pcm16) % 2:  # codec helpers require whole 16-bit frames
        pcm16 = pcm16[:-1]
    session.speaking = True
    started = time.monotonic()
    frame = 0
    try:
        for offset in range(0, len(pcm16), _PCM_BYTES_PER_20MS):
            if session.ended:
                raise CallEnded(f"stream closed while playing {what}")
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
                raise CallEnded(
                    f"stream closed while playing {what}: {exc}"
                ) from exc
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
    started = last_frame
    frames = 0

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

        frames += 1
        pcm.extend(recordings_service.decode_chunk(session.encoding, item))
        state = detector.feed(bytes(pcm[-_PCM_BYTES_PER_20MS * 2:]))
        if state == "ended":
            break

    _log_capture_window("turn", started, frames, len(pcm), detector.has_speech)
    return bytes(pcm), detector.has_speech


def _log_capture_window(
    what: str, started: float, frames: int, pcm_bytes: int, had_speech: bool
) -> None:
    """Say how much audio a capture window ACTUALLY collected.

    Live 4 Oct 2026: three of five answers were empty and the log only ever
    said "no speech detected", which reads as "the patient did not answer" --
    the one conclusion the evidence did not support. A window that stayed open
    for seconds and received (almost) no frames is a transport failure, and it
    now says so at WARNING level.
    """
    window = time.monotonic() - started
    audio_sec = pcm_bytes / 2 / 8000
    logger.info(
        "Capture window (%s): %.1fs open, %d frames, %.1fs of audio, speech=%s",
        what, window, frames, audio_sec, had_speech,
    )
    if window >= _DEAD_WINDOW_SEC and audio_sec < _DEAD_WINDOW_AUDIO_SEC:
        logger.warning(
            "Capture window (%s) was open %.1fs but received %.1fs of audio -- "
            "the media leg is not delivering speech right now.",
            what, window, audio_sec,
        )



async def capture_final_answer(
    inbox: asyncio.Queue, session, settings: Settings
) -> tuple[bytes, bool]:
    """Collect the final open-ended answer in a fixed window.

    Unlike capture_turn, this uses FINAL_ANSWER_SEC (not the silence
    detector) so a patient who pauses mid-sentence is not cut off. The
    call is closed after the window either way.
    """
    deadline = time.monotonic() + settings.final_answer_sec
    # The patient needs room to think before they start. Live 4 Oct 2026: the
    # window used to end after TURN_GAP_SEC (2.5 s) of no frames, so the bot
    # started talking again while the patient was still formulating their
    # answer -- on the very question that carries free-text symptoms, and the
    # one they are slowest to answer.
    #
    # Two rules now:
    #   - before ANY sound: give up after final_answer_patience_sec (a patient
    #     who is not going to answer should not hold a billed line open for a
    #     whole minute);
    #   - after ANY sound: silence NEVER ends the window early. They may pause
    #     mid-sentence; we only stop at the full final_answer_sec.
    first_sound_deadline = time.monotonic() + min(
        settings.final_answer_patience_sec, settings.final_answer_sec
    )
    pcm = bytearray()
    last_frame = time.monotonic()
    started = last_frame
    had_speech = False
    frames = 0

    while time.monotonic() < deadline:
        if session.ended:
            break
        now = time.monotonic()
        if not had_speech and now >= first_sound_deadline:
            logger.info(
                "Final answer: nothing heard in %.0fs -- closing the window",
                settings.final_answer_patience_sec,
            )
            break
        remaining = deadline - now
        remaining_gap = settings.turn_gap_sec - (now - last_frame)
        try:
            kind, item = await asyncio.wait_for(
                inbox.get(), timeout=max(0.05, min(remaining, remaining_gap))
            )
        except asyncio.TimeoutError:
            # Silence: keep waiting (see the rules above). Only the two
            # deadlines above, or the stream ending, can close this window.
            continue

        last_frame = time.monotonic()
        if kind == "closed":
            raise CallEnded("stream closed during final answer")
        if kind == "event":  # 'stop' -> the provider ended the call
            break
        if session.speaking:
            continue  # our own voice; not patient audio

        frames += 1
        chunk = recordings_service.decode_chunk(session.encoding, item)
        if chunk:
            pcm.extend(chunk)
            had_speech = True

    _log_capture_window("final answer", started, frames, len(pcm), had_speech)
    return bytes(pcm), had_speech


async def wait_for_hangup(session, settings: Settings) -> str:
    """After the goodbye: say nothing, and let the patient end the call.

    The closing line used to end the call on the spot, and it also told the
    patient to phone the hospital -- advice the care team owns, and on a
    post-discharge follow-up it is actively wrong (they have already called).
    The patient now hangs up in their own time; if they are still connected
    HANGUP_GRACE_SEC after the goodbye we hang up for them, because the line is
    billed either way and holding it open serves nobody.

    Returns a short phrase for the log / `ended_reason`.
    """
    grace = max(0.0, settings.hangup_grace_sec)
    if grace <= 0:
        return "closed immediately after the closing line"
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if session.stop_received or session.ended:
            waited = grace - (deadline - time.monotonic())
            logger.info("Patient ended the call themselves after %.1fs", waited)
            return f"patient hung up {waited:.0f}s after the closing line"
        await asyncio.sleep(0.25)
    logger.info(
        "Patient still connected %.0fs after the goodbye -- hanging up for them",
        grace,
    )
    return f"auto-disconnected {grace:.0f}s after the closing line"
