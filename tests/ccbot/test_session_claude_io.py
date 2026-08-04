"""Tests for session_claude_io — encode_cwd + path-builder pure logic."""

from pathlib import Path
import json

import pytest

from ccbot import session_claude_io


class TestEncodeCwd:
    def test_basic_unix_path(self) -> None:
        assert session_claude_io.encode_cwd("/home/user/proj") == "-home-user-proj"

    def test_path_with_underscores(self) -> None:
        assert (
            session_claude_io.encode_cwd("/home/user_name/Code/proj")
            == "-home-user-name-Code-proj"
        )

    def test_alphanumeric_preserved(self) -> None:
        assert session_claude_io.encode_cwd("abc123XYZ-foo") == "abc123XYZ-foo"

    def test_dots_become_dashes(self) -> None:
        assert session_claude_io.encode_cwd("/foo/bar.baz") == "-foo-bar-baz"


class TestBuildSessionFilePath:
    def test_empty_inputs_return_none(self) -> None:
        assert session_claude_io.build_session_file_path("", "/tmp") is None
        assert session_claude_io.build_session_file_path("abc", "") is None

    def test_uses_encoded_cwd(self) -> None:
        p = session_claude_io.build_session_file_path("uuid-123", "/x_y/z")
        assert p is not None
        assert p.name == "uuid-123.jsonl"
        assert "-x-y-z" in str(p)


class TestParseSessionFile:
    def test_picker_description_uses_user_message_not_model_summary(
        self, tmp_path: Path
    ) -> None:
        transcript = tmp_path / "session.jsonl"
        rows = [
            {"type": "summary", "summary": "model generated description"},
            {
                "type": "user",
                "message": {"content": "Верни описание из моего сообщения"},
            },
            {
                "type": "user",
                "message": {"content": "<system-reminder>internal</system-reminder>"},
            },
        ]
        transcript.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
            encoding="utf-8",
        )

        parsed = session_claude_io._parse_session_file(transcript, "sid")

        assert parsed is not None
        assert parsed.summary == "Верни описание из моего сообщения"


@pytest.mark.asyncio
class TestGetSessionDirect:
    async def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        result = await session_claude_io.get_session_direct("nope", str(tmp_path))
        assert result is None
