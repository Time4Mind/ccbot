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

Public API: transcribe_voice(ogg_data) -> str (raises ValueError on failure).
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
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


async def _whisper_cpp_transcribe(ogg_data: bytes) -> str:
    """Run whisper.cpp on a WAV converted from the OGG payload."""
    if not os.path.exists(config.whisper_model_path):
        raise ValueError(
            f"whisper model not found at {config.whisper_model_path}. "
            "Set WHISPER_MODEL_PATH or download ggml-medium.bin."
        )
    wav = await _ogg_to_wav(ogg_data)

    # whisper.cpp's CLI wants a file path, not stdin.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav)
        tmp_path = tmp.name

    try:
        cmd = [
            config.whisper_bin,
            "-m",
            config.whisper_model_path,
            "-f",
            tmp_path,
            "-nt",  # no timestamps
            "-otxt",  # write a .txt next to the input
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
        from Foundation import NSURL  # type: ignore[import-not-found]
        from Speech import (  # type: ignore[import-not-found]
            SFSpeechRecognizer,
            SFSpeechURLRecognitionRequest,
        )
    except ImportError:
        return None

    import threading

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


async def transcribe_voice(ogg_data: bytes, user_id: int | None = None) -> str:
    """Dispatch to the configured backend; raise ValueError on failure.

    Backend resolution order:
      1. Per-user setting (`voice` key in user_settings) when `user_id` given
         and the value isn't `auto` (auto means "follow the env default").
      2. Env-var `VOICE_BACKEND` (config.voice_backend).
      3. Platform fallback when the resolved value is `auto`.
    """
    backend = (config.voice_backend or "auto").lower()
    if user_id is not None:
        from .session import session_manager

        per_user = (
            session_manager.get_user_settings(user_id).get("voice") or ""
        ).lower()
        if per_user and per_user != "auto":
            backend = per_user
    if backend == "off":
        raise ValueError("Voice backend is disabled")
    if backend == "auto":
        backend = "apple" if platform.system() == "Darwin" else "whisper"

    if backend == "whisper":
        return await _whisper_cpp_transcribe(ogg_data)
    if backend == "apple":
        return await _apple_speech_transcribe(ogg_data)

    raise ValueError(f"Unknown VOICE_BACKEND: {backend}")
