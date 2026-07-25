"""Voice-to-text transcription dispatcher.

Backend chosen at runtime via VOICE_BACKEND env var:
  - "auto":   Apple Speech on Darwin if PyObjC bindings are installed,
              else whisper.cpp.
  - "whisper": whisper.cpp binary (default arm64-friendly choice).
  - "apple":  macOS Apple Speech via SFSpeechRecognizer (PyObjC). Falls
              back to whisper.cpp on permission denial / unavailable
              recognizer / missing pyobjc-framework-Speech.
  - "off":    voice messages rejected.

DM-multisession spec section 8 — J4 selected: transcription is local
(whisper.cpp / Apple Speech), no third-party API key required.

The whisper.cpp path is tuned for arm64 (measured on the Kali-on-Android
host, 8 cores, MATMUL_INT8 + i8mm, whisper built with REPACK=1):

  - q8_0 medium instead of fp16 — 1.8x faster, byte-identical output.
  - a tiny-model language-detect pre-pass so the real run can pin ``-l``
    and encode once instead of twice (``-l auto`` costs a whole extra
    encoder pass). See ``_detect_language``.
  - ``-t`` from ``WHISPER_THREADS`` (default 6 of 8) instead of
    whisper-cli's own default of 4.

End to end this took a typical voice message from ~32 s to ~9 s.

Public API: transcribe_voice(ogg_data) -> str (raises ValueError on failure).
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import tempfile

from .config import config

logger = logging.getLogger(__name__)


async def _run(cmd: list[str], stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
    """Run a subprocess. Returns (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(stdin)
    return proc.returncode or 0, stdout, stderr


async def _ogg_to_wav(ogg_data: bytes) -> bytes:
    """Convert OGG to 16kHz mono WAV via ffmpeg (whisper.cpp's expected input)."""
    code, stdout, stderr = await _run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-f",
            "wav",
            "-ar",
            "16000",
            "-ac",
            "1",
            "pipe:1",
        ],
        stdin=ogg_data,
    )
    if code != 0:
        raise ValueError(f"ffmpeg failed: {stderr.decode(errors='replace')[:200]}")
    return stdout


def resolve_whisper_model() -> str:
    """Path of the transcription model, with an fp16 back-compat fallback.

    The default moved to ``ggml-medium-q8_0.bin``; a host provisioned
    before that still only has ``ggml-medium.bin`` on disk. Rather than
    failing (or silently re-downloading 800 MB), use whatever is there.
    """
    primary = config.whisper_model_path
    if os.path.exists(primary):
        return primary
    legacy = os.path.join(os.path.dirname(primary), "ggml-medium.bin")
    if os.path.exists(legacy):
        logger.info("q8_0 model absent, falling back to fp16 %s", legacy)
        return legacy
    return primary


_LANG_RE = re.compile(r"auto-detected language: ([a-z]{2,3}) \(p = ([\d.]+)\)")


async def _detect_language(wav_path: str) -> str:
    """Pick the language to pin for the real transcription pass.

    Why this exists: passing ``-l auto`` to whisper makes it run the
    encoder TWICE — once inside ``whisper_lang_auto_detect`` and once for
    the actual decode. On medium that is 12.4 s of pure overhead, about
    half the wall time of a voice message. Running the detect pass on the
    tiny model instead costs 0.6 s, and the real pass then gets ``-l xx``
    and encodes once.

    Accuracy shape (measured on espeak ru/en samples, deliberately harder
    than live speech): tiny nails English every time (p >= 0.966) but is
    unreliable on Russian (it guessed de / fr / da, never above p=0.704).
    So we only *leave* the default language on a confident detection —
    Russian audio that tiny misreads as some third language still falls
    through to ``ru``, and only a p >= 0.9 call moves us off it.

    Any failure (model missing, whisper error, unparseable output) returns
    the default — detection is an optimisation, never a hard dependency.
    """
    default = config.whisper_lang_default
    lang_model = config.whisper_lang_model_path
    if not os.path.exists(lang_model):
        return default
    try:
        code, stdout, stderr = await _run(
            [
                config.whisper_bin,
                "-m",
                lang_model,
                "-f",
                wav_path,
                "-dl",  # detect language and exit
                "-t",
                str(config.whisper_threads),
            ]
        )
    except OSError as e:
        logger.debug("language detect failed to spawn: %s", e)
        return default
    if code != 0:
        return default
    blob = stdout.decode(errors="replace") + stderr.decode(errors="replace")
    m = _LANG_RE.search(blob)
    if not m:
        return default
    lang, prob = m.group(1), float(m.group(2))
    if lang != default and prob >= config.whisper_lang_min_p:
        logger.info("voice language detected as %s (p=%.3f)", lang, prob)
        return lang
    return default


async def _whisper_cpp_transcribe(ogg_data: bytes) -> str:
    """Run whisper.cpp on a WAV converted from the OGG payload."""
    model = resolve_whisper_model()
    if not os.path.exists(model):
        raise ValueError(
            f"whisper model not found at {model}. "
            "Set WHISPER_MODEL_PATH or download ggml-medium-q8_0.bin."
        )
    wav = await _ogg_to_wav(ogg_data)

    # whisper.cpp's CLI wants a file path, not stdin.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav)
        tmp_path = tmp.name

    try:
        lang = await _detect_language(tmp_path)
        cmd = [
            config.whisper_bin,
            "-m",
            model,
            "-f",
            tmp_path,
            "-nt",  # no timestamps
            "-otxt",  # write a .txt next to the input
            "-t",
            str(config.whisper_threads),
            "-l",
            lang,  # pinned, never "auto" — see _detect_language for why.
        ]
        code, stdout, stderr = await _run(cmd)
        if code != 0:
            raise ValueError(
                f"whisper-cli failed: {stderr.decode(errors='replace')[:200]}"
            )
        # Read the produced .txt file. whisper.cpp emits <input>.txt by default.
        out_txt = tmp_path + ".txt"
        try:
            with open(out_txt, "r", encoding="utf-8") as f:
                text = f.read().strip()
        except OSError:
            # Some whisper.cpp builds also print transcription on stdout.
            text = stdout.decode(errors="replace").strip()
        return text
    finally:
        for p in (tmp_path, tmp_path + ".txt"):
            try:
                os.unlink(p)
            except OSError:
                pass


def _apple_speech_sync(wav_path: str, timeout: float = 30.0) -> str | None:
    """Run SFSpeechRecognizer synchronously. Returns text or None on failure.

    Caller runs this in a thread pool — SFSpeechRecognizer's callback model
    means we block on a threading.Event; doing that on the asyncio loop
    would freeze the bot.
    """
    try:
        from Foundation import NSURL  # type: ignore
        from Speech import SFSpeechRecognizer, SFSpeechURLRecognitionRequest  # type: ignore
    except ImportError:
        return None

    import threading

    # macOS gates speech recognition behind a TCC authorization the
    # process must explicitly request. isAvailable() can read True while
    # the status is still notDetermined, so skipping this leaves the
    # recognitionTask to fail silently — the exact reason the Apple
    # backend "didn't work" from the launchd daemon (a different
    # responsible process than the terminal that was already granted).
    # 0 notDetermined · 1 denied · 2 restricted · 3 authorized.
    status = SFSpeechRecognizer.authorizationStatus()
    if status == 0:
        auth_done = threading.Event()

        def _auth_cb(new_status: int) -> None:
            auth_done.set()

        SFSpeechRecognizer.requestAuthorization_(_auth_cb)
        auth_done.wait(timeout=10.0)
        status = SFSpeechRecognizer.authorizationStatus()
    if status != 3:
        logger.info(
            "Apple Speech not authorized (status=%s); "
            "grant Speech Recognition to the bot process or use whisper",
            status,
        )
        return None

    rec = SFSpeechRecognizer.alloc().init()
    if rec is None or not rec.isAvailable():
        return None

    url = NSURL.fileURLWithPath_(wav_path)
    request = SFSpeechURLRecognitionRequest.alloc().initWithURL_(url)
    request.setShouldReportPartialResults_(False)

    result_text: list[str | None] = [None]
    failed: list[bool] = [False]
    done = threading.Event()

    def callback(result: object, error: object) -> None:
        try:
            if error is not None:
                failed[0] = True
                done.set()
                return
            if result is None:
                return
            # Only the final result is interesting (we disabled partials).
            if not getattr(result, "isFinal", lambda: False)():
                return
            transcription = getattr(result, "bestTranscription", lambda: None)()
            if transcription is not None:
                formatted = getattr(transcription, "formattedString", lambda: None)()
                if formatted is not None:
                    result_text[0] = str(formatted)
        finally:
            done.set()

    rec.recognitionTaskWithRequest_resultHandler_(request, callback)
    if not done.wait(timeout=timeout) or failed[0]:
        return None
    return result_text[0]


async def _apple_speech_transcribe(ogg_data: bytes) -> str:
    """Apple Speech via PyObjC SFSpeechRecognizer; whisper.cpp fallback."""
    wav = await _ogg_to_wav(ogg_data)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav)
        tmp_path = tmp.name
    try:
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, _apple_speech_sync, tmp_path)
        if text:
            return text.strip()
        logger.info(
            "Apple Speech unavailable or returned empty result; "
            "falling back to whisper.cpp"
        )
        return await _whisper_cpp_transcribe(ogg_data)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def resolve_voice_backend(user_id: int | None = None) -> str:
    """Resolve the effective voice backend for a user.

    Resolution order:
      1. Per-user setting (`voice` key in user_settings) when `user_id` given
         and the value isn't `auto` (auto means "follow the env default").
      2. Env-var `VOICE_BACKEND` (config.voice_backend).
      3. Platform fallback when the resolved value is `auto` (Apple on
         Darwin, whisper elsewhere).

    Returns one of: `whisper`, `apple`, `off`. Shared by the voice
    handler's enable-check and `transcribe_voice` so a per-user override
    (e.g. `apple`) is honoured even when the global env is `off` — the
    global value is only a default, not a hard gate.
    """
    backend = (config.voice_backend or "auto").lower()
    if user_id is not None:
        from .session import session_manager

        per_user = (
            session_manager.get_user_settings(user_id).get("voice") or ""
        ).lower()
        if per_user and per_user != "auto":
            backend = per_user
    if backend == "auto":
        backend = "apple" if platform.system() == "Darwin" else "whisper"
    return backend


async def transcribe_voice(ogg_data: bytes, user_id: int | None = None) -> str:
    """Dispatch to the configured backend; raise ValueError on failure."""
    backend = resolve_voice_backend(user_id)
    if backend == "off":
        raise ValueError("Voice backend is disabled")

    if backend == "whisper":
        return await _whisper_cpp_transcribe(ogg_data)
    if backend == "apple":
        return await _apple_speech_transcribe(ogg_data)

    raise ValueError(f"Unknown VOICE_BACKEND: {backend}")
