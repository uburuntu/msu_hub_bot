from msu_hub_bot.settings import settings

from aiogram.types import Message

from msu_hub_bot.telegram.wrapper import BotWrapper
from msu_hub_bot.providers.vk.posts import VkPost
from msu_hub_bot.providers.vk.utils import utf16_length


async def publish_vk_post(
    post: VkPost, bot: BotWrapper, chat_id: int, reply_to: int | None = None, with_header: bool = True
) -> Message | None:
    # The configured destination prefers a captioned album when it fits.
    if chat_id == settings.vk_default_chat_id:
        text, web_preview, photos_urls, gifs_urls = post.for_publish(False, False)
        if utf16_length(text) <= 1024:
            return await bot.send_super_message_prefer_album(text, web_preview, photos_urls, gifs_urls, chat_id, reply_to)
        return await bot.send_super_message(text, web_preview, photos_urls, gifs_urls, chat_id, reply_to)

    text, web_preview, photos_urls, gifs_urls = post.for_publish(with_header)
    return await bot.send_super_message(text, web_preview, photos_urls, gifs_urls, chat_id, reply_to)
