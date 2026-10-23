"""Automatic link ownership, preferences and provider delivery."""

from collections.abc import Callable
from typing import Any, Protocol

from aiogram.enums import MessageEntityType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, URLInputFile
from yarl import URL

from msu_hub_bot.execution.executor import ExecutorBusy
from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.providers.fxembed import FxEmbed, PostLink, is_x_url, parse_post_url
from msu_hub_bot.providers.instagram import fetch_instagram
from msu_hub_bot.providers.link_diagnostics import LinkDiagnostic, LinkExtraction, LinkReason, LinkStage, collect_link_diagnostics
from msu_hub_bot.providers.link_models import LinkPost, LinkSite
from msu_hub_bot.providers.tiktok import fetch_tiktok, normalize_tiktok_url
from msu_hub_bot.providers.vk.api import VkApi
from msu_hub_bot.providers.vk.posts import VkPost
from msu_hub_bot.providers.youtube import fetch_youtube, parse_youtube_url
from msu_hub_bot.telegram.links.native import publish_native_post
from msu_hub_bot.telegram.links.video import text_with_preview
from msu_hub_bot.telegram.links.vk import publish_vk_post
from msu_hub_bot.telegram.links.x import publish_x_post
from msu_hub_bot.telegram.middlewares.settings import Settings
from msu_hub_bot.telegram.utils import extract_urls
from msu_hub_bot.telegram.wrapper import BotWrapper
from msu_hub_bot.telemetry import Boundary, Operation, Outcome, Provider, Telemetry


class PreviewExecutor(Protocol):
    async def run(self, func: Callable[..., Any], *args: Any, timeout: float | None = 180) -> tuple[Any, bool]: ...


_SITE_HOSTS: dict[str, LinkSite] = {
    **{host: "youtube" for host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be")},
    **{host: "instagram" for host in ("instagram.com", "www.instagram.com", "m.instagram.com")},
    **{host: "tiktok" for host in ("tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com")},
}
_PROVIDERS = {"youtube": Provider.YOUTUBE, "instagram": Provider.INSTAGRAM, "tiktok": Provider.TIKTOK}
# Previews may wait this long for a worker; providers keep their own extraction deadlines.
_QUEUE_SECONDS = 60


def _unavailable(attempt: Operation, diagnostics: tuple[LinkDiagnostic, ...], *, generic: bool = False) -> None:
    failures = [item for item in diagnostics if item.reason not in {LinkReason.OK, LinkReason.READY}]
    useful = [item for item in failures if item.reason not in {LinkReason.UNAVAILABLE, LinkReason.EMPTY}]
    if generic and failures and all(item.reason is LinkReason.EMPTY for item in failures):
        attempt.link_result(LinkStage.EXTRACT, LinkReason.UNSUPPORTED)
        return
    if failure := next(iter(reversed(useful or failures)), None):
        attempt.link_result(failure.stage, failure.reason)
        if failure.http_status is not None:
            attempt.http_status(failure.http_status)
    else:
        attempt.link_result(LinkStage.EXTRACT, LinkReason.UNSUPPORTED if generic else LinkReason.UNAVAILABLE)


class LinkService:
    def __init__(self, bot: BotWrapper, vk_api: VkApi, executor: PreviewExecutor, *, telemetry: Telemetry | None = None) -> None:
        self.bot = bot
        self.vk_api = vk_api
        self.executor = executor
        self.fxembed = FxEmbed()
        self.telemetry = telemetry or Telemetry()

    async def handle_x_post(self, message: Message, link: PostLink) -> None:
        with self.telemetry.link_preview(link.url, Provider.FXEMBED) as attempt:
            attempt.link_result(LinkStage.EXTRACT, LinkReason.READY)
            try:
                with self.telemetry.operation(Boundary.PROVIDER, "fxembed.fetch", provider=Provider.FXEMBED):
                    post = await self.fxembed.get_post(link)
            except ExternalServiceError as error:
                attempt.link_error(LinkStage.EXTRACT, error)
                diagnostic = getattr(error, "diagnostic", None)
                if isinstance(diagnostic, LinkDiagnostic):
                    attempt.link_diagnostics((diagnostic,))
                    _unavailable(attempt, (diagnostic,))
                return
            attempt.link_result(LinkStage.DELIVERY, LinkReason.READY)
            sent = await publish_x_post(
                post,
                self.bot,
                message.chat.id,
                message.message_id,
                message_thread_id=message.message_thread_id if message.is_topic_message else None,
                link=link,
                telemetry=self.telemetry,
            )
            if sent is None:
                attempt.link_result(LinkStage.DELIVERY, LinkReason.EMPTY)

    @staticmethod
    def x_preview_allowed(message: Message) -> bool:
        return not (
            (message.from_user and message.from_user.is_bot)
            or message.is_automatic_forward
            or (message.link_preview_options and message.link_preview_options.is_disabled is True)
            or (message.text or message.caption or "").lstrip().startswith("/")
            or any(entity.type == MessageEntityType.BOT_COMMAND for entity in message.entities or message.caption_entities or [])
        )

    async def handle_vk_posts(self, message: Message, url: URL) -> bool:
        matches = VkPost.pattern_vk_post.findall(str(url))[:2]
        paths = ",".join(dict.fromkeys(matches))
        if not paths:
            return False
        with self.telemetry.link_preview(str(url), Provider.VK) as attempt:
            attempt.link_result(LinkStage.EXTRACT, LinkReason.READY)
            with self.telemetry.operation(Boundary.PROVIDER, "links.extract", provider=Provider.VK) as operation:
                posts = await VkPost.from_api_by_id(self.vk_api, paths)
                if not posts:
                    operation.set_outcome(Outcome.UNAVAILABLE)
                    attempt.link_result(LinkStage.EXTRACT, LinkReason.EMPTY)
                    return True
            attempt.link_result(LinkStage.DELIVERY, LinkReason.READY)
            for post in posts:
                sent = await publish_vk_post(
                    post,
                    self.bot,
                    message.chat.id,
                    message.message_id,
                    message_thread_id=message.message_thread_id if message.is_topic_message else None,
                    parsed_link=True,
                )
                if sent is None:
                    attempt.link_result(LinkStage.DELIVERY, LinkReason.EMPTY)
        return True

    async def handle_video(self, message: Message, url: URL) -> None:
        with self.telemetry.link_preview(str(url), Provider.YTDLP) as attempt:
            attempt.link_result(LinkStage.EXTRACT, LinkReason.READY)
            try:
                result, timed_out = await self.executor.run(
                    collect_link_diagnostics, text_with_preview, str(url), timeout=60 + _QUEUE_SECONDS
                )
            except ExecutorBusy:
                attempt.link_result(LinkStage.EXTRACT, LinkReason.BUSY)
                return
            if timed_out:
                attempt.link_result(LinkStage.EXTRACT, LinkReason.TIMEOUT)
                return
            if not isinstance(result, LinkExtraction):
                attempt.link_result(LinkStage.EXTRACT, LinkReason.UNEXPECTED)
                return
            attempt.link_diagnostics(result.diagnostics)
            if result.value is None:
                _unavailable(attempt, result.diagnostics, generic=True)
                return
            text, preview = result.value
            attempt.link_result(LinkStage.DELIVERY, LinkReason.READY)
            if preview:
                video_url, width, height = preview
                await message.reply_video(URLInputFile(video_url, filename="video.mp4"), caption=text, width=width, height=height)
            else:
                await message.reply(text, disable_web_page_preview=True)

    async def handle_native(self, message: Message, url: URL, site: LinkSite) -> None:
        fetchers: dict[LinkSite, Callable[[str], LinkPost | None]] = {
            "youtube": fetch_youtube,
            "instagram": fetch_instagram,
            "tiktok": fetch_tiktok,
        }
        extracting = True
        try:
            with self.telemetry.link_preview(str(url), _PROVIDERS[site]) as attempt:
                attempt.link_result(LinkStage.EXTRACT, LinkReason.READY)
                with self.telemetry.operation(Boundary.PROVIDER, "links.extract", provider=_PROVIDERS[site]) as operation:
                    extracted, timed_out = await self.executor.run(
                        collect_link_diagnostics, fetchers[site], str(url), timeout=85 + _QUEUE_SECONDS
                    )
                    if timed_out:
                        operation.set_outcome(Outcome.TIMEOUT)
                        attempt.link_result(LinkStage.EXTRACT, LinkReason.TIMEOUT)
                        return
                    if not isinstance(extracted, LinkExtraction):
                        operation.set_outcome(Outcome.UNAVAILABLE)
                        attempt.link_result(LinkStage.EXTRACT, LinkReason.UNAVAILABLE)
                        return
                    attempt.link_diagnostics(extracted.diagnostics)
                    result = extracted.value
                    if not isinstance(result, LinkPost) or not result.assets:
                        _unavailable(attempt, extracted.diagnostics)
                        operation.set_outcome(attempt.outcome)
                        return
                extracting = False
                attempt.link_result(LinkStage.DELIVERY, LinkReason.READY)
                with self.telemetry.operation(
                    Boundary.TELEGRAM,
                    "links.publish",
                    provider=_PROVIDERS[site],
                    telegram_method="sendRichMessage",
                    target_chat_id=message.chat.id,
                    target_message_id=message.message_id,
                ) as operation:
                    sent = await publish_native_post(
                        result,
                        self.bot,
                        message.chat.id,
                        message.message_id,
                        message_thread_id=message.message_thread_id if message.is_topic_message else None,
                    )
                    if sent is None:
                        operation.set_outcome(Outcome.IGNORED)
                        attempt.link_result(LinkStage.DELIVERY, LinkReason.EMPTY)
        except (ExecutorBusy, ExternalServiceError, TimeoutError, ValueError, OSError, TelegramAPIError) as error:
            if not extracting and not isinstance(error, (TelegramAPIError, TimeoutError)):
                raise
            # Telemetry records the failure; the chat keeps its original link.
            # An uncertain Telegram write is never repeated.
            return

    def _skip(self, url: URL, provider: Provider, reason: LinkReason) -> None:
        with self.telemetry.link_preview(str(url), provider) as attempt:
            attempt.link_result(LinkStage.ROUTE, reason)

    async def view(self, message: Message, preferences: Settings, *, x_previews: bool = True) -> None:
        seen_x: set[tuple[str, str | None, int | None]] = set()
        seen_native: set[tuple[LinkSite, str]] = set()
        considered = 0
        for url, entity_type in extract_urls(message):
            if considered >= 2:
                break
            if is_x_url(str(url)):
                link = parse_post_url(str(url))
                if link is None:
                    # Explicit embed modifiers and non-post routes keep their own behavior.
                    considered += 1
                    self._skip(url, Provider.FXEMBED, LinkReason.UNSUPPORTED)
                    continue
                identity = (link.id, link.media_kind, link.media_index)
                if identity in seen_x:
                    continue
                seen_x.add(identity)
                considered += 1
                if x_previews and preferences.auto_x_previews and self.x_preview_allowed(message):
                    await self.handle_x_post(message, link)
                else:
                    self._skip(url, Provider.FXEMBED, LinkReason.DISABLED if not preferences.auto_x_previews else LinkReason.POLICY)
                continue
            site = _SITE_HOSTS.get(url.host or "")
            if site:
                if url.scheme == "http":
                    url = url.with_scheme("https")
                canonical = str(url)
                if site == "youtube" and (youtube := parse_youtube_url(canonical)):
                    canonical = youtube.url
                elif site == "tiktok":
                    canonical = normalize_tiktok_url(canonical) or canonical
                elif site == "instagram":
                    canonical = str(url.with_host("www.instagram.com").with_query(None).with_fragment(None)).rstrip("/")
                identity_native = site, canonical
                if identity_native in seen_native:
                    continue
                seen_native.add(identity_native)
                considered += 1
                if x_previews and preferences.auto_video_links and self.x_preview_allowed(message):
                    await self.handle_native(message, url, site)
                else:
                    self._skip(url, _PROVIDERS[site], LinkReason.DISABLED if not preferences.auto_video_links else LinkReason.POLICY)
                continue
            considered += 1
            if entity_type == MessageEntityType.URL and await self.handle_vk_posts(message, url):
                continue
            if url.host and preferences.auto_video_links:
                await self.handle_video(message, url)
