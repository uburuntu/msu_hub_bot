"""Automatic message previews, after routing and without holding FSM isolation."""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from aiogram import BaseMiddleware
from aiogram.enums import MessageEntityType
from aiogram.types import InputMediaDocument, InputMediaVideo, Message, TelegramObject, URLInputFile
from yarl import URL

from common.externals.exceptions import ExternalServiceError
from common.externals.instagram import InstagramViewer
from common.externals.tiktok import tiktok_text_with_preview
from common.externals.topdf import convert_to_pdf
from common.externals.ydl import YDL
from common.tg.context import bot_for
from common.tg.delivery import AlbumMedia, reply_album
from common.tg.files import download
from common.tg.middlewares.settings import Settings
from common.tg.state import UpdateStateContext, release_state_isolation
from common.tg.utils import extract_urls
from common.tg.wrapper import BotWrapper
from common.utils import megabytes, valid_filename
from common.vk.api import VkApi
from common.vk.posts import VkPost
from common.vk.publish import publish_vk_post


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
        result, timed_out = await self.executor.run(YDL.text_with_preview, str(url), timeout=60)
        if timed_out or result is None:
            return
        text, preview = result
        if preview:
            video_url, width, height = preview
            await message.reply_video(URLInputFile(video_url, filename="video.mp4"), caption=text, width=width, height=height)
        else:
            await message.reply(text, disable_web_page_preview=True)

    @staticmethod
    async def handle_tiktok(message: Message, url: URL) -> None:
        result = await tiktok_text_with_preview(str(url))
        if result:
            text, preview = result
            if preview:
                await message.reply_video(URLInputFile(preview, filename="video.mp4"), caption=text)
            else:
                await message.reply(text)

    async def view(self, message: Message, preferences: Settings) -> None:
        for url, entity_type in extract_urls(message)[:2]:
            if entity_type == MessageEntityType.URL:
                await self.handle_vk_posts(message, url)
            if url.host:
                if url.host.endswith("instagram.com"):
                    await self.handle_instagram(message, url)
                elif url.host.endswith("tiktok.com"):
                    if preferences.auto_video_links:
                        await self.handle_tiktok(message, url)
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
                pdf_url, thumbnail, name = await convert_to_pdf(
                    file, destination.file_name, destination.mime_type or "application/octet-stream"
                )
        except ExternalServiceError:
            return
        await message.reply_document(URLInputFile(pdf_url, filename=name), thumbnail=URLInputFile(thumbnail, filename="thumbnail.jpg"))

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
