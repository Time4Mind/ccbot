"""Voice-to-text transcription dispatcher.

Backend chosen at runtime via VOICE_BACKEND env var:
  - "auto":   Apple Speech on Darwin if available, else whisper.cpp.
  - "whisper": whisper.cpp binary (default arm64-friendly choice).
  - "apple":  macOS Apple Speech via AppleScript helper (Mac only).
  - "openai": legacy OpenAI gpt-4o-transcribe HTTP API.
  - "off":    voice messages rejected.

DM-multisession spec section 8 — J4 selected, but the OpenAI path is kept
for sites that already have an API key configured.

Public API: transcribe_voice(ogg_data) -> str (raises ValueError on failure).
close_client() — no-op except for the OpenAI path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import tempfile

import httpx

from .config import config

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


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


async def _apple_speech_transcribe(ogg_data: bytes) -> str:
    """Apple Speech Recognition — placeholder; falls back to whisper.cpp."""
    # Implementing AVSpeechRecognizer from Python requires PyObjC + a model
    # download dance that breaks the "lightweight on demand" guarantee.
    # For v0.1 we route Darwin → whisper.cpp; revisit when needed.
    return await _whisper_cpp_transcribe(ogg_data)


async def _openai_transcribe(ogg_data: bytes) -> str:
    if not config.openai_api_key:
        raise ValueError("OpenAI backend selected but OPENAI_API_KEY is unset")
    url = f"{config.openai_base_url.rstrip('/')}/audio/transcriptions"
    client = _get_client()
    response = await client.post(
        url,
        headers={"Authorization": f"Bearer {config.openai_api_key}"},
        files={"file": ("voice.ogg", ogg_data, "audio/ogg")},
        data={"model": "gpt-4o-transcribe"},
    )
    response.raise_for_status()
    text = response.json().get("text", "").strip()
    if not text:
        raise ValueError("Empty transcription returned by API")
    return text


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

    if backend == "openai":
        return await _openai_transcribe(ogg_data)
    if backend == "whisper":
        return await _whisper_cpp_transcribe(ogg_data)
    if backend == "apple":
        return await _apple_speech_transcribe(ogg_data)

    raise ValueError(f"Unknown VOICE_BACKEND: {backend}")


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None
