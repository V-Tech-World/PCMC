"""Preflight: prove this machine can place a working call BEFORE dialling.

Run it on ANY machine (old PC, new PC, after a zip transfer, after a config
change). It checks every environment-dependent piece the call pipeline relies
on -- the things that work on one PC and silently break on another:

  1. backend/.env exists and the required values are real (not placeholders)
  2. the ngrok tunnel is running and its URL matches PUBLIC_WSS_URL
     (the #1 "works on my machine" failure: a new PC runs ngrok with a
      different domain, or not at all, so Zernio cannot open the media
      WebSocket -- the greeting still plays on the provider's own audio path,
      then the call dies with no agent voice)
  3. DNS + end-to-end: https://<ngrok-host>/health actually reaches THIS
     backend through the public tunnel
  4. dependency sanity (av<14 pin, faster-whisper, pyttsx3, scipy, audioop)
  5. TTS really synthesises audio (Windows SAPI voices differ per machine)
  6. STT self-test passes (model cache is per-user, not inside the zip)
  7. Windows 1 ms timer, local /health, .venv usage

Run (from backend/):
    .venv\\Scripts\\python tools\\preflight.py
    .venv\\Scripts\\python tools\\preflight.py --quick   # skip TTS/STT

Exit code: 0 = ready to dial, 1 = at least one FAIL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")

_results: list[tuple[str, str, str]] = []  # (status, name, detail)


def _ok(name: str, detail: str = "") -> None:
    _results.append(("OK", name, detail))


def _warn(name: str, detail: str) -> None:
    _results.append(("WARN", name, detail))


def _fail(name: str, detail: str) -> None:
    _results.append(("FAIL", name, detail))


def _mask(secret: str) -> str:
    if not secret:
        return "<not set>"
    if len(secret) <= 12:
        return secret
    return f"{secret[:6]}...({len(secret)} chars)"


def _http_json(url: str, timeout: float, headers: dict | None = None) -> tuple[int, str]:
    """GET url, return (status_code, body_text). Raises on connection errors."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # server answered with 4xx/5xx
        return exc.code, exc.read().decode("utf-8", "replace")


# --------------------------------------------------------------------- checks


def check_env() -> str:
    """Config presence + placeholder detection. Returns the wss:// host."""
    env_path = BACKEND_DIR / ".env"
    if not env_path.is_file():
        _fail(".env", f"missing at {env_path} -- copy .env.example to .env and fill it in")
    else:
        _ok(".env", str(env_path))

    s = get_settings()

    if not s.zernio_api_key or "replace" in s.zernio_api_key.lower():
        _fail("ZERNIO_API_KEY", "not set (or still the .env.example placeholder)")
    else:
        _ok("ZERNIO_API_KEY", _mask(s.zernio_api_key))

    fn = s.from_number.strip()
    if not fn or "X" in fn.upper() or "replace" in fn.lower():
        _fail(
            "FROM_NUMBER",
            f"{fn or '<not set>'} is empty/placeholder -- Zernio will fall back to "
            "the account's default caller ID (this is why calls arrive from an "
            "unexpected number)",
        )
    elif not _E164_RE.match(fn):
        _fail("FROM_NUMBER", f"{fn} is not E.164 (expected e.g. +18888344640)")
    else:
        _ok("FROM_NUMBER", fn)

    if not s.calls_api_key or "change-me" in s.calls_api_key.lower():
        _fail("CALLS_API_KEY", "not set (or placeholder) -- POST /calls will refuse to dial")
    else:
        _ok("CALLS_API_KEY", _mask(s.calls_api_key))

    wss = s.public_wss_url.strip()
    host = ""
    if not wss:
        _fail("PUBLIC_WSS_URL", "not set -- ngrok URL goes here")
    elif not wss.startswith("wss://") or not wss.rstrip("/").endswith("/media-stream"):
        _fail("PUBLIC_WSS_URL", f"{wss} must start with wss:// and end with /media-stream")
    elif "YOUR-STATIC-DOMAIN" in wss.upper():
        _fail(
            "PUBLIC_WSS_URL",
            "still the .env.example placeholder -- put THIS machine's ngrok URL here",
        )
    else:
        host = wss.removeprefix("wss://").split("/", 1)[0]
        _ok("PUBLIC_WSS_URL", f"wss://{host}/media-stream")
    return host


def check_dns(host: str) -> bool:
    if not host:
        return False
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        _fail(
            "DNS",
            f"{host} does not resolve ({exc}) -- the ngrok domain is dead (old "
            "session or wrong machine). Start ngrok HERE and update PUBLIC_WSS_URL "
            "in .env, then restart the backend.",
        )
        return False
    _ok("DNS", f"{host} resolves")
    return True


def check_ngrok(host: str) -> None:
    """Compare the live ngrok tunnel with what .env tells Zernio to dial."""
    try:
        status, body = _http_json("http://127.0.0.1:4040/api/tunnels", timeout=2)
        if status != 200:
            _warn("ngrok API", f"127.0.0.1:4040 answered HTTP {status}")
            return
        tunnels = json.loads(body).get("tunnels") or []
    except (urllib.error.URLError, OSError, ValueError):
        _warn(
            "ngrok",
            "local API (127.0.0.1:4040) not reachable -- ngrok is NOT running on "
            "this machine. Calls will place, the greeting may play, then the line "
            "drops with no agent voice (Zernio cannot open the media stream).",
        )
        return

    urls = [t.get("public_url", "") for t in tunnels]
    if not urls:
        _warn("ngrok", "running but no tunnels -- start one: ngrok http 8000")
    elif host and any(u.startswith(f"https://{host}") for u in urls):
        _ok("ngrok", f"tunnel matches PUBLIC_WSS_URL ({urls[0]})")
    else:
        _fail(
            "ngrok",
            f"URL MISMATCH: .env says {host!r} but ngrok is serving {urls}. "
            "Update PUBLIC_WSS_URL in .env to that URL (https -> wss, + "
            "/media-stream) and restart the backend.",
        )


def check_local_backend() -> bool:
    try:
        status, body = _http_json("http://127.0.0.1:8000/health", timeout=3)
    except (urllib.error.URLError, OSError):
        _warn("backend", "not answering on 127.0.0.1:8000 (start uvicorn for live checks)")
        return False
    if status != 200:
        _warn("backend", f"/health answered HTTP {status}")
        return False
    try:
        data = json.loads(body)
    except ValueError:
        _warn("backend", "/health returned non-JSON")
        return False
    _ok(
        "backend",
        f"running v{data.get('version')}, telephony_configured="
        f"{data.get('telephony_configured')}, stt_pipeline_ok={data.get('stt_pipeline_ok')}",
    )
    return True


def check_tunnel_end_to_end(host: str, dns_ok: bool) -> None:
    """Reach /health THROUGH the public tunnel -- what Zernio's side sees."""
    if not host or not dns_ok:
        return
    url = f"https://{host}/health"
    try:
        status, body = _http_json(
            url,
            timeout=8,
            headers={
                "User-Agent": "voicecare-preflight/1.0",
                # ngrok's browser-protection page must not mask a real answer.
                "ngrok-skip-browser-warning": "1",
            },
        )
    except (urllib.error.URLError, OSError) as exc:
        _fail(
            "tunnel end-to-end",
            f"cannot reach {url} ({exc}) -- Zernio will hit the same wall: no media "
            "stream, no agent voice, call drops after the greeting",
        )
        return
    if status == 200 and "voicecare-backend" in body:
        _ok("tunnel end-to-end", f"{url} reached this backend through the public tunnel")
    elif status == 200 and ("ngrok" in body.lower() or "interstitial" in body.lower()):
        _warn(
            "tunnel end-to-end",
            f"{url} returned an ngrok notice page, not the backend -- the tunnel "
            "may target the wrong port (must forward to 127.0.0.1:8000)",
        )
    else:
        _fail(
            "tunnel end-to-end",
            f"{url} answered HTTP {status} -- the tunnel is not forwarding to this "
            "backend (target must be 127.0.0.1:8000)",
        )


def check_dependencies() -> None:
    try:
        import av

        major = int(av.__version__.split(".", 1)[0])
        if major >= 14:
            _fail(
                "av",
                f"av {av.__version__} installed -- av>=14 breaks faster-whisper on "
                "the FIRST patient answer. Reinstall requirements.txt (pins av<14).",
            )
        else:
            _ok("av", f"{av.__version__} (<14, correct)")
    except ImportError:
        _fail("av", "not installed -- pip install -r requirements.txt")

    for mod in ("faster_whisper", "pyttsx3", "numpy", "scipy", "audioop"):
        try:
            __import__(mod)
            _ok(mod, "importable")
        except ImportError as exc:
            _fail(mod, f"missing ({exc}) -- pip install -r requirements.txt")

    in_venv = getattr(sys, "prefix", "") != getattr(sys, "base_prefix", "")
    if in_venv:
        _ok("venv", f"active ({Path(sys.prefix).name})")
    else:
        _warn("venv", "NOT running inside .venv -- use .venv\\Scripts\\python")


def check_tts() -> None:
    try:
        from app.services.tts import Speaker

        speaker = Speaker()
        pcm = speaker.synthesize("Preflight test, one two three.")
        seconds = len(pcm) / 2 / 8000
        if seconds < 0.1:
            _fail("TTS", f"backend {speaker.backend} produced only {seconds:.2f}s of audio")
        else:
            _ok("TTS", f"backend {speaker.backend} -> {seconds:.2f}s of 8 kHz audio")
    except Exception as exc:  # noqa: BLE001 - report, do not crash the tool
        _fail(
            "TTS",
            f"{type(exc).__name__}: {exc} -- the agent would be SILENT on the call "
            "and the flow would crash right after the greeting",
        )


def check_stt() -> None:
    s = get_settings()
    # The model lives in the per-user HuggingFace cache, NOT inside the zip --
    # report where it stands before the slower self-test runs.
    if os.environ.get("HF_HUB_CACHE"):
        hub = Path(os.environ["HF_HUB_CACHE"])
    elif os.environ.get("HF_HOME"):
        hub = Path(os.environ["HF_HOME"]) / "hub"
    else:
        hub = Path.home() / ".cache" / "huggingface" / "hub"
    cache = hub / f"models--Systran--faster-whisper-{s.stt_model}"
    if cache.is_dir():
        _ok("STT model cache", f"{s.stt_model} already downloaded ({cache})")
    else:
        _warn(
            "STT model cache",
            f"{s.stt_model} not cached yet -- first run downloads ~75 MB "
            "(needs internet; the boot self-test will fetch it)",
        )

    from app.api.health import verify_stt_pipeline

    ok, detail = verify_stt_pipeline()
    if ok:
        _ok("STT self-test", "model loaded and inference ran")
    else:
        _fail(
            "STT self-test",
            f"{detail} -- calls would crash on the FIRST answer (after the "
            "greeting). Check av<14 in requirements.txt.",
        )


def check_timer() -> None:
    from app.core.timing import _IS_WINDOWS, acquire_fine_timer, release_fine_timer

    if not _IS_WINDOWS:
        _ok("timer", "non-Windows platform (not needed)")
        return
    if acquire_fine_timer():
        _ok("timer", "1 ms Windows timer acquired (audio pacing will not drift)")
    else:
        _warn("timer", "could not raise the Windows timer -- spoken audio may stretch")
    release_fine_timer()



def main() -> int:
    parser = argparse.ArgumentParser(description="VoiceCare environment preflight")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="skip the TTS synthesis and STT self-test (no model load, no SAPI)",
    )
    args = parser.parse_args()

    print("VoiceCare preflight")
    print("=" * 72)

    host = check_env()
    dns_ok = check_dns(host)
    check_ngrok(host)
    backend_up = check_local_backend()
    check_tunnel_end_to_end(host, dns_ok)
    check_dependencies()
    check_timer()
    if not args.quick:
        check_tts()
        check_stt()

    print("-" * 72)
    width = max((len(name) for _, name, _ in _results), default=10)
    fails = 0
    for status, name, detail in _results:
        if status == "FAIL":
            fails += 1
        tag = {"OK": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[status]
        print(f"{tag} {name.ljust(width)}  {detail}")
    print("-" * 72)
    if fails:
        print(f"{fails} FAIL(s) -- fix these before dialling. The ngrok/")
        print("PUBLIC_WSS_URL items are the usual cause of 'greeting plays, then")
        print("the call drops with no agent voice' on a freshly copied machine.")
        return 1
    if not backend_up:
        print("Note: backend was not running; live checks were skipped.")
    print("Ready: environment looks good for a live call.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
