"""An isolated native router consumes the real reaction presentation and repository contract."""

import asyncio
from types import SimpleNamespace

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetChatMember, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from teleforge.app import App
from teleforge.testing import RecordingBot, RecordingSession

from msu_hub_bot.commands.reactions import PERIODS, TITLES, ReactionCallback
from msu_hub_bot.features.reactions import ReactionsFeature
from telegram_helpers import make_message
from test_reactions_command import reaction_repository, scoreboard


@pytest.fixture
async def reaction_feature():
    session = RecordingSession()
    options = SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR, edit_error=None)

    async def respond(bot, method):
        if isinstance(method, GetChatMember):
            return SimpleNamespace(status=options.status)
        if isinstance(method, EditMessageText) and options.edit_error:
            raise TelegramBadRequest(method=method, message=options.edit_error)
        return session._default(bot, method)

    session.responder = respond
    bot = RecordingBot(session=session, bot_id=123456789)
    repository = reaction_repository()
    feature = ReactionsFeature()
    app = App(feature)
    dispatcher = app.create_dispatcher()
    dispatcher["db"] = repository
    source = make_message(
        bot,
        message_id=100,
        text="/reactions",
        message_thread_id=17,
        is_topic_message=True,
        reply_to_message=make_message(bot, message_id=50, text="unrelated reply"),
    )
    rig = SimpleNamespace(
        session=session,
        bot=bot,
        repository=repository,
        feature=feature,
        app=app,
        dispatcher=dispatcher,
        source=source,
        options=options,
    )
    try:
        yield rig
    finally:
        await app.aclose()
        await bot.session.close()


async def open_card(rig, *, text="/reactions") -> Message:
    result = await rig.dispatcher.feed_update(rig.bot, Update(update_id=1, message=rig.source.model_copy(update={"text": text})))
    assert isinstance(result, Message)
    return result


def click(rig, ui, *, row=0, column=0, actor=77, data=None):
    return Update(
        update_id=actor,
        callback_query=CallbackQuery(
            id=f"click-{actor}",
            chat_instance="synthetic",
            from_user=User(id=actor, is_bot=False, first_name="Clicker"),
            message=ui,
            data=data if data is not None else ui.reply_markup.inline_keyboard[row][column].callback_data,
        ),
    )


@pytest.mark.parametrize("alias", ["/reactions", "/реакции", "#reactions", "#реакции"])
async def test_command_aliases_keep_copy_keyboard_and_reply_target(reaction_feature, alias):
    rig = reaction_feature
    card = await open_card(rig, text=alias)
    rig.repository.reaction_scoreboard.assert_awaited_once_with(rig.source.chat.id, days=30)
    sent = next(request for request in rig.bot.requests if isinstance(request, SendMessage))
    assert TITLES["getters"] in sent.text and PERIODS[30] in sent.text
    assert sent.reply_parameters.message_id == rig.source.message_id
    assert sent.message_thread_id == 17 and sent.link_preview_options.is_disabled
    assert [len(row) for row in card.reply_markup.inline_keyboard] == [2, 2, 3, 1]
    assert all(button.callback_data.startswith("react:") for row in card.reply_markup.inline_keyboard for button in row)
    assert all(len(button.callback_data.encode()) <= 64 for row in card.reply_markup.inline_keyboard for button in row)
    compiled = rig.app.build_router().sub_routers[0]
    assert compiled.message.handlers[0].flags["handler_key"] == "Reactions.process"
    assert compiled.callback_query.handlers[0].flags["handler_key"] == "Reactions.process_cb"
    assert compiled.callback_query.handlers[0].flags["fsm_release"] is True


@pytest.mark.parametrize(
    "row,column,view,days",
    [
        (0, 0, "getters", 30),
        (0, 1, "givers", 30),
        (1, 0, "posts", 30),
        (1, 1, "pulse", 30),
        (2, 0, "getters", 1),
        (2, 1, "getters", 7),
        (2, 2, "getters", 30),
        (3, 0, "getters", 30),
    ],
)
async def test_bound_buttons_render_requested_view_and_period_in_actual_clicked_ui(reaction_feature, row, column, view, days):
    rig = reaction_feature
    card = (await open_card(rig)).model_copy(update={"reply_to_message": rig.source})
    start = len(rig.bot.requests)
    await rig.dispatcher.feed_update(rig.bot, click(rig, card, row=row, column=column))
    rig.repository.reaction_scoreboard.assert_awaited_with(rig.source.chat.id, days=days)
    requests = rig.bot.requests[start:]
    assert isinstance(requests[0], AnswerCallbackQuery)
    edits = [request for request in requests if isinstance(request, EditMessageText)]
    assert len(edits) == 1 and edits[0].message_id == card.message_id
    assert TITLES[view] in edits[0].text and PERIODS[days] in edits[0].text
    assert edits[0].reply_markup is not None and edits[0].link_preview_options.is_disabled
    assert not any(isinstance(request, SendMessage) for request in requests)


async def test_private_command_keeps_group_guidance_without_storage(reaction_feature):
    rig = reaction_feature
    rig.source = rig.source.model_copy(update={"chat": Chat(id=42, type="private"), "is_topic_message": False})
    await open_card(rig)
    rig.repository.reaction_scoreboard.assert_not_awaited()
    sent = next(request for request in rig.bot.requests if isinstance(request, SendMessage))
    assert "Рейтинг живёт в групповом чате" in sent.text and sent.reply_markup is None


@pytest.mark.parametrize("change", [{"chat": Chat(id=-100999999999, type="supergroup")}, {"message_thread_id": 18}])
async def test_native_payload_uses_telegram_actual_clicked_chat_and_topic(reaction_feature, change):
    rig = reaction_feature
    card = await open_card(rig)
    rig.repository.reaction_scoreboard.reset_mock()
    start = len(rig.bot.requests)
    actual = card.model_copy(update=change)
    await rig.dispatcher.feed_update(rig.bot, click(rig, actual, data="react:getters:30"))
    rig.repository.reaction_scoreboard.assert_awaited_once_with(actual.chat.id, days=30)
    edited = next(request for request in rig.bot.requests[start:] if isinstance(request, EditMessageText))
    assert (edited.chat_id, edited.message_id) == (actual.chat.id, actual.message_id)
    assert ReactionCallback.unpack(edited.reply_markup.inline_keyboard[-1][0].callback_data).view == "getters"


async def test_overlapping_public_refreshes_acknowledge_both_and_only_query_once(reaction_feature):
    rig = reaction_feature
    card = await open_card(rig)
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(chat_id, *, days):
        entered.set()
        await release.wait()
        return scoreboard(days=days)

    rig.repository.reaction_scoreboard.reset_mock()
    rig.repository.reaction_scoreboard.side_effect = read
    first = asyncio.create_task(rig.dispatcher.feed_update(rig.bot, click(rig, card, actor=77)))
    await entered.wait()
    await rig.dispatcher.feed_update(rig.bot, click(rig, card, actor=88))
    assert len([request for request in rig.bot.requests if isinstance(request, AnswerCallbackQuery)]) == 2
    release.set()
    await first
    rig.repository.reaction_scoreboard.assert_awaited_once()


async def test_not_modified_edit_is_success_and_permission_cache_is_shared(reaction_feature):
    rig = reaction_feature
    rig.options.status = ChatMemberStatus.MEMBER
    card = await open_card(rig)
    assert "Сейчас показываю сохранённые данные" in card.text
    rig.options.edit_error = "Bad Request: message is not modified"
    await rig.dispatcher.feed_update(rig.bot, click(rig, card))
    assert len([request for request in rig.bot.requests if isinstance(request, GetChatMember)]) == 1


async def test_active_conversation_preserves_legacy_state_filter(reaction_feature):
    rig = reaction_feature
    card = await open_card(rig)
    state = rig.dispatcher.fsm.get_context(bot=rig.bot, chat_id=card.chat.id, user_id=77, thread_id=17)
    await state.set_state("posting:destination")
    rig.repository.reaction_scoreboard.reset_mock()
    await rig.dispatcher.feed_update(rig.bot, click(rig, card))
    rig.repository.reaction_scoreboard.assert_not_awaited()
    assert await state.get_state() == "posting:destination"
