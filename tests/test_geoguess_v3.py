"""GeoGuess delivery and task ownership through real aiogram objects."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageCaption, EditMessageText, SendMessage, SendPhoto
from aiogram.types import CallbackQuery

from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.providers.geoguess import Photo
from msu_hub_bot.telegram.runtime import Supervisor
from msu_hub_bot.telegram.wrapper import BotWrapper
from msu_hub_bot.commands import geoguess as game
from telegram_helpers import RecordingSession, make_message


PHOTO = Photo(
    "Норвегия",
    "Берген",
    "https://upload.wikimedia.org/test.jpg",
    "https://commons.wikimedia.org/?curid=1",
    "Author <name>",
    "CC BY 3.0",
    "https://creativecommons.org/licenses/by/3.0",
)


class GameSession(RecordingSession):
    def __init__(self):
        super().__init__()
        self.timeouts = []
        self.messages = {}
        self.caption_error = False

    async def make_request(self, bot, method, timeout=None):
        self.timeouts.append(timeout)
        if isinstance(method, (SendMessage, SendPhoto)):
            self.methods.append(method)
            message = make_message(
                bot,
                message_id=100 + len(self.messages),
                chat={"id": method.chat_id, "type": "supergroup"},
                message_thread_id=method.message_thread_id,
                is_topic_message=method.message_thread_id is not None,
            )
            self.messages[message.message_id] = message
            return message
        if isinstance(method, (EditMessageText, EditMessageCaption)):
            self.methods.append(method)
            if isinstance(method, EditMessageCaption) and self.caption_error:
                raise TelegramBadRequest(method=method, message="message can't be edited")
            return self.messages[method.message_id]
        return await super().make_request(bot, method, timeout)


@pytest.fixture
async def rig(monkeypatch):
    monkeypatch.setattr(game.Geoguess, "rounds", {})
    monkeypatch.setattr(game, "random_photo", AsyncMock(return_value=PHOTO))
    session = GameSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    message = make_message(bot, message_id=10, is_topic_message=True, message_thread_id=17)
    pipe = MagicMock()
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=False)
    pipe.execute = AsyncMock()
    client = SimpleNamespace(pipeline=MagicMock(return_value=pipe), zrevrange=AsyncMock(return_value=[]), hget=AsyncMock())
    supervisor = Supervisor()
    yield SimpleNamespace(
        bot=bot,
        session=session,
        message=message,
        pipe=pipe,
        client=client,
        redis=SimpleNamespace(redis=AsyncMock(return_value=client)),
        supervisor=supervisor,
    )
    await game.Geoguess.shutdown()
    await supervisor.drain(timeout=1, cancel_timeout=0.5)
    await session.close()


async def click(rig, round_, choice, user_id=42, token=None):
    data = game.GeoguessCallback(round=token or round_.token, choice=str(choice)).pack()
    query = CallbackQuery.model_validate(
        {
            "id": "synthetic",
            "chat_instance": "synthetic",
            "message": round_.message,
            "data": data,
            "from_user": {"id": user_id, "is_bot": False, "first_name": "User <name>"},
        },
        context={"bot": rig.bot},
    )
    return await game.Geoguess.process_cb(query, game.GeoguessCallback.unpack(data), rig.redis, rig.supervisor)


async def start(rig):
    await game.Geoguess.process(rig.message)
    return game.Geoguess.rounds[rig.message.chat.id]


async def test_round_uses_real_shortcuts_and_scores_once(rig):
    round_ = await start(rig)
    await game.Geoguess.process(rig.message)
    correct = round_.options.index(PHOTO.country)
    await click(rig, round_, correct)
    await click(rig, round_, (correct + 1) % 4)
    await click(rig, round_, correct, user_id=43, token="stale")
    assert round_.votes == {42: (correct, "User <name>")}
    await asyncio.gather(click(rig, round_, "finish"), click(rig, round_, "finish"))
    await click(rig, round_, correct, user_id=44)
    await rig.supervisor.drain(timeout=1, cancel_timeout=0.5)

    rig.pipe.zincrby.assert_called_once_with(game.score_key(rig.message.chat.id), 1, "42")
    rig.pipe.execute.assert_awaited_once()
    assert not game.Geoguess.rounds and rig.supervisor.job_count == 0
    methods = rig.session.methods
    photos = [method for method in methods if isinstance(method, SendPhoto)]
    assert len(photos) == 1 and photos[0].photo == PHOTO.url
    assert photos[0].reply_parameters.message_id == rig.message.message_id
    assert len([button for row in photos[0].reply_markup.inline_keyboard for button in row]) == 5
    assert {type(method) for method in methods} == {SendPhoto, SendMessage, EditMessageText, AnswerCallbackQuery, EditMessageCaption}
    assert all(method.message_thread_id == 17 for method in methods if isinstance(method, (SendMessage, SendPhoto)))
    boards = [method for method in methods if isinstance(method, SendMessage) and "Кто что выбрал" in method.text]
    assert boards[0].reply_parameters.message_id == round_.message.message_id
    reveals = [method for method in methods if isinstance(method, EditMessageCaption)]
    assert len(reveals) == 1 and reveals[0].message_id == round_.message.message_id
    assert "Берген, Норвегия" in reveals[0].caption and "User &lt;name&gt;" in reveals[0].caption
    assert reveals[0].reply_markup is None
    assert all(timeout == game.SEND_TIMEOUT for timeout in rig.session.timeouts)
    assert all("request_timeout" not in method.model_extra for method in methods)


async def test_failed_caption_edit_replies_in_the_same_topic(rig):
    round_ = await start(rig)
    rig.session.caption_error = True
    await click(rig, round_, "finish")
    result = rig.session.methods[-1]
    assert isinstance(result, SendMessage) and "Берген, Норвегия" in result.text
    assert result.reply_parameters.message_id == round_.message.message_id and result.message_thread_id == 17
    assert result.parse_mode == "HTML" and result.disable_web_page_preview is True
    assert not game.Geoguess.rounds


async def test_leaderboard_uses_real_shortcuts_when_storage_fails(rig):
    rig.client.zrevrange.return_value = [(b"42", 3)]
    rig.client.hget.return_value = b"User <name>"
    await game.Geoguess.top(rig.message, rig.redis)
    assert "User &lt;name&gt; — 3" in rig.session.methods[-1].text
    rig.client.zrevrange.side_effect = RuntimeError("synthetic storage failure")
    await game.Geoguess.top(rig.message, rig.redis)
    result = rig.session.methods[-1]
    assert isinstance(result, SendMessage) and result.text == "Рейтинг сейчас недоступен."
    assert result.reply_parameters.message_id == rig.message.message_id and result.message_thread_id == 17


async def test_failed_photo_releases_round_and_replies(rig):
    game.random_photo.side_effect = ExternalServiceError("synthetic photo failure")
    await game.Geoguess.process(rig.message)
    result = rig.session.methods[-1]
    assert isinstance(result, SendMessage) and result.text == "Ошибка, попробуйте еще раз"
    assert result.message_thread_id == 17 and not game.Geoguess.rounds


async def test_cancelled_callback_does_not_cancel_started_finish(rig):
    round_ = await start(rig)
    await click(rig, round_, round_.options.index(PHOTO.country))
    entered, release = asyncio.Event(), asyncio.Event()

    async def save():
        entered.set()
        await release.wait()

    rig.pipe.execute.side_effect = save
    worker = asyncio.create_task(click(rig, round_, "finish"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert rig.supervisor.job_count == 1 and not round_.task.done()
        release.set()
        drained = await rig.supervisor.drain(timeout=1, cancel_timeout=0.5)
        assert drained.cancelled_jobs == drained.failed_jobs == 0
        await click(rig, round_, "finish")
        rig.pipe.zincrby.assert_called_once_with(game.score_key(rig.message.chat.id), 1, "42")
        rig.pipe.execute.assert_awaited_once()
        assert len([method for method in rig.session.methods if isinstance(method, EditMessageCaption)]) == 1
        assert not game.Geoguess.rounds and rig.supervisor.job_count == 0
    finally:
        release.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_send_deadline_cancels_blocked_request_middleware(rig, monkeypatch):
    monkeypatch.setattr(game, "SEND_TIMEOUT", 0.01)
    cancelled = asyncio.Event()

    async def blocked(make_request, bot, method):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    rig.session.middleware(blocked)
    request = asyncio.create_task(game._send(rig.message.reply("synthetic")))
    try:
        done, _ = await asyncio.wait({request}, timeout=0.5)
        assert request in done, "The GeoGuess deadline must include request middleware"
        with pytest.raises(TimeoutError):
            await request
        assert cancelled.is_set() and not rig.session.methods
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
