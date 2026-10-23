"""Cross-provider routing and native delivery retain Telegram behavior and limits."""

from dataclasses import replace
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramServerError
from aiogram.filters import Command
from aiogram.methods import SendMessage, SendRichMessage
from aiogram.types import (
    InputRichBlockDetails,
    InputRichBlockPhoto,
    InputRichBlockSlideshow,
    InputRichBlockVideo,
    Message,
    RichTextCustomEmoji,
    Update,
)

from msu_hub_bot.execution.executor import ExecutorBusy
from msu_hub_bot.providers.link_diagnostics import LinkExtraction, LinkReason, LinkStage, collect_link_diagnostics, record_link_diagnostic
from msu_hub_bot.providers.link_models import LinkAsset, LinkPost
from msu_hub_bot.providers.vk.api import VkApi
from msu_hub_bot.providers.vk.posts import VkPost
from msu_hub_bot.telegram.filters import MetaCommand
from msu_hub_bot.telegram.links import native, service, vk
from msu_hub_bot.telegram.middlewares.settings import Settings
from msu_hub_bot.telegram.middlewares.telemetry import DispatchTelemetryMiddleware
from msu_hub_bot.telegram.middlewares.viewer import ViewerMiddleware, preview_policy
from msu_hub_bot.telegram.wrapper import BotWrapper
from msu_hub_bot.telemetry import Telemetry
from telegram_helpers import RecordingSession, make_message
from telemetry_helpers import Capture, config

SITES = [
    ("youtube", "https://youtu.be/abcdefghijk", "fetch_youtube", "5278611117130653414"),
    ("instagram", "https://www.instagram.com/p/EXAMPLE/", "fetch_instagram", "5281024850096301559"),
    ("tiktok", "https://vt.tiktok.com/EXAMPLE", "fetch_tiktok", "5280662183057825163"),
]


def post(site="tiktok", **changes):
    return replace(
        LinkPost(
            site=site,
            url=f"https://www.{site}.com/example",
            author="Автор",
            username="example",
            author_url=f"https://www.{site}.com/@example",
            title="Заголовок",
            text="Первый абзац 🐱\n\nВторой абзац <&>",
            assets=(LinkAsset("video", b"video", 576, 1024, 10.5),),
        ),
        **changes,
    )


class NumberedSession(RecordingSession):
    async def make_request(self, bot, method, timeout=None):
        result = await super().make_request(bot, method, timeout)
        return result.model_copy(update={"message_id": 900 + len(self.methods)}) if isinstance(result, Message) else result


@pytest.fixture
async def runtime():
    session = NumberedSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    api = VkApi("synthetic-token")
    executor = SimpleNamespace(run=AsyncMock(return_value=(None, False)))
    viewer = ViewerMiddleware(bot, api, executor)
    yield SimpleNamespace(bot=bot, api=api, executor=executor, viewer=viewer, session=session)
    await api.close()
    await session.close()


def source(runtime, url=SITES[2][1], *, prefix="", **changes):
    return make_message(
        runtime.bot,
        text=prefix + url,
        entities=[{"type": "url", "offset": len(prefix.encode("utf-16-le")) // 2, "length": len(url)}],
        **changes,
    )


def blocks(messages):
    def walk(items):
        for block in items:
            yield block
            yield from walk(getattr(block, "blocks", []))

    for message in messages:
        yield from walk(message.blocks or [])


def text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(map(text, value))
    if isinstance(value, RichTextCustomEmoji):
        return value.alternative_text
    return text(value.text)


def visible(messages):
    return "".join(text(block.text) for block in blocks(messages) if hasattr(block, "text"))


def emoji_ids(value):
    if isinstance(value, str):
        return []
    if isinstance(value, list):
        return [item for part in value for item in emoji_ids(part)]
    if isinstance(value, RichTextCustomEmoji):
        return [value.custom_emoji_id]
    return emoji_ids(value.text)


@pytest.mark.parametrize("site,url,fetcher,emoji", SITES)
@pytest.mark.parametrize("topic", [False, True])
async def test_new_sites_use_their_provider_and_preserve_source_topic(runtime, site, url, fetcher, emoji, topic):
    runtime.executor.run.return_value = LinkExtraction(post(site), ()), False
    message = source(runtime, url, message_id=501, message_thread_id=77, is_topic_message=topic)
    await runtime.viewer.view(message, Settings())
    runtime.executor.run.assert_awaited_once_with(collect_link_diagnostics, getattr(service, fetcher), url, timeout=145)
    assert len(runtime.session.methods) == 1
    method = runtime.session.methods[0]
    assert isinstance(method, SendRichMessage) and method.chat_id == message.chat.id
    assert method.reply_parameters.message_id == 501
    assert method.message_thread_id == (77 if topic else None)
    assert method.rich_message.skip_entity_detection is True
    assert emoji in emoji_ids(method.rich_message.blocks[0].text)


@pytest.mark.parametrize("assets", [(), (LinkAsset("photo", b"thumbnail", 480, 360),)])
async def test_youtube_without_video_never_sends_or_retries_generic_extraction(runtime, assets):
    runtime.executor.run.return_value = LinkExtraction(post("youtube", assets=assets), ()), False
    await runtime.viewer.view(source(runtime, SITES[0][1]), Settings())
    runtime.executor.run.assert_awaited_once()
    assert not runtime.session.methods


@pytest.mark.parametrize("site", ["instagram", "tiktok"])
def test_other_native_sites_still_render_photo_posts(site):
    result = native.render_native_post(post(site, assets=(LinkAsset("photo", b"photo", 480, 360),)))
    assert any(isinstance(block, InputRichBlockPhoto) for block in blocks(result))


@pytest.mark.parametrize("site,url,fetcher,emoji", SITES)
async def test_disabled_auto_video_links_skips_every_new_provider(runtime, site, url, fetcher, emoji):
    await runtime.viewer.view(source(runtime, url), Settings(auto_video_links=False, auto_x_previews=True))
    runtime.executor.run.assert_not_awaited()
    assert not runtime.session.methods


@pytest.mark.parametrize(
    "url,fetcher",
    [
        ("https://www.youtube.com/playlist?list=EXAMPLE", "fetch_youtube"),
        ("https://www.instagram.com/example/", "fetch_instagram"),
        ("https://www.tiktok.com/@example/live", "fetch_tiktok"),
    ],
)
async def test_unsupported_recognized_routes_never_use_generic_ydl(runtime, url, fetcher):
    await runtime.viewer.view(source(runtime, url), Settings())
    runtime.executor.run.assert_awaited_once_with(collect_link_diagnostics, getattr(service, fetcher), url, timeout=145)
    assert not runtime.session.methods


@pytest.mark.parametrize("failure", ["unavailable", "timeout", "busy", "provider_error"])
async def test_native_failures_stay_quiet_without_a_generic_retry(runtime, failure):
    if failure == "timeout":
        runtime.executor.run.return_value = None, True
    elif failure == "busy":
        runtime.executor.run.side_effect = ExecutorBusy()
    elif failure == "provider_error":
        runtime.executor.run.side_effect = OSError("synthetic unavailable")
    await runtime.viewer.view(source(runtime), Settings())
    assert runtime.executor.run.await_count == 1 and not runtime.session.methods


@pytest.mark.parametrize(
    "fields,prefix",
    [
        ({"link_preview_options": {"is_disabled": True}}, ""),
        ({"is_automatic_forward": True}, ""),
        ({"from_user": {"id": 52, "is_bot": True, "first_name": "Bot"}}, ""),
        ({}, "/ydl "),
    ],
)
async def test_native_message_level_suppression(runtime, fields, prefix):
    await runtime.viewer.view(source(runtime, prefix=prefix, **fields), Settings())
    runtime.executor.run.assert_not_awaited()
    assert not runtime.session.methods


@pytest.mark.parametrize("flow", ["slash_command", "hashtag_command", "finishing_fsm", "handler_policy"])
async def test_router_and_conversation_suppression_reaches_native_previews(runtime, flow):
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.message.outer_middleware(runtime.viewer)
    dispatcher.message.middleware(preview_policy)
    prefix = "/meme " if flow == "slash_command" else "#meme " if flow == "hashtag_command" else ""

    async def selected(message, **data):
        data["raw_state"] = None
        return True

    filters = [Command("meme")] if flow == "slash_command" else [MetaCommand("meme")] if flow == "hashtag_command" else []
    dispatcher.message.register(selected, *filters, flags={"automatic_previews": False} if flow == "handler_policy" else {})
    await dispatcher.feed_update(
        runtime.bot,
        Update(update_id=1, message=source(runtime, prefix=prefix)),
        settings=Settings(),
        raw_state="waiting:input" if flow == "finishing_fsm" else None,
    )
    runtime.executor.run.assert_not_awaited()
    assert not any(isinstance(method, SendRichMessage) for method in runtime.session.methods)


async def test_repeated_native_url_is_published_only_once(runtime):
    url = SITES[2][1]
    message = make_message(
        runtime.bot,
        text=url + " " + url,
        entities=[{"type": "url", "offset": offset, "length": len(url)} for offset in (0, len(url) + 1)],
    )
    runtime.executor.run.return_value = LinkExtraction(post(), ()), False
    await runtime.viewer.view(message, Settings())
    assert runtime.executor.run.await_count == 1
    assert len(runtime.session.methods) == 1


async def test_other_downloaders_keep_their_standard_emoji_text(runtime):
    caption = '🎞 Original preview\n\n— <a href="https://example.org/media">MP4</a>'
    runtime.executor.run.return_value = LinkExtraction((caption, None), ()), False
    await runtime.viewer.view(source(runtime, "https://example.org/watch/123"), Settings())
    assert runtime.executor.run.call_args.args[:2] == (collect_link_diagnostics, service.text_with_preview)
    method = runtime.session.methods[0]
    assert isinstance(method, SendMessage) and method.text == caption and "tg-emoji" not in method.text


def test_native_album_keeps_all_slides_text_and_video_geometry():
    assets = tuple(LinkAsset("photo", str(index).encode(), 1350, 1080) for index in range(12))
    original = post(assets=assets)
    messages = native.render_native_post(original)
    assert len(messages) == 1 and original.text in visible(messages)
    assert "\n\nЗаголовок\n\n" in visible(messages)
    assert any(isinstance(block, InputRichBlockSlideshow) for block in blocks(messages))
    assert [block.photo.media.data for block in blocks(messages) if isinstance(block, InputRichBlockPhoto)] == [
        str(index).encode() for index in range(12)
    ]
    video = next(block.video for block in blocks(native.render_native_post(post())) if isinstance(block, InputRichBlockVideo))
    assert (video.width, video.height) == (576, 1024) and video.supports_streaming is True


def test_youtube_description_is_preserved_after_the_video_in_details():
    original = post("youtube")
    messages = native.render_native_post(original)
    top = messages[0].blocks
    assert isinstance(top[-1], InputRichBlockDetails) and top[-1].summary == "Описание"
    assert original.text in visible(messages)
    assert next(index for index, block in enumerate(top) if isinstance(block, InputRichBlockVideo)) < len(top) - 1


def test_rich_overflow_preserves_every_astral_character_and_media_order():
    original = post(text="😀 paragraph\n" * 5000)
    messages = native.render_native_post(original)
    assert len(messages) > 1 and visible(messages).endswith(original.text)
    assert sum(isinstance(block, InputRichBlockVideo) for block in blocks(messages)) == 1
    for message in messages:
        nested = list(blocks([message]))
        count = sum(len(text(block.text).encode("utf-16-le")) // 2 for block in nested if hasattr(block, "text"))
        assert count <= 32768 and len(nested) <= 500
        assert message.skip_entity_detection is True
    assert isinstance(list(blocks(messages))[-1], InputRichBlockVideo)


@pytest.mark.parametrize(
    "asset",
    [
        LinkAsset("photo", b"", 1, 1),
        LinkAsset("photo", b"x", 0, 1),
        LinkAsset("video", b"x", 1, 10001),
        LinkAsset("video", b"x", 1, 1, float("inf")),
    ],
)
def test_invalid_media_is_not_sent_as_a_partial_post(asset):
    assert native.render_native_post(post(assets=(LinkAsset("photo", b"ok", 1, 1), asset))) == []


def test_media_count_and_per_asset_sizes_are_bounded():
    assert native.render_native_post(post(assets=(LinkAsset("photo", b"x", 1, 1),) * 51)) == []
    assert native.render_native_post(post(assets=(LinkAsset("photo", b"x" * (9 * 1024 * 1024 + 1), 1, 1),))) == []


async def test_rich_chunks_keep_reply_chain_and_topic(runtime):
    result = await native.publish_native_post(post(text="😀" * 40000), runtime.bot, -10042, 501, message_thread_id=77)
    methods = runtime.session.methods
    assert result and len(methods) > 1 and all(isinstance(method, SendRichMessage) for method in methods)
    assert methods[0].reply_parameters.message_id == 501
    assert [method.reply_parameters.message_id for method in methods[1:]] == [900 + index for index in range(1, len(methods))]
    assert all(method.chat_id == -10042 and method.message_thread_id == 77 for method in methods)


async def test_definite_custom_emoji_rejection_keeps_the_native_media_on_retry(runtime, monkeypatch):
    original = post()
    failed = SendRichMessage(chat_id=-10042, rich_message=native.render_native_post(original)[0])
    send = AsyncMock(
        side_effect=[TelegramBadRequest(method=failed, message="Bad Request: CUSTOM_EMOJI_INVALID"), make_message(message_id=99)]
    )
    monkeypatch.setattr(runtime.bot, "send_rich_message", send)
    await native.publish_native_post(original, runtime.bot, -10042, 501, message_thread_id=77)
    assert send.await_count == 2 and not runtime.session.methods
    first, retry = [call.kwargs for call in send.await_args_list]
    assert emoji_ids(first["rich_message"].blocks[0].text)
    assert not emoji_ids(retry["rich_message"].blocks[0].text)
    assert visible([first["rich_message"]]) == visible([retry["rich_message"]])
    assert [block for block in blocks([first["rich_message"]]) if isinstance(block, InputRichBlockVideo)] == [
        block for block in blocks([retry["rich_message"]]) if isinstance(block, InputRichBlockVideo)
    ]
    assert retry["reply_parameters"].message_id == 501 and retry["message_thread_id"] == 77


@pytest.mark.parametrize("error_type", [TelegramNetworkError, TelegramServerError, TelegramBadRequest, TimeoutError])
async def test_uncertain_or_unrelated_native_send_failure_is_never_replayed(runtime, monkeypatch, error_type):
    original = post()
    method = SendRichMessage(chat_id=1, rich_message=native.render_native_post(original)[0])
    error = TimeoutError() if error_type is TimeoutError else error_type(method=method, message="chat not found")
    send = AsyncMock(side_effect=error)
    monkeypatch.setattr(runtime.bot, "send_rich_message", send)
    with pytest.raises(error_type):
        await native.publish_native_post(original, runtime.bot, 1, 501)
    send.assert_awaited_once()
    assert not runtime.session.methods


async def test_native_service_does_not_fall_back_after_uncertain_delivery(runtime, monkeypatch):
    runtime.executor.run.return_value = LinkExtraction(post(), ()), False
    publish = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(service, "publish_native_post", publish)
    await runtime.viewer.view(source(runtime), Settings())
    publish.assert_awaited_once()
    assert runtime.executor.run.await_count == 1 and not runtime.session.methods


async def test_vk_link_path_marks_branding_and_preserves_reply_topic(runtime, monkeypatch):
    item = object()
    monkeypatch.setattr(VkPost, "from_api_by_id", AsyncMock(return_value=[item]))
    publish = AsyncMock()
    monkeypatch.setattr(service, "publish_vk_post", publish)
    message = source(runtime, "https://vk.ru/wall-10_1", message_id=501, message_thread_id=77, is_topic_message=True)
    await runtime.viewer.view(message, Settings())
    publish.assert_awaited_once_with(item, runtime.bot, message.chat.id, 501, message_thread_id=77, parsed_link=True)
    runtime.executor.run.assert_not_awaited()


@pytest.mark.parametrize("destination", [-10, -20])
@pytest.mark.parametrize("parsed_link", [False, True])
async def test_vk_logo_is_only_added_to_parsed_links(monkeypatch, destination, parsed_link):
    monkeypatch.setattr(vk, "settings", SimpleNamespace(vk_default_chat_id=-10))
    bot = SimpleNamespace(send_super_message=AsyncMock(), send_super_message_prefer_album=AsyncMock())
    item = SimpleNamespace(for_publish=Mock(return_value=("Текст", None, [], [])))
    await vk.publish_vk_post(item, bot, destination, reply_to=501, parsed_link=parsed_link)
    sender = bot.send_super_message_prefer_album if destination == -10 else bot.send_super_message
    value = sender.await_args.args[0]
    assert value == (vk._VK_LOGO if parsed_link else "") + "Текст"


def test_vk_splitting_preserves_the_complete_custom_emoji_tag_and_link():
    class CheckedHTML(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = []
            self.visible = ""

        def handle_starttag(self, tag, attrs):
            assert tag in {"a", "tg-emoji"}
            self.tags.append(tag)

        def handle_endtag(self, tag):
            assert self.tags.pop() == tag

        def handle_data(self, value):
            self.visible += value

    label = "Абзац 😀 " * 200
    source_text = vk._VK_LOGO + '<a href="https://vk.ru/example">' + label + "</a>"
    chunks = list(vk.split_html(source_text, limit=64, max_bytes=512))
    assert len(chunks) > 1 and sum(chunk.count(vk._VK_LOGO.strip()) for chunk in chunks) == 1
    visible_text = ""
    for chunk in chunks:
        parsed = CheckedHTML()
        parsed.feed(chunk)
        parsed.close()
        assert not parsed.tags and len(parsed.visible.encode("utf-16-le")) // 2 <= 64
        visible_text += parsed.visible
    assert visible_text == "💙 " + label


async def test_native_links_in_rich_message_content_are_not_automatically_expanded(runtime):
    message = make_message(
        runtime.bot,
        rich_message={"blocks": [{"type": "paragraph", "text": SITES[2][1]}]},
    )
    await runtime.viewer.view(message, Settings())
    runtime.executor.run.assert_not_awaited()
    assert not runtime.session.methods


@pytest.mark.parametrize(
    "stage,operation,outcome,reason",
    [
        ("provider_none", "links.extract", "unavailable", None),
        ("provider_timeout", "links.extract", "timeout", "timeout"),
        ("publish_timeout", "links.publish", "timeout", "timeout"),
        ("publish_rejected", "links.publish", "rejected", "bad_request"),
    ],
)
async def test_quiet_native_failures_keep_safe_telemetry_outcomes(runtime, monkeypatch, stage, operation, outcome, reason):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    capture = Capture()
    telemetry = Telemetry(config(), transport=capture)
    runtime.viewer.telemetry = runtime.viewer.links.telemetry = telemetry
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(DispatchTelemetryMiddleware(telemetry))
    dispatcher.message.outer_middleware(runtime.viewer)
    dispatcher.message.middleware(preview_policy)

    @dispatcher.message()
    async def handled(message):
        return True

    canary = "DO_NOT_EXPORT_NATIVE_PAYLOAD"
    runtime.executor.run.return_value = LinkExtraction(post(text=canary), ()), False
    if stage == "provider_none":
        runtime.executor.run.return_value = None, False
    elif stage == "provider_timeout":
        runtime.executor.run.side_effect = TimeoutError(canary)
    else:
        method = SendRichMessage(chat_id=-10042, rich_message=native.render_native_post(post(text=canary))[0])
        error = TimeoutError(canary) if stage == "publish_timeout" else TelegramBadRequest(method=method, message=canary)
        monkeypatch.setattr(service, "publish_native_post", AsyncMock(side_effect=error))
    await telemetry.start()
    try:
        await dispatcher.feed_update(
            runtime.bot,
            Update(update_id=17, message=source(runtime, "https://vt.tiktok.com/EXAMPLE?tracking=PRIVATEURL", prefix=canary + " ")),
            settings=Settings(),
        )
    finally:
        await telemetry.close()
    found = [
        span
        for span in capture.spans()
        if any(attribute.key == "operation" and attribute.value.string_value == operation for attribute in span.attributes)
    ]
    assert len(found) == 1
    attributes = {item.key: item.value for item in found[0].attributes}
    assert attributes["outcome"].string_value == outcome
    assert attributes["provider"].string_value == "tiktok"
    assert attributes["telegram.user_id"].int_value == 42
    assert attributes["telegram.update_id"].int_value == 17
    if reason:
        assert attributes["error.reason"].string_value == reason
    serialized = capture.serialized()
    assert canary not in serialized and "PRIVATEURL" not in serialized and "https://vt.tiktok.com/EXAMPLE" in serialized
    assert not runtime.session.methods


@pytest.mark.parametrize("recovered", [False, True])
async def test_worker_diagnostics_are_replayed_with_source_even_without_a_trace(runtime, monkeypatch, recovered):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    capture = Capture()
    telemetry = Telemetry(config(sample_rate=0), transport=capture)
    runtime.viewer.links.telemetry = telemetry

    def fetch(url):
        assert url == "https://vt.tiktok.com/EXAMPLE?tracking=CANARY"
        record_link_diagnostic(LinkStage.VIDEO, LinkReason.HTTP_ERROR, http_status=403)
        if not recovered:
            record_link_diagnostic(LinkStage.ADAPTER, LinkReason.UNAVAILABLE)
            return None
        record_link_diagnostic(LinkStage.VIDEO, LinkReason.OK)
        return post(text="CANARY")

    async def execute(func, *args, **kwargs):
        import asyncio

        return await asyncio.to_thread(func, *args), False

    monkeypatch.setattr(service, "fetch_tiktok", fetch)
    runtime.executor.run.side_effect = execute
    await telemetry.start()
    try:
        with telemetry.context(user_id=42, chat_id=-10042, message_id=501):
            await runtime.viewer.view(source(runtime, "https://vt.tiktok.com/EXAMPLE?tracking=CANARY"), Settings())
    finally:
        await telemetry.close()
    logs = [{item.key: getattr(item.value, item.value.WhichOneof("value")) for item in row.attributes} for row in capture.logs()]
    terminal = next(row for row in logs if row["operation"] == "links.preview")
    assert terminal["link.source_url"] == "https://vt.tiktok.com/EXAMPLE"
    assert terminal["outcome"] == ("success" if recovered else "unavailable")
    assert terminal["link.reason"] == ("ready" if recovered else "http_error")
    if not recovered:
        assert terminal["http.response.status_code"] == 403
    assert len(runtime.session.methods) == int(recovered)
    assert "CANARY" not in capture.serialized()
    assert not capture.spans()


@pytest.mark.parametrize(
    "source,target",
    [
        ("https://en.wikipedia.org/wiki/Telegram_(software).", "https://en.wikipedia.org/wiki/Telegram_(software)"),
        ("(https://example.org/post).", "https://example.org/post"),
    ],
)
def test_native_body_preserves_balanced_parentheses_in_links(source, target):
    from msu_hub_bot.telegram.links.native import _body

    spans = _body(source, "instagram")
    assert "".join(span.text for span in spans) == source
    assert [span.url for span in spans if span.url] == [target]
