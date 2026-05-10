"""Tests for session_claude_io — encode_cwd + path-builder pure logic."""

from pathlib import Path

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


@pytest.mark.asyncio
class TestGetSessionDirect:
    async def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        result = await session_claude_io.get_session_direct("nope", str(tmp_path))
        assert result is None
