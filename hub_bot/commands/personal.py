import random

from aiogram.enums import ContentType
from aiogram.types import Message

from common.tg.context import bot_for
from common.tg.utils import chat_link


async def process_sanya(message: Message) -> Message | bool | None:
    target = message.reply_to_message or message
    stickers = (
        "CAACAgIAAxkBAALaXV-ZaJ7v72HlhakrY676v6XS0B1HAAL0BAACYVerAbPyU2VqvBkZGwQ",
        "CAACAgIAAxkBAALaXl-ZaJ7cNvl3Q-3aXHE8IIpcDrU5AALQAANdo0QJo3fNYPYURkAbBA",
        "CAACAgIAAxkBAALaY1-ZaNL9bY5zWzVe0O6I4iDy24pWAAIrBQACYVerAf4XKPklQMeSGwQ",
        "CAACAgIAAxkBAALaZl-ZaNgocgtFmKS6qXse-7-pNgGQAAIsBQACYVerAazNZehk4Rk3GwQ",
        "CAACAgIAAxkBAALaaV-ZaOb6hhmBrLZthd-pwbyS8oAiAAI5BQACYVerAQABBcECL6M3bxsE",
        "CAACAgIAAxkBAALab1-ZaRAp-IJNBoX_0sWtzuat9avcAAJKBQACYVerATpy2RWL930oGwQ",
        "CAACAgIAAxkBAALacl-ZaU7_Q8nzfGDPjXIcEqAtzcKCAALAAAOWc94KDde_j1Hdqy4bBA",
        "CAACAgIAAxkBAALadV-ZaVqYPdIvMtkg4eE8ZTso9vUPAALEAAOWc94K-0Qt0T_1wPsbBA",
    )
    return await target.reply_sticker(random.choice(stickers))


async def process_dyubs(message: Message) -> Message | bool | None:
    target = message.reply_to_message or message
    sticker_set = await bot_for(message).get_sticker_set("with_love_for_471376384_by_msu_hub_bot")
    return await target.reply_sticker(random.choice(sticker_set.stickers).file_id)


async def process_popov(message: Message) -> Message | bool | None:
    target = message.reply_to_message or message
    sticker_set = await bot_for(message).get_sticker_set("popovble")
    return await target.reply_sticker(random.choice(sticker_set.stickers).file_id)


async def process_pookie_pook(message: Message) -> Message | bool | None:
    if message.content_type not in (
        ContentType.ANIMATION,
        ContentType.AUDIO,
        ContentType.DOCUMENT,
        ContentType.PHOTO,
        ContentType.VIDEO,
        ContentType.VOICE,
    ):
        return True

    if message.forward_origin is not None:
        return True

    if message.text or message.caption:
        return True

    texts = (await chat_link(message.chat), f"@{message.chat.username}")
    return await message.edit_caption(caption=random.choice(texts))
