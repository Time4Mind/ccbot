"""User warnings identify the original session and survive recovery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
from ccbot.input_recovery import InputRecovery, InputRequest
from ccbot.session import Session, session_manager


async def test_notice_names_source_not_active_and_keeps_name_after_removal(
    tmp_path, monkeypatch
):
    sess = Session(id="s", name="LocalBT", window_id="@1")
    other = Session(id="other", name="Other", window_id="@2")
    monkeypatch.setattr(session_manager, "sessions", {"s": sess, "other": other})
    monkeypatch.setattr(session_manager, "get_active_session", lambda _: other)
    recovery = InputRecovery(tmp_path / "journal")
    item = InputRequest(
        "42:1",
        42,
        1,
        "s",
        "@1",
        "text",
        {"message": {"text": "Проверь отчёт"}},
        state="unknown",
        notice="Получение агентом не подтверждено.",
    )
    recovery.records[item.id] = item
    recovery.bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=99))
    )
    await recovery.deliver_notice(item)
    rendered = recovery.bot.send_message.await_args.kwargs["text"]
    assert "LocalBT" in rendered and "№1" in rendered
    assert "Проверь отчёт" in rendered
    assert "Other" not in rendered
    recovery.save()
    monkeypatch.setattr(session_manager, "sessions", {})
    restored = InputRecovery(recovery.path)
    restored.load()
    restored.complete(restored.records[item.id])
    restored.bot = SimpleNamespace(edit_message_text=AsyncMock())
    worker = asyncio.create_task(restored.feedback_loop())
    try:
        async with asyncio.timeout(1):
            while not restored.bot.edit_message_text.await_count:
                await asyncio.sleep(0)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    corrected = restored.bot.edit_message_text.await_args.kwargs["text"]
    assert "LocalBT" in corrected and "№1" in corrected
    assert "подтверждено" in corrected
