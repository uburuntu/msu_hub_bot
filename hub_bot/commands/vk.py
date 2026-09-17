from collections import defaultdict
from logging import Logger
from operator import attrgetter
from types import TracebackType

from aiogram.types import Message
from aiogram.utils.markdown import hcode, hpre
from pendulum import UTC, DateTime, now
from tabulate import tabulate

from common.db.base import BotRepository
from common.tg.storage import RedisStorage
from common.tg.utils import command_arguments
from common.tg.wrapper import BotWrapper
from common.utils import cut_long_text, one_liner
from common.vk.api import VkApi
from common.vk.posts import VkPost
from common.vk.publish import publish_vk_post
from common.db.models import VkPatch, VkSubscription


async def process_list_vk_wall(message: Message, db: BotRepository) -> bool:
    headers = ["owner_id", "chat_id", "post", "reposts", "header", "susp", "comment"]
    configs = await db.list_vk_subscriptions()
    rows = [[v.owner_id, v.chat_id, v.last_post_id, v.with_reposts, v.with_header, v.is_suspended, v.description] for v in configs]
    for text in cut_long_text(tabulate(rows, headers=headers)):
        await message.reply(hpre(text))
    return True


async def _push_posts(
    posts: list[VkPost],
    chat_id: int,
    vwps: dict[tuple[int, int], VkSubscription],
    notify_chat_id: int,
    bot: BotWrapper,
    db: BotRepository,
) -> None:
    for post in posts:
        config = vwps[(post.owner_id, chat_id)]
        if post.id <= config.last_post_id or (not config.with_reposts and post.is_repost):
            continue
        try:
            await publish_vk_post(post, bot, config.chat_id, with_header=config.with_header)
            await db.advance_vk_cursor(config.owner_id, config.chat_id, post.id)
        except Exception:
            if notify_chat_id:
                await bot.send_message(notify_chat_id, "☢️ Не удалось опубликовать пост из VK. Попробуйте повторить выгрузку позже.")


async def process_vk_wall(message: Message, bot: BotWrapper, db: BotRepository, vk_api: VkApi) -> Message:
    args = one_liner(command_arguments(message)).split()
    if len(args) < 2:
        return await message.reply("Usage: " + hcode("/vk_wall owner_id chat_id from_id with_reposts with_header suspended comment"))
    owner_id = int(args[0])
    chat_id = int(args[1])
    last_post_id = int(args[2]) if len(args) > 2 else None
    with_reposts = bool(int(args[3])) if len(args) > 3 else None
    with_header = bool(int(args[4])) if len(args) > 4 else None
    is_suspended = bool(int(args[5])) if len(args) > 5 else None
    description = args[6] if len(args) > 6 else None
    changes = VkPatch.model_validate({key: value for key, value in {
        "last_post_id": last_post_id, "with_reposts": with_reposts, "with_header": with_header,
        "is_suspended": is_suspended, "description": description,
    }.items() if value is not None})
    config = await db.upsert_vk_subscription(owner_id, chat_id, changes)
    if is_suspended:
        return await message.reply("ℹ️ Выгрузка стены заморожена")
    posts = await VkPost.from_api_wall(vk_api, config.owner_id)
    await _push_posts(posts, chat_id, {(owner_id, chat_id): config}, message.chat.id, bot, db)
    return await message.reply("ℹ️ Выгрузка стены окончена")


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


class LastCheckUpdater:
    def __init__(self, r: RedisStorage, name: str) -> None:
        self.r = r
        self.name = name
        self.curr_dt = now(UTC)

    async def __aenter__(self) -> DateTime:
        default = self.curr_dt.subtract(days=10)
        return await self.r.get_dt(self.name, default) or default

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            await self.r.set_dt(self.name, self.curr_dt)


async def process_vk_wall_posting(
    bot: BotWrapper,
    db: BotRepository,
    redis: RedisStorage,
    vk_api: VkApi,
    events_chat_id: int,
    logger: Logger,
) -> bool:
    configs = await db.list_vk_subscriptions()
    vwps = {(v.owner_id, v.chat_id): v for v in configs if not v.is_suspended}
    owner_ids = {v.owner_id for v in vwps.values()}
    if not owner_ids:
        return True
    async with LastCheckUpdater(redis, "vk_newsfeed_last_check") as dt:
        posts = await VkPost.from_api_newsfeed(vk_api, owner_ids=owner_ids, from_ts=dt.int_timestamp)
        if not posts:
            return True
        posts_by_owner_id: dict[int, list[VkPost]] = defaultdict(list)
        for post in posts:
            posts_by_owner_id[post.owner_id].append(post)
        posts_by_chat_id: dict[int, list[VkPost]] = defaultdict(list)
        for owner_id, chat_id in vwps:
            if owner_posts := posts_by_owner_id.get(owner_id):
                posts_by_chat_id[chat_id] += owner_posts
        for chat_posts in posts_by_chat_id.values():
            chat_posts.sort(key=attrgetter("date"))
        logger.info(
            "[VK] Extracted %d post(s) from %d wall(s) to %d chat(s)",
            len(posts),
            len(posts_by_owner_id),
            len(posts_by_chat_id),
        )
        for chat_id, chat_posts in posts_by_chat_id.items():
            await _push_posts(chat_posts, chat_id, vwps, events_chat_id, bot, db)
    return True
