"""Automatic message previews, after routing and without holding FSM isolation."""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from aiogram import BaseMiddleware
from aiogram.enums import MessageEntityType
from aiogram.types import InputMediaDocument, InputMediaVideo, Message, TelegramObject, URLInputFile
from yarl import URL

from msu_hub_bot.execution.executor import ExecutorBusy
from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.providers.instagram import InstagramViewer
from msu_hub_bot.providers.pdf import convert_to_pdf
from msu_hub_bot.providers.ydl import YDL
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.delivery import AlbumMedia, reply_album
from msu_hub_bot.telegram.files import download, input_file
from msu_hub_bot.telegram.middlewares.settings import Settings
from msu_hub_bot.telegram.state import UpdateStateContext, release_state_isolation
from msu_hub_bot.telegram.utils import extract_urls
from msu_hub_bot.telegram.wrapper import BotWrapper
from msu_hub_bot.utils import megabytes, valid_filename
from msu_hub_bot.providers.vk.api import VkApi
from msu_hub_bot.providers.vk.posts import VkPost
from msu_hub_bot.providers.vk.publish import publish_vk_post


class PreviewExecutor(Protocol):
    async def run(self, func: Callable[..., Any], *args: Any, timeout: float | None = 180) -> tuple[Any, bool]: ...


class ViewerMiddleware(BaseMiddleware):
    def __init__(self, bot: BotWrapper, vk_api: VkApi, executor: PreviewExecutor) -> None:
        self.bot = bot
        self.vk_api = vk_api
        self.executor = executor

    async def handle_vk_posts(self, message: Message, url: URL) -> None:
        matches = VkPost.pattern_vk_post.findall(str(url))[:2]
        paths = ",".join(dict.fromkeys(matches))
        if paths:
            for post in await VkPost.from_api_by_id(self.vk_api, paths):
                await publish_vk_post(post, self.bot, message.chat.id, message.message_id)

    @staticmethod
    async def handle_instagram(message: Message, url: URL) -> None:
        result = await InstagramViewer.links(url)
        if result is None:
            return
        links, prefix = result
        documents: list[AlbumMedia] = []
        videos: list[AlbumMedia] = []
        for link, caption in links:
            extension = URL(link).name.rpartition(".")[2]
            file = URLInputFile(link, filename=valid_filename(f"{prefix}.{extension}"))
            if extension == "mp4":
                videos.append(InputMediaVideo(media=file, caption=caption))
            else:
                documents.append(InputMediaDocument(media=file, caption=caption))
        if documents:
            await reply_album(message, documents)
        if videos:
            await reply_album(message, videos)

    async def handle_video(self, message: Message, url: URL) -> None:
        try:
            result, timed_out = await self.executor.run(YDL.text_with_preview, str(url), timeout=60)
        except ExecutorBusy:
            return
        if timed_out or result is None:
            return
        text, preview = result
        if preview:
            video_url, width, height = preview
            await message.reply_video(URLInputFile(video_url, filename="video.mp4"), caption=text, width=width, height=height)
        else:
            await message.reply(text, disable_web_page_preview=True)

    async def view(self, message: Message, preferences: Settings) -> None:
        for url, entity_type in extract_urls(message)[:2]:
            if entity_type == MessageEntityType.URL:
                await self.handle_vk_posts(message, url)
            if url.host:
                if url.host.endswith("instagram.com"):
                    await self.handle_instagram(message, url)
                elif preferences.auto_video_links:
                    await self.handle_video(message, url)

        destination = message.document
        extensions = tuple(
            f".{extension}"
            for extension in (
                "azw",
                "azw3",
                "azw4",
                "cbr",
                "cbz",
                "cgm",
                "chm",
                "djv",
                "djvu",
                "doc",
                "docx",
                "epub",
                "fb2",
                "lit",
                "lrf",
                "mobi",
                "odg",
                "odm",
                "odp",
                "ppt",
                "pptx",
                "rb",
                "sda",
                "sdc",
                "sdd",
                "sdp",
                "sdw",
                "uof",
                "uop",
                "uos",
                "wks",
                "wmf",
                "wpd",
                "wps",
                "xbm",
                "xps",
            )
        )
        if destination is None or not destination.file_name or not destination.file_name.endswith(extensions):
            return
        if destination.file_size is not None and destination.file_size >= megabytes(20):
            return
        try:
            file = await download(destination, bot_for(message))
            if file is None:
                return
            with file:
                if file.getbuffer().nbytes >= megabytes(20):
                    return
                with await convert_to_pdf(file, destination.file_name, destination.mime_type or "application/octet-stream") as converted:
                    result = input_file(converted, str(getattr(converted, "name", "document.pdf")))
        except ExternalServiceError, TimeoutError:
            return
        await message.reply_document(result)

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        result = await handler(event, data)
        if isinstance(event, Message):
            preferences = data.get("settings")
            if not isinstance(preferences, Settings):
                raise RuntimeError("Viewer middleware requires chat preferences")
            context = data.get("state_context")
            if isinstance(context, UpdateStateContext):
                release_state_isolation(context)
            await self.view(event, preferences)
        return result
