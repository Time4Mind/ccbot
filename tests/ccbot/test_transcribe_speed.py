"""Tests for the whisper.cpp speed path: q8_0 model resolution, the
tiny-model language-detect pre-pass, and the pinned ``-l`` invocation.

Background — passing ``-l auto`` makes whisper run the encoder twice (once
inside ``whisper_lang_auto_detect``, once for the real decode). On medium
that is 12.4 s of pure overhead, roughly half a voice message's wall time.
Detecting on the ~40x cheaper tiny encoder and pinning ``-l`` removes it.

The accuracy shape these tests lock in: tiny detects English very reliably
but is shaky on Russian, so we only move OFF the default language on a
confident detection. Russian audio that tiny misreads as some third
language still falls through to the default.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from ccbot import transcribe


def _detect_output(lang: str, p: float) -> tuple[int, bytes, bytes]:
    return (
        0,
        b"",
        f"whisper_full_with_state: auto-detected language: {lang} "
        f"(p = {p:.6f})\n".encode(),
    )


class TestResolveWhisperModel:
    def test_prefers_configured_q8_when_present(self, tmp_path) -> None:
        q8 = tmp_path / "ggml-medium-q8_0.bin"
        q8.write_bytes(b"x")
        with patch.object(transcribe.config, "whisper_model_path", str(q8)):
            assert transcribe.resolve_whisper_model() == str(q8)

    def test_falls_back_to_fp16_on_preexisting_hosts(self, tmp_path) -> None:
        """A host provisioned before the q8_0 switch only has the fp16
        model — use it instead of failing or re-downloading 785 MB."""
        fp16 = tmp_path / "ggml-medium.bin"
        fp16.write_bytes(b"x")
        q8 = tmp_path / "ggml-medium-q8_0.bin"
        with patch.object(transcribe.config, "whisper_model_path", str(q8)):
            assert transcribe.resolve_whisper_model() == str(fp16)

    def test_returns_configured_path_when_nothing_exists(self, tmp_path) -> None:
        q8 = tmp_path / "ggml-medium-q8_0.bin"
        with patch.object(transcribe.config, "whisper_model_path", str(q8)):
            assert transcribe.resolve_whisper_model() == str(q8)


class TestDetectLanguage:
    @pytest.fixture
    def lang_model(self, tmp_path):
        m = tmp_path / "ggml-tiny.bin"
        m.write_bytes(b"x")
        with (
            patch.object(transcribe.config, "whisper_lang_model_path", str(m)),
            patch.object(transcribe.config, "whisper_lang_default", "ru"),
            patch.object(transcribe.config, "whisper_lang_min_p", 0.9),
        ):
            yield m

    @pytest.mark.asyncio
    async def test_confident_english_wins(self, lang_model) -> None:
        with patch.object(
            transcribe, "_run", new=AsyncMock(return_value=_detect_output("en", 0.991))
        ):
            assert await transcribe._detect_language("/tmp/x.wav") == "en"

    @pytest.mark.asyncio
    async def test_unconfident_english_keeps_default(self, lang_model) -> None:
        with patch.object(
            transcribe, "_run", new=AsyncMock(return_value=_detect_output("en", 0.62))
        ):
            assert await transcribe._detect_language("/tmp/x.wav") == "ru"

    @pytest.mark.asyncio
    async def test_third_language_misdetect_falls_through_to_default(
        self, lang_model
    ) -> None:
        """Real observed failure: tiny called Russian espeak audio 'de'
        (p=0.125), 'fr' (p=0.704), 'da' (p=0.442). All must land on ru."""
        for lang, p in (("de", 0.125), ("fr", 0.704), ("da", 0.442)):
            with patch.object(
                transcribe,
                "_run",
                new=AsyncMock(return_value=_detect_output(lang, p)),
            ):
                assert await transcribe._detect_language("/tmp/x.wav") == "ru"

    @pytest.mark.asyncio
    async def test_confident_third_language_is_honoured(self, lang_model) -> None:
        with patch.object(
            transcribe, "_run", new=AsyncMock(return_value=_detect_output("zh", 0.97))
        ):
            assert await transcribe._detect_language("/tmp/x.wav") == "zh"

    @pytest.mark.asyncio
    async def test_missing_model_returns_default_without_spawning(
        self, tmp_path
    ) -> None:
        runner = AsyncMock()
        with (
            patch.object(
                transcribe.config,
                "whisper_lang_model_path",
                str(tmp_path / "absent.bin"),
            ),
            patch.object(transcribe.config, "whisper_lang_default", "ru"),
            patch.object(transcribe, "_run", new=runner),
        ):
            assert await transcribe._detect_language("/tmp/x.wav") == "ru"
        runner.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_default(self, lang_model) -> None:
        with patch.object(
            transcribe, "_run", new=AsyncMock(return_value=(1, b"", b"boom"))
        ):
            assert await transcribe._detect_language("/tmp/x.wav") == "ru"

    @pytest.mark.asyncio
    async def test_unparseable_output_returns_default(self, lang_model) -> None:
        with patch.object(
            transcribe, "_run", new=AsyncMock(return_value=(0, b"noise", b""))
        ):
            assert await transcribe._detect_language("/tmp/x.wav") == "ru"


class TestTranscribeInvocation:
    @pytest.mark.asyncio
    async def test_pins_language_and_threads_never_auto(self, tmp_path) -> None:
        model = tmp_path / "ggml-medium-q8_0.bin"
        model.write_bytes(b"x")
        captured: list[list[str]] = []

        async def _fake_run(cmd, stdin=None):
            captured.append(cmd)
            # Emulate whisper writing <input>.txt next to the wav.
            wav = cmd[cmd.index("-f") + 1]
            with open(wav + ".txt", "w", encoding="utf-8") as f:
                f.write("привет")
            return 0, b"", b""

        with (
            patch.object(transcribe.config, "whisper_model_path", str(model)),
            patch.object(transcribe.config, "whisper_threads", 6),
            patch.object(
                transcribe, "_ogg_to_wav", new=AsyncMock(return_value=b"RIFF")
            ),
            patch.object(
                transcribe, "_detect_language", new=AsyncMock(return_value="ru")
            ),
            patch.object(transcribe, "_run", new=_fake_run),
        ):
            text = await transcribe._whisper_cpp_transcribe(b"ogg")

        assert text == "привет"
        cmd = captured[0]
        assert "auto" not in cmd, "-l auto costs a second full encoder pass"
        assert cmd[cmd.index("-l") + 1] == "ru"
        assert cmd[cmd.index("-t") + 1] == "6"
        assert cmd[cmd.index("-m") + 1] == str(model)

    @pytest.mark.asyncio
    async def test_detected_language_reaches_the_transcription_pass(
        self, tmp_path
    ) -> None:
        model = tmp_path / "ggml-medium-q8_0.bin"
        model.write_bytes(b"x")
        captured: list[list[str]] = []

        async def _fake_run(cmd, stdin=None):
            captured.append(cmd)
            wav = cmd[cmd.index("-f") + 1]
            with open(wav + ".txt", "w", encoding="utf-8") as f:
                f.write("hello")
            return 0, b"", b""

        with (
            patch.object(transcribe.config, "whisper_model_path", str(model)),
            patch.object(
                transcribe, "_ogg_to_wav", new=AsyncMock(return_value=b"RIFF")
            ),
            patch.object(
                transcribe, "_detect_language", new=AsyncMock(return_value="en")
            ),
            patch.object(transcribe, "_run", new=_fake_run),
        ):
            assert await transcribe._whisper_cpp_transcribe(b"ogg") == "hello"
        assert captured[0][captured[0].index("-l") + 1] == "en"

    @pytest.mark.asyncio
    async def test_tempfiles_are_cleaned_up(self, tmp_path) -> None:
        model = tmp_path / "ggml-medium-q8_0.bin"
        model.write_bytes(b"x")
        seen: list[str] = []

        async def _fake_run(cmd, stdin=None):
            wav = cmd[cmd.index("-f") + 1]
            seen.append(wav)
            with open(wav + ".txt", "w", encoding="utf-8") as f:
                f.write("ok")
            return 0, b"", b""

        with (
            patch.object(transcribe.config, "whisper_model_path", str(model)),
            patch.object(
                transcribe, "_ogg_to_wav", new=AsyncMock(return_value=b"RIFF")
            ),
            patch.object(
                transcribe, "_detect_language", new=AsyncMock(return_value="ru")
            ),
            patch.object(transcribe, "_run", new=_fake_run),
        ):
            await transcribe._whisper_cpp_transcribe(b"ogg")

        assert not os.path.exists(seen[0])
        assert not os.path.exists(seen[0] + ".txt")
