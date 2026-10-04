"""
Central configuration for the VoiceCare backend.

Every setting is read from the single `.env` file in the backend root (one
env file per component, per the root README section 11), with normal
environment variables taking precedence. The real `.env` is git-ignored;
`.env.example` is the committed template with placeholder values.

Secrets (API keys) are never hard-coded and never logged.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """All runtime settings. Each field maps to an env var of the same name."""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Zernio telephony ---------------------------------------------------
    zernio_api_key: str = ""          # secret -- never logged
    from_number: str = ""             # the number Zernio dials from
    public_wss_url: str = ""          # wss://.../media-stream exposed via ngrok

    # -- Call script (survey-informed opening, see SURVEY_INSIGHTS.md) ------
    hospital_name: str = "VoiceCare"
    greeting_text: str = ""           # optional override of the default opening

    # -- Guard for the manual "call now" endpoint (POST /calls) -------------
    calls_api_key: str = ""           # required header: X-Api-Key

    # -- Safety rails ---------------------------------------------------------
    allowed_call_prefixes: str = "+94"  # comma-separated E.164 prefixes we may dial
    media_stream_require_token: bool = True
    test_to_number: str = ""            # used when POST /calls omits phone_number
    call_amd: bool = False              # Zernio answering-machine detection.
                                        # true = bridge deferred until human vs
                                        # machine is known (stops voicemail runs,
                                        # but may block the bridge if the callee
                                        # stays silent after answering -- seen
                                        # live). Keep false until provider
                                        # behavior is validated; per-call override
                                        # available on POST /calls.

    # -- Speech-to-text (faster-whisper, Step 2) ------------------------------
    stt_model: str = "base"           # tiny/base/small... ; first run downloads it
    stt_device: str = "cpu"           # "cpu" (or "cuda" on GPU machines)
    stt_compute_type: str = "int8"    # int8 is the fast choice for CPU
    stt_language: str = "en"          # Step 2 is English-first per the README plan

    # -- Text-to-speech + dialogue (Step 3) ------------------------------------
    tts_backend: str = "auto"         # auto | pyttsx3 | gtts (gtts needs ffmpeg)
    tts_rate: int = 160               # pyttsx3 rate (words/min); slower = clearer
    tts_voice: str = ""               # optional voice name substring, e.g.
                                      # "Zira" / "Hazel" / "David". Empty = system
                                      # default. Available names are logged at
                                      # startup so you can pick one.
    tts_bandpass_enabled: bool = True  # 300-3400 Hz telephony band + level match.
                                      # Filtering + levelling is what makes our
                                      # TTS sound like a normal phone voice
                                      # instead of harsh narrowband speech.
    speak_codec: str = "pcmu"         # codec for audio WE send to the patient.
                                      # Zernio docs: the bidirectional media codec
                                      # is fixed to PCMU (G.711 u-law) 8 kHz in
                                      # both directions, while the start event's
                                      # media_format describes the INBOUND track.
                                      # Encoding outbound frames with the inbound
                                      # codec distorts speech ("radio off-station").
                                      # pcmu (default) | pcma | auto (mirror inbound)
    filler_enabled: bool = True      # "One moment, please." before STT. Covers
                                      # the STT+TTS dead air so the patient is
                                      # never left in silence after answering.
                                      # The one thing the filler costs is its
                                      # own ~1 s of audio, which is still less
                                      # than the gap it hides.
    dialogue_enabled: bool = True     # false = Step 1 passthrough (record only)
    turn_silence_sec: float = 1.2     # silence that ends a spoken answer
    turn_max_sec: float = 10.0        # hard cap per answer (README: 8-10s)
    turn_gap_sec: float = 2.5         # no media frames for this long -> turn over
    final_answer_sec: float = 60.0    # fixed capture window for the final open
                                      # question: the patient may hang up when
                                      # done (the question tells them to); we
                                      # close the call either way after this.
    final_answer_patience_sec: float = 15.0
                                      # how long we wait for the FIRST sound in
                                      # that window before giving up on it. Once
                                      # the patient has said anything, silence
                                      # never ends the window early -- only the
                                      # full final_answer_sec does. Live 4 Oct
                                      # 2026: the window was cut after 2.5 s by
                                      # the frame-gap timeout and the bot talked
                                      # over the patient's answer.
    hangup_grace_sec: float = 60.0    # after the closing line we stop talking
                                      # and let the patient end their own call.
                                      # If they are still connected this long
                                      # after the goodbye, we hang up for them
                                      # (the line is billed either way).
    max_silent_attempts: int = 2      # how many times a question may go
                                      # unheard before we stop asking and close
                                      # politely. Without this cap, a patient we
                                      # cannot hear (background noise, muted
                                      # line, they put the phone down) got the
                                      # ENTIRE script fired at them in a few
                                      # seconds -- live bug, 2 Oct 2026.
    beep_enabled: bool = True         # beep before the final answer capture:
                                      # the question says "speak clearly after
                                      # the beep", so the beep is the cue.
    beep_frequency_hz: float = 1000.0  # telephony dial/alert tone (A through F)
    beep_duration_ms: float = 350.0    # per beep (two beeps with a short gap)

    # -- Cost rails (call-duration + rate limits) ------------------------------
    # Every answered minute is a billed minute, so the call driver enforces a
    # hard wall-clock cap and the dialing endpoint caps how often calls can be
    # placed. Both are in-process guards -- simple, no external state, and
    # enough for a single backend instance.
    max_call_duration_sec: float = 300.0   # hard wall-clock cap per call (5 min);
                                           # the dialogue aborts (assessment is
                                           # still logged) when exceeded
    max_concurrent_calls: int = 3          # streams allowed at once; 0 = unlimited
    max_calls_per_hour: int = 30           # POST /calls sliding-window limit;
                                           # 0 = unlimited
    max_calls_per_day: int = 200           # POST /calls daily limit; 0 = unlimited

    # -- Database (Step 6) ------------------------------------------------------
    # SQLAlchemy URL. Empty = the default SQLite file backend/voicecare.db.
    database_url: str = ""

    # -- Alerts (Step 7, reworked) ----------------------------------------------
    # A HIGH-risk call ALWAYS gets its alert text built and stored on the call
    # row; ALERT_DELIVERY decides whether that text is also pushed anywhere:
    #   whatsapp (default) = post the SHORT alert into the care team's WhatsApp
    #            conversation. Needs ZERNIO_ALERT_CONVERSATION_ID -- a WhatsApp
    #            window only lives 24 h, so `python get-info.py` opens/re-opens
    #            it (see backend/get-info.py) and prints the id for .env. If the
    #            conversation is missing the alert is still prepared and stored
    #            (status 'not_configured') and can be sent later from the
    #            dashboard's call detail ("Send alert now").
    #   ready             = prepare + store only; nothing leaves the backend
    alerts_enabled: bool = True
    alert_delivery: str = "whatsapp"       # whatsapp | ready
    # Sandbox WhatsApp credentials (only used when ALERT_DELIVERY=whatsapp).
    # Deliberately separate from FROM_NUMBER (the real toll-free voice line):
    # voice keeps dialing from the toll-free number, alerts ride the sandbox.
    #
    # The aliases matter: pydantic would otherwise map these fields to
    # INBOX_ACCOUNT_ID / ALERT_CONVERSATION_ID, while .env, .env.example and
    # every doc have always used the ZERNIO_-prefixed names -- so following the
    # documentation silently produced "WhatsApp not configured".
    inbox_account_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ZERNIO_INBOX_ACCOUNT_ID", "INBOX_ACCOUNT_ID", "inbox_account_id",
        ),
    )
    alert_conversation_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ZERNIO_ALERT_CONVERSATION_ID", "ALERT_CONVERSATION_ID",
            "alert_conversation_id",
        ),
    )

    # -- Scheduler (Step 8) ------------------------------------------------------
    # SCHEDULE_CALLS_ENABLED is the master switch.
    #   false (the default for this demo): the backend NEVER dials on its own.
    #       Every /schedule endpoint still reports the computed next-call time
    #       per patient, so the dashboard column is real data, not a mock.
    #   true: an in-process APScheduler job wakes every
    #       SCHEDULE_INTERVAL_MINUTES, finds the patients whose check-in is due
    #       and dials them through exactly the same guards as POST /calls
    #       (hourly/daily budget + concurrency cap + per-tick cap below), so the
    #       automatic path can never outspend the manual one.
    schedule_calls_enabled: bool = False
    schedule_checkin_days: str = "3,7,14,30"  # day offsets after discharge_date
    schedule_hour: int = 9                    # local hour the check-in is due
    schedule_minute: int = 0
    schedule_interval_minutes: int = 30       # how often the tick looks for due patients
    schedule_grace_days: int = 2              # catch-up window for an overdue slot
    schedule_max_dials_per_tick: int = 2      # hard cap per tick (cost rail)
    schedule_min_hours_between_calls: float = 24.0  # per-patient auto-dial cooldown: a patient dialed less than this many hours ago is skipped by the tick (manual + auto dials both start the window; manual calls are never blocked by it)
    schedule_dry_run: bool = False            # true = log the dial plan, dial nothing

    # -- Staff auth (Step 9, JWT) -------------------------------------------------
    # One shared dashboard, three roles (nurse | doctor | admin), no patient
    # accounts. Empty JWT_SECRET falls back to CALLS_API_KEY so a fresh demo
    # works; if both are empty, /auth/login refuses to issue tokens.
    jwt_secret: str = ""                  # secret -- never logged
    jwt_expires_minutes: int = 480
    jwt_issuer: str = "voicecare"
    admin_username: str = "admin"         # seeded on startup when no staff exist
    admin_password: str = ""              # required for the seed to happen
    admin_display_name: str = "System Administrator"
    default_hospital: str = "VoiceCare"
    # Browser origins allowed to call the API (the Vite dev server).
    cors_allow_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # -- Runtime ----------------------------------------------------------------
    recordings_dir: Path = BACKEND_DIR / "recordings"
    backend_host: str = "127.0.0.1"   # loopback only: ngrok connects from this machine
    backend_port: int = 8000
    log_level: str = "INFO"

    @property
    def allowed_prefixes(self) -> list[str]:
        return [p.strip() for p in self.allowed_call_prefixes.split(",") if p.strip()]

    @property
    def checkin_day_offsets(self) -> list[int]:
        """Day offsets after discharge that a check-in call is due on.

        Survey-informed (SURVEY_INSIGHTS.md): weekly-ish, not daily -- so the
        default is 3/7/14/30 days post-discharge rather than every day.
        """
        offsets: list[int] = []
        for part in self.schedule_checkin_days.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                continue
            if value >= 0:
                offsets.append(value)
        return sorted(set(offsets)) or [7]

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def signing_secret(self) -> str:
        """Secret used to sign staff JWTs.

        JWT_SECRET wins; an empty value falls back to CALLS_API_KEY so a fresh
        checkout can still log in. Both empty -> auth is disabled (503), never
        'open'.
        """
        return self.jwt_secret.strip() or self.calls_api_key.strip()

    @property
    def greeting(self) -> str:
        """Opening line of every call -- addresses privacy/trust up front,
        per the survey findings in SURVEY_INSIGHTS.md."""
        if self.greeting_text.strip():
            return self.greeting_text.strip()
        return (
            f"This is a short automated check-in call from {self.hospital_name}. "
            "Your answers are kept private and are only seen by your care team. "
            "This will take about two minutes."
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
