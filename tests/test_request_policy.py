from unittest.mock import AsyncMock

import pytest
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import GetMe, GetUpdates, SendMessage

from msu_hub_bot.telegram.wrapper import BotWrapper
from telegram_helpers import RecordingSession, make_message


class FailingSession(RecordingSession):
    def __init__(self, failure, times=1):
        super().__init__()
        self.failure = failure
        self.remaining = times
        self.attempts = 0

    async def make_request(self, bot, method, timeout=None):
        self.attempts += 1
        if self.remaining:
            self.remaining -= 1
            raise self.failure(method)
        return await super().make_request(bot, method, timeout)


@pytest.mark.parametrize("error", [TelegramNetworkError, TelegramServerError])
async def test_ambiguous_mutations_are_never_replayed(error):
    session = FailingSession(lambda method: error(method=method, message="ambiguous"))
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    with pytest.raises(error):
        await bot(SendMessage(chat_id=42, text="one write"))
    assert session.attempts == 1


async def test_reads_retry_with_a_bound(monkeypatch):
    monkeypatch.setattr("msu_hub_bot.telegram.wrapper.asyncio.sleep", AsyncMock())
    session = FailingSession(lambda method: TelegramNetworkError(method=method, message="offline"), times=10)
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    with pytest.raises(TelegramNetworkError):
        await bot(GetMe())
    assert session.attempts == 3


async def test_explicit_rejection_can_retry_a_write(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("msu_hub_bot.telegram.wrapper.asyncio.sleep", sleep)
    session = FailingSession(lambda method: TelegramRetryAfter(method=method, message="wait", retry_after=2))
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    await bot.send_message(42, "one accepted write")
    assert session.attempts == 2
    sleep.assert_awaited_once_with(2)


async def test_long_retry_after_is_returned_to_error_policy():
    session = FailingSession(lambda method: TelegramRetryAfter(method=method, message="wait", retry_after=120))
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    with pytest.raises(TelegramRetryAfter):
        await bot.send_message(42, "later")
    assert session.attempts == 1


async def test_reply_fallback_is_nested_before_transport():
    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    message = make_message(bot)
    await message.reply("hi", allow_sending_without_reply=True)
    sent = session.methods[0]
    assert sent.reply_parameters.message_id == message.message_id
    assert sent.reply_parameters.allow_sending_without_reply is True


async def test_heartbeat_records_only_successful_getupdates(monkeypatch):
    marked = []
    monkeypatch.setattr("msu_hub_bot.telegram.wrapper.mark_poll_success", lambda: marked.append(True))
    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    await bot(GetMe())
    assert marked == []
    await bot(GetUpdates(timeout=0))
    assert marked == [True]


async def test_poll_telemetry_uses_metrics_without_traces(monkeypatch):
    from msu_hub_bot.telemetry import Telemetry
    from telemetry_helpers import Capture, config

    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    capture = Capture()
    telemetry = Telemetry(config(), transport=capture)
    await telemetry.start()
    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session, telemetry=telemetry)
    try:
        await bot(GetUpdates(timeout=0))
        await bot(GetUpdates(timeout=0))
    finally:
        await bot.session.close()
        await telemetry.close()
    assert capture.spans() == []
    output = capture.serialized()
    assert "bot.poll.requests" in output
    assert "success" in output
    assert "123456789" not in output
