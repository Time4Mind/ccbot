import pytest

from ccbot.transcript_opencode import project_snapshot
from ccbot.transcript_parser import TranscriptParser


TLS = "EPROTO: The TLS handshake failed. Does the server speak TLS on this port?"


@pytest.mark.parametrize(
    "name,text,status,eligible",
    [
        ("UnknownError", TLS, None, True),
        ("UnknownError", TLS + " certificate expired", None, False),
        ("UnknownError", TLS, 401, False),
        ("APIError", TLS, None, False),
    ],
)
def test_only_native_pre_http_handshake_error_is_marked(name, text, status, eligible):
    msg = dict(
        id="msg_err",
        role="assistant",
        time_created=1,
        time={"completed": 2},
        parts=[],
        error={"name": name, "data": {"message": text, "statusCode": status}},
    )
    rows = project_snapshot(
        {
            "session": {"id": "ses_root", "directory": "/tmp"},
            "messages": [msg],
            "usage": [],
        }
    )
    entries, _ = TranscriptParser.parse_entries(rows)
    assert (entries[0].api_error == "opencode_tls_handshake") is eligible


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_discussing_tls_is_ordinary_text(role):
    msg = dict(
        id="msg_text",
        role=role,
        time_created=1,
        time={"completed": 2},
        parts=[dict(id="p1", type="text", text=TLS)],
    )
    rows = project_snapshot(
        {
            "session": {"id": "ses_root", "directory": "/tmp"},
            "messages": [msg],
            "usage": [],
        }
    )
    entries, _ = TranscriptParser.parse_entries(rows)
    assert entries[0].text == TLS and not entries[0].api_error
