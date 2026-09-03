"""Behavioral contract for the Bria-compatible Parakeet voice backend."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from ccbot import transcribe


@pytest.mark.asyncio
async def test_auto_uses_bria_parakeet_command_and_model(tmp_path) -> None:
    model = tmp_path / "parakeet-tdt-0.6b-v3.q8_0.gguf"
    model.write_bytes(b"model")
    captured: list[tuple[list[str], bytes | None]] = []
    input_payloads: list[bytes] = []

    async def fake_run(cmd: list[str], stdin: bytes | None = None):
        captured.append((cmd, stdin))
        if cmd[0] == "ffmpeg":
            input_payloads.append(open(cmd[cmd.index("-i") + 1], "rb").read())
            return 0, b"", b""
        return 0, b"  recognized text\n", b""

    with (
        patch.object(transcribe.config, "voice_backend", "auto"),
        patch.object(
            transcribe.config,
            "parakeet_bin",
            "/opt/nemo-speech/bin/nemo-speech",
            create=True,
        ),
        patch.object(transcribe.config, "parakeet_model_path", str(model), create=True),
        patch.object(transcribe, "_run", new=fake_run),
    ):
        assert await transcribe.transcribe_voice(b"telegram-ogg") == "recognized text"

    convert_cmd = captured[0][0]
    assert convert_cmd[:4] == ["ffmpeg", "-nostdin", "-y", "-i"]
    assert convert_cmd[5:-1] == ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]
    assert captured[0][1] is None
    assert input_payloads == [b"telegram-ogg"]
    parakeet_cmd = captured[1][0]
    assert parakeet_cmd[:3] == [
        "/opt/nemo-speech/bin/nemo-speech",
        "--quiet",
        "transcribe",
    ]
    assert parakeet_cmd[3] == convert_cmd[-1]
    assert parakeet_cmd[4:] == ["--model", str(model)]
    assert len(captured) == 2, "one conversion and one inference are expected"
    assert not os.path.exists(convert_cmd[4])
    assert not os.path.exists(convert_cmd[-1])
    assert not os.path.exists(parakeet_cmd[3])


@pytest.mark.asyncio
async def test_parakeet_requires_the_configured_model(tmp_path) -> None:
    runner = AsyncMock()
    with (
        patch.object(transcribe.config, "voice_backend", "parakeet"),
        patch.object(
            transcribe.config,
            "parakeet_model_path",
            str(tmp_path / "missing.gguf"),
            create=True,
        ),
        patch.object(transcribe, "_run", new=runner),
    ):
        with pytest.raises(ValueError, match="parakeet model not found"):
            await transcribe.transcribe_voice(b"ogg")
    runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_parakeet_rejects_an_empty_transcription(tmp_path) -> None:
    model = tmp_path / "parakeet.gguf"
    model.write_bytes(b"model")

    async def fake_run(cmd: list[str], stdin: bytes | None = None):
        if cmd[0] == "ffmpeg":
            return 0, b"", b""
        return 0, b" \n", b""

    with (
        patch.object(transcribe.config, "voice_backend", "parakeet"),
        patch.object(transcribe.config, "parakeet_bin", "nemo-speech", create=True),
        patch.object(transcribe.config, "parakeet_model_path", str(model), create=True),
        patch.object(transcribe, "_run", new=fake_run),
    ):
        with pytest.raises(
            ValueError, match="Parakeet returned an empty transcription"
        ):
            await transcribe.transcribe_voice(b"ogg")
