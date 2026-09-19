from aiogram.types import Message
from aiogram.utils.markdown import hcode, hpre
from tabulate import tabulate

from msu_hub_bot.storage.base import BotRepository
from msu_hub_bot.telegram.utils import command_arguments
from msu_hub_bot.telegram.wrapper import BotWrapper
from msu_hub_bot.utils import cut_long_text, one_liner
from msu_hub_bot.providers.vk.api import VkApi
from msu_hub_bot.providers.vk.posts import VkPost
from msu_hub_bot.providers.vk.publish import publish_vk_post
from msu_hub_bot.storage.models import VkPatch


async def process_list_vk_wall(message: Message, db: BotRepository) -> bool:
    headers = ["owner_id", "chat_id", "post", "reposts", "header", "susp", "comment"]
    configs = await db.list_vk_subscriptions()
    rows = [[v.owner_id, v.chat_id, v.last_post_id, v.with_reposts, v.with_header, v.is_suspended, v.description] for v in configs]
    for text in cut_long_text(tabulate(rows, headers=headers)):
        await message.reply(hpre(text))
    return True


async def process_vk_wall(message: Message, bot: BotWrapper, db: BotRepository, vk_api: VkApi) -> Message:
    args = one_liner(command_arguments(message)).split()
    if len(args) < 2:
        return await message.reply("Usage: " + hcode("/vk_wall owner_id chat_id from_id with_reposts with_header suspended comment"))
    owner_id = int(args[0])
    chat_id = int(args[1])
    last_post_id = int(args[2]) if len(args) > 2 else None
    with_reposts = bool(int(args[3])) if len(args) > 3 else None
    with_header = bool(int(args[4])) if len(args) > 4 else None
    is_suspended = True
    description = args[6] if len(args) > 6 else None
    changes = VkPatch.model_validate(
        {
            key: value
            for key, value in {
                "last_post_id": last_post_id,
                "with_reposts": with_reposts,
                "with_header": with_header,
                "is_suspended": is_suspended,
                "description": description,
            }.items()
            if value is not None
        }
    )
    await db.upsert_vk_subscription(owner_id, chat_id, changes)
    return await message.reply("ℹ️ Подписка сохранена, выгрузка заморожена. Настройки и предпросмотр — в /app из нужного чата.")


async def process_vk_post(message: Message, bot: BotWrapper, vk_api: VkApi) -> Message | bool:
    args = one_liner(command_arguments(message)).split()
    if len(args) < 2:
        return await message.reply("Usage: " + hcode("/vk_post url chat_id with_header"))
    url = args[0]
    chat_id = int(args[1])
    with_header = bool(int(args[2])) if len(args) > 2 else True
    matches = VkPost.pattern_vk_post.findall(url)
    if not matches:
        return True
    posts = await VkPost.from_api_by_id(vk_api, matches[0])
    if posts:
        await publish_vk_post(posts[0], bot, chat_id, None, with_header)
    return True
