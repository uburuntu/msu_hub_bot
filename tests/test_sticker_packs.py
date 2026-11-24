"""/packs lists, edit permissions, private menus and cover conversion; no network."""

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import GetStickerSet
from aiogram.types import CallbackQuery, InputRichBlockButtons, Message
from PIL import Image

from msu_hub_bot.commands import sticker_packs
from msu_hub_bot.commands.sticker_packs import PackCallback, PackStates
from msu_hub_bot.media.sticker_media import StickerMediaError, prepare_cover
from telegram_helpers import make_message

CHAT = -1001234567890
OWNER = abs(CHAT)
FIRST = f"with_love_for_{OWNER}_by_msu_hub_bot"
SECOND = f"with_love_for_{OWNER}_2_by_msu_hub_bot"
LEGACY = f"with_love_for_{OWNER}a_by_msu_hub_bot"


def missing():
    return TelegramBadRequest(method=GetStickerSet(name="pack"), message="Bad Request: STICKERSET_INVALID")


def sticker_set(title, count=3):
    return SimpleNamespace(title=title, stickers=[object()] * count)


def fake_bot(packs=None, *, admin=True, bot_admin=True, can_delete=True):
    packs = {FIRST: sticker_set("Котики", 120), SECOND: sticker_set("Котики (2)", 4)} if packs is None else packs

    async def lookup(name):
        if name in packs:
            return packs[name]
        raise missing()

    status = ChatMemberStatus.ADMINISTRATOR if bot_admin else ChatMemberStatus.MEMBER
    return SimpleNamespace(
        id=123456789,
        get_sticker_set=AsyncMock(side_effect=lookup),
        get_chat_administrators=AsyncMock(return_value=[SimpleNamespace(user=SimpleNamespace(id=42 if admin else 7))]),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status=status, can_delete_messages=can_delete)),
        send_rich_message=AsyncMock(),
        edit_ephemeral_message_text=AsyncMock(return_value=True),
        edit_message_text=AsyncMock(),
        delete_message=AsyncMock(return_value=True),
        set_sticker_set_title=AsyncMock(return_value=True),
        set_sticker_set_thumbnail=AsyncMock(return_value=True),
        get_chat=AsyncMock(return_value=SimpleNamespace(photo=None)),
        get_user_profile_photos=AsyncMock(return_value=SimpleNamespace(photos=[])),
        download=AsyncMock(),
    )


def buttons(rich):
    return [button for block in rich.blocks if isinstance(block, InputRichBlockButtons) for button in block.buttons]


def state_for(chat_id=CHAT, user_id=42):
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=chat_id, user_id=user_id))


def query(data, *, message_fields=None, chat=None):
    message = make_message(None, message_id=5, **(message_fields or {}), **({"chat": chat} if chat else {}))
    return CallbackQuery.model_validate(
        {"id": "q", "from": {"id": 42, "is_bot": False, "first_name": "Admin"}, "chat_instance": "c", "data": data, "message": message}
    )


@pytest.fixture(autouse=True)
def silent_answers(monkeypatch):
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    monkeypatch.setattr(Message, "reply", AsyncMock())


async def test_series_and_legacy_pack_are_listed_in_order():
    bot = fake_bot({FIRST: sticker_set("Котики"), SECOND: sticker_set("Котики (2)"), LEGACY: sticker_set("Старые")})
    packs = await sticker_packs.read_packs(bot, OWNER)
    assert [(pack.index, pack.name) for pack in packs] == [(1, FIRST), (2, SECOND), (0, LEGACY)]


def test_editors_get_rename_and_cover_buttons_and_others_only_a_link():
    packs = [sticker_packs.Pack(1, FIRST, "Котики", 120), sticker_packs.Pack(0, LEGACY, "Старые", 3)]
    editable = sticker_packs.render_list(packs, OWNER, personal=False, editable=True)
    assert [button.text for button in buttons(editable)] == ["Открыть", "✏️ Название", "🖼 Обложка"] * 2
    assert PackCallback.unpack(buttons(editable)[1].callback_data) == PackCallback(action="rename", owner=OWNER, index=1)
    read_only = sticker_packs.render_list(packs, OWNER, personal=False, editable=False)
    assert [button.url for button in buttons(read_only)] == [f"https://t.me/addstickers/{FIRST}", f"https://t.me/addstickers/{LEGACY}"]


async def test_admin_bot_sends_a_private_menu():
    bot = fake_bot()
    await sticker_packs.process_packs(make_message(None, text="/packs"), bot)
    kwargs = bot.send_rich_message.await_args.kwargs
    assert kwargs["ephemeral_message_parameters"].receiver_user_id == 42
    assert kwargs.get("reply_parameters") is None
    assert len(buttons(kwargs["rich_message"])) == 6


async def test_without_delete_rights_the_menu_is_public_and_non_admins_only_get_links():
    bot = fake_bot(admin=False, can_delete=False)
    await sticker_packs.process_packs(make_message(None, text="/packs", message_id=9), bot)
    kwargs = bot.send_rich_message.await_args.kwargs
    assert kwargs.get("ephemeral_message_parameters") is None
    assert kwargs["reply_parameters"].message_id == 9
    assert {button.text for button in buttons(kwargs["rich_message"])} == {"Открыть"}


async def test_dm_lists_personal_packs():
    personal = "with_love_for_42_by_msu_hub_bot"
    bot = fake_bot({personal: sticker_set("Мои")})
    await sticker_packs.process_packs(make_message(None, text="/packs", chat={"id": 42, "type": "private"}), bot)
    rich = bot.send_rich_message.await_args.kwargs["rich_message"]
    assert buttons(rich)[0].url.endswith(personal) and len(buttons(rich)) == 3
    bot.get_chat_member.assert_not_awaited()


async def test_no_packs_explains_how_to_start_one():
    message = make_message(None, text="/packs")
    await sticker_packs.process_packs(message, fake_bot({}))
    assert "/sc" in message.reply.await_args.args[0]


@pytest.mark.parametrize(
    "owner,chat",
    [(OWNER + 1, None), (7, {"id": 42, "type": "private"})],
    ids=["another-chat", "another-user"],
)
async def test_forged_callbacks_cannot_reach_other_packs(owner, chat):
    bot = fake_bot()
    state = state_for(chat["id"] if chat else CHAT)
    cb = query(PackCallback(action="rename", owner=owner, index=1).pack(), chat=chat)
    await sticker_packs.process_packs_cb(cb, PackCallback.unpack(cb.data), state, bot, None)
    assert cb.answer.await_args.kwargs["show_alert"] is True
    assert await state.get_state() is None
    bot.edit_ephemeral_message_text.assert_not_awaited()


async def test_non_admin_cannot_press_edit_buttons_on_a_public_menu():
    bot = fake_bot(admin=False)
    state = state_for()
    cb = query(PackCallback(action="cover", owner=OWNER, index=1).pack())
    await sticker_packs.process_packs_cb(cb, PackCallback.unpack(cb.data), state, bot, None)
    assert "админы" in cb.answer.await_args.args[0]
    assert await state.get_state() is None


async def test_private_rename_updates_one_pack_and_tidies_the_typed_title():
    bot = fake_bot()
    state = state_for()
    cb = query(PackCallback(action="rename", owner=OWNER, index=2).pack(), message_fields={"ephemeral_message_id": 77})
    await sticker_packs.process_packs_cb(cb, PackCallback.unpack(cb.data), state, bot, None)
    assert await state.get_state() == PackStates.title.state
    assert bot.edit_ephemeral_message_text.await_args.kwargs["ephemeral_message_id"] == 77

    title = make_message(None, message_id=11, text="  Новые котики  ")
    await sticker_packs.process_pack_title(title, state, bot)

    bot.set_sticker_set_title.assert_awaited_once_with(name=SECOND, title="Новые котики")
    bot.delete_message.assert_awaited_once_with(chat_id=CHAT, message_id=11)
    rich = bot.edit_ephemeral_message_text.await_args.kwargs["rich_message"]
    assert any("Название обновлено" in str(block.text) for block in rich.blocks if hasattr(block, "text"))
    assert await state.get_state() is None


async def test_public_rename_edits_the_menu_and_leaves_the_title_message():
    bot = fake_bot(can_delete=False)
    state = state_for()
    cb = query(PackCallback(action="rename", owner=OWNER, index=1).pack())
    await sticker_packs.process_packs_cb(cb, PackCallback.unpack(cb.data), state, bot, None)
    await sticker_packs.process_pack_title(make_message(None, message_id=11, text="Котики"), state, bot)
    bot.delete_message.assert_not_awaited()
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 5


async def test_expired_private_menu_falls_back_to_a_reply(monkeypatch):
    bot = fake_bot()
    bot.edit_ephemeral_message_text.side_effect = TelegramBadRequest(method=GetStickerSet(name="pack"), message="Bad Request: gone")
    monkeypatch.setattr(Message, "answer", AsyncMock())
    state = state_for()
    await state.set_state(PackStates.title)
    await state.set_data({"pack_edit": {"owner": OWNER, "index": 1, "chat_id": CHAT, "menu_ephemeral_id": 3}})
    message = make_message(None, text="Котики")
    await sticker_packs.process_pack_title(message, state, bot)
    assert "Название обновлено" in message.answer.await_args.args[0]


async def test_overlong_title_keeps_waiting():
    bot = fake_bot()
    state = state_for()
    await state.set_state(PackStates.title)
    await state.set_data({"pack_edit": {"owner": OWNER, "index": 1, "chat_id": CHAT, "menu_message_id": 5}})
    message = make_message(None, text="x" * 65)
    await sticker_packs.process_pack_title(message, state, bot)
    assert "64" in message.reply.await_args.args[0]
    bot.set_sticker_set_title.assert_not_awaited()
    assert await state.get_state() == PackStates.title.state


async def test_reset_drops_the_custom_cover():
    bot = fake_bot()
    cb = query(PackCallback(action="reset", owner=OWNER, index=1).pack())
    await sticker_packs.process_packs_cb(cb, PackCallback.unpack(cb.data), state_for(), bot, None)
    kwargs = bot.set_sticker_set_thumbnail.await_args.kwargs
    assert kwargs == {"name": FIRST, "user_id": 42, "format": "static"}


def executor():
    async def run(function, *args, **kwargs):
        return function(*args), False

    return SimpleNamespace(run=AsyncMock(side_effect=run))


def png(size=(300, 150), color=(255, 0, 0, 255)):
    output = io.BytesIO()
    Image.new("RGBA", size, color).save(output, format="PNG")
    return output.getvalue()


async def test_sent_photo_becomes_a_static_cover(monkeypatch):
    bot = fake_bot()
    state = state_for()
    await state.set_state(PackStates.cover)
    await state.set_data({"pack_edit": {"owner": OWNER, "index": 1, "chat_id": CHAT, "menu_ephemeral_id": 3}})
    monkeypatch.setattr(sticker_packs, "download_by_file_id", AsyncMock(return_value=io.BytesIO(png())))
    photo = make_message(None, message_id=12, photo=[{"file_id": "big", "file_unique_id": "big", "width": 300, "height": 150}])

    await sticker_packs.process_pack_cover(photo, state, bot, executor())

    kwargs = bot.set_sticker_set_thumbnail.await_args.kwargs
    assert kwargs["format"] == "static" and kwargs["name"] == FIRST
    with Image.open(io.BytesIO(kwargs["thumbnail"].data)) as cover:
        assert (cover.format, cover.size) == ("WEBP", (100, 100))
    sticker_packs.download_by_file_id.assert_awaited_once()
    assert sticker_packs.download_by_file_id.await_args.args[0] == "big"
    bot.delete_message.assert_awaited_once_with(chat_id=CHAT, message_id=12)


async def test_animated_sticker_cover_uses_telegrams_still(monkeypatch):
    bot = fake_bot()
    state = state_for()
    await state.set_state(PackStates.cover)
    await state.set_data({"pack_edit": {"owner": OWNER, "index": 1, "chat_id": CHAT, "menu_message_id": 5}})
    monkeypatch.setattr(sticker_packs, "download_by_file_id", AsyncMock(return_value=io.BytesIO(png())))
    animated = {
        "file_id": "tgs",
        "file_unique_id": "tgs",
        "type": "regular",
        "width": 512,
        "height": 512,
        "is_animated": True,
        "is_video": False,
        "thumbnail": {"file_id": "still", "file_unique_id": "still", "width": 128, "height": 128},
    }
    await sticker_packs.process_pack_cover(make_message(None, sticker=animated), state, bot, executor())
    assert sticker_packs.download_by_file_id.await_args.args[0] == "still"
    bot.set_sticker_set_thumbnail.assert_awaited_once()


async def test_rejected_legacy_cover_explains_itself(monkeypatch):
    bot = fake_bot()
    bot.set_sticker_set_thumbnail.side_effect = TelegramBadRequest(method=GetStickerSet(name="pack"), message="Bad Request: synthetic")
    state = state_for()
    await state.set_state(PackStates.cover)
    await state.set_data({"pack_edit": {"owner": OWNER, "index": 0, "chat_id": CHAT, "menu_message_id": 5}})
    monkeypatch.setattr(sticker_packs, "download_by_file_id", AsyncMock(return_value=io.BytesIO(png())))
    photo = make_message(None, photo=[{"file_id": "big", "file_unique_id": "big", "width": 300, "height": 150}])
    await sticker_packs.process_pack_cover(photo, state, bot, executor())
    rich = bot.edit_message_text.await_args.kwargs["rich_message"]
    assert any("анимацией" in str(block.text) for block in rich.blocks if hasattr(block, "text"))


async def test_chat_without_photo_is_reported():
    bot = fake_bot()
    cb = query(PackCallback(action="photo", owner=OWNER, index=1).pack())
    await sticker_packs.process_packs_cb(cb, PackCallback.unpack(cb.data), state_for(), bot, executor())
    assert cb.answer.await_args.args[0] == "У чата нет фото."
    bot.set_sticker_set_thumbnail.assert_not_awaited()


def test_cover_is_a_transparent_letterboxed_square_from_the_first_frame():
    frames = [Image.new("RGBA", (200, 50), color) for color in ((0, 0, 255, 255), (0, 255, 0, 255))]
    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=100)
    with Image.open(io.BytesIO(prepare_cover(output.getvalue()))) as cover:
        rgba = cover.convert("RGBA")
        assert cover.size == (100, 100)
        assert rgba.getpixel((50, 2))[3] == 0
        assert rgba.getpixel((50, 50))[:3] == (0, 0, 255)


def test_unreadable_cover_input_is_a_media_error():
    with pytest.raises(StickerMediaError):
        prepare_cover(b"not an image")
