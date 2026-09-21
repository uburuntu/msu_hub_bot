"""Downloader metadata and link contracts do not require external video traffic."""

from unittest.mock import MagicMock

import pytest
import requests
from yt_dlp.utils import DownloadError, UnsupportedError

from msu_hub_bot.providers.link_diagnostics import LinkExtraction, LinkReason, LinkStage, collect_link_diagnostics
from msu_hub_bot.providers.ydl import YDL
from msu_hub_bot.telegram.links.video import text_with_preview


def test_extractor_is_owned_and_failures_stay_quiet(monkeypatch):
    client = MagicMock()
    client.__enter__.return_value = client
    client.extract_info.side_effect = DownloadError("provider response with a private URL")
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("msu_hub_bot.providers.ydl._SingleVideoYoutubeDL", factory)
    assert YDL.extract_data("https://example.test/video") is None
    client.__exit__.assert_called_once()
    options = factory.call_args.args[0]
    assert options["remote_components"] == []
    assert options["js_runtimes"] == {"deno": {}}
    assert options["cachedir"] is False
    assert options["socket_timeout"] == 10
    assert options["allowed_extractors"] == ["default", "-generic", "end"]
    client.extract_info.assert_called_once_with("https://example.test/video", download=False)


def test_injected_extractor_remains_caller_owned():
    client = MagicMock()
    client.extract_info.return_value = {"title": "video"}
    assert YDL.extract_data("https://example.test/video", client) == {"title": "video"}
    client.__enter__.assert_not_called()
    client.__exit__.assert_not_called()


@pytest.mark.parametrize(
    "url",
    ["https://t.me/example", "https://example.org/article", "https://max.ru/example", "https://example.org/video.mp4"],
)
def test_unknown_pages_are_rejected_by_real_extractor_selection_before_network(url, monkeypatch):
    with YDL.create_ydl() as client:
        monkeypatch.setattr(client, "urlopen", lambda *args, **kwargs: pytest.fail("unsupported page reached the network"))
        result = collect_link_diagnostics(YDL.extract_data, url, client)
    assert result.value is None
    assert [(item.stage, item.reason) for item in result.diagnostics] == [(LinkStage.EXTRACT, LinkReason.UNSUPPORTED)]


@pytest.mark.parametrize(
    "url,key",
    [
        ("https://vimeo.com/123456789", "Vimeo"),
        ("https://www.youtube.com/watch?v=abcdefghijk", "Youtube"),
        ("https://t.me/example/123", "TelegramEmbed"),
    ],
)
def test_concrete_video_extractors_still_run(url, key, monkeypatch):
    with YDL.create_ydl() as client:
        extractor = client.get_info_extractor(key)
        monkeypatch.setattr(extractor, "initialize", lambda: None)
        extract = MagicMock(return_value={"id": "synthetic", "title": "clip", "url": "https://cdn.example.org/video.mp4", "ext": "mp4"})
        monkeypatch.setattr(extractor, "_real_extract", extract)
        monkeypatch.setattr(client, "urlopen", lambda *args, **kwargs: pytest.fail("synthetic extractor reached the network"))
        result = YDL.extract_data(url, client)
    assert result is not None and result["title"] == "clip"
    extract.assert_called_once_with(url)


@pytest.mark.parametrize("kind", ["playlist", "multi_video", "compat_list"])
def test_collection_entries_are_rejected_before_they_are_enumerated(kind):
    def entries():
        pytest.fail("Unsupported collection entries must not be fetched")
        yield

    result = {
        "_type": kind,
        "id": "synthetic",
        "title": "Synthetic collection",
        "extractor": "synthetic",
        "extractor_key": "Synthetic",
        "webpage_url": "https://example.test/collection",
        "entries": entries(),
    }
    with YDL.create_ydl() as client:
        assert client.process_ie_result(result, download=False) is None


@pytest.mark.parametrize("collection", [False, True])
def test_transparent_extractor_redirects_preserve_videos_and_reject_collections(monkeypatch, collection):
    embedded = (
        {"_type": "playlist", "entries": iter(())}
        if collection
        else {"id": "synthetic", "title": "Embedded title", "url": "https://example.test/video.mp4", "ext": "mp4"}
    )
    with YDL.create_ydl() as client:
        extract_info = MagicMock(return_value=embedded)
        monkeypatch.setattr(client, "extract_info", extract_info)
        result = client.process_ie_result(
            {"_type": "url_transparent", "url": "https://example.test/embedded", "title": "Page title"}, download=False
        )
        extract_info.assert_called_once_with("https://example.test/embedded", ie_key=None, extra_info={}, download=False, process=False)
    if collection:
        assert result is None
    else:
        assert result["title"] == "Page title"
        assert result["url"] == embedded["url"]


def extract(monkeypatch, info):
    client = MagicMock()
    client.extract_info.return_value = info
    monkeypatch.setattr(YDL, "post_process_links", lambda links: (links, None))
    return YDL.extract("https://example.test/video", client)


def test_youtube_keeps_muxed_video_and_best_audio_with_partial_metadata(monkeypatch):
    def fmt(key, **kwargs):
        return {"url": f"https://example.test/{key}", "format_id": key, "acodec": "aac", "vcodec": "h264", **kwargs}

    result = extract(
        monkeypatch,
        {
            "extractor": "youtube",
            "title": "clip",
            "formats": [
                fmt("muxed", protocol=None, format=None, height=720),
                fmt("silent", acodec="none"),
                fmt("hls", protocol="m3u8_native"),
                fmt("audio", vcodec="none", asr=None),
                fmt("better_audio", vcodec="none", asr=48000),
            ],
        },
    )
    assert result == (
        "clip",
        [("https://example.test/muxed", "muxed", None, 720), ("https://example.test/better_audio", "better_audio", None, None)],
        None,
    )


def test_vk_accepts_current_direct_formats_and_preserves_quality_order(monkeypatch):
    result = extract(
        monkeypatch,
        {
            "extractor": "vk",
            "formats": [
                {"url": "https://example.test/360", "format_id": "cache360", "height": 360},
                {"url": "https://example.test/720", "format_id": "url720", "height": 720},
                {"url": "https://example.test/hls", "format_id": "hls", "protocol": "m3u8"},
            ],
        },
    )
    assert result is not None
    assert [link[0] for link in result[1]] == ["https://example.test/720", "https://example.test/360"]


@pytest.mark.parametrize("info", [None, {"extractor": "generic"}, {"_type": "playlist"}, {"formats": []}])
def test_unusable_extractions_have_no_reply(monkeypatch, info):
    assert extract(monkeypatch, info) is None


def test_heads_are_bounded_closed_and_do_not_hide_usable_links(monkeypatch):
    def response(size, content_type="video/mp4"):
        result = MagicMock()
        result.__enter__.return_value = result
        result.headers = {"Content-Length": size, "Content-Type": content_type}
        return result

    large, small, unknown, malformed = response("30000000"), response("100", "video/mp4; charset=binary"), response("0"), response("NaN")
    session = MagicMock()
    session.__enter__.return_value = session
    session.head.side_effect = [large, requests.Timeout(), small, unknown, malformed]
    monkeypatch.setattr(requests, "Session", lambda: session)
    links = [(f"https://example.test/{i}", str(i), 100, 50) for i in range(5)]
    ordered, preview = YDL.post_process_links(links)
    assert ordered == [links[0], links[2], links[1], links[3], links[4]]
    assert preview == (links[2][0], 100, 50)
    for call in session.head.call_args_list:
        assert 0 < call.kwargs["timeout"] <= 5
        assert call.kwargs["allow_redirects"] is True
    for item in (large, small, unknown, malformed):
        item.__exit__.assert_called_once()
    session.__exit__.assert_called_once()


def test_preview_probes_stop_after_budget_but_links_remain(monkeypatch):
    monkeypatch.setattr("msu_hub_bot.providers.ydl.time.monotonic", MagicMock(side_effect=[0, 21, 22]))
    session = MagicMock()
    session.__enter__.return_value = session
    monkeypatch.setattr(requests, "Session", lambda: session)
    links = [("https://example.test/a", "a", None, None), ("https://example.test/b", "b", None, None)]
    assert YDL.post_process_links(links) == (links, None)
    session.head.assert_not_called()


@pytest.mark.parametrize(
    "cause,reason,status",
    [
        (requests.Timeout("PRIVATE_BODY"), LinkReason.TIMEOUT, None),
        (requests.ConnectionError("PRIVATE_BODY"), LinkReason.NETWORK_ERROR, None),
        (UnsupportedError("https://example.test/PRIVATE_BODY"), LinkReason.UNSUPPORTED, None),
        (None, LinkReason.UNAVAILABLE, None),
    ],
)
def test_scoped_extractor_failures_preserve_fixed_causes_without_message_parsing(cause, reason, status):
    client = MagicMock()
    info = (type(cause), cause, None) if cause is not None else None
    client.extract_info.side_effect = DownloadError("HTTP 403 PRIVATE_BODY", exc_info=info)

    result = collect_link_diagnostics(YDL.extract_data, "https://example.test/PRIVATE_URL", client)

    assert result.value is None
    assert [(item.stage, item.reason, item.http_status) for item in result.diagnostics] == [(LinkStage.EXTRACT, reason, status)]
    assert "PRIVATE" not in repr(result.diagnostics)
    assert not collect_link_diagnostics(lambda: None).diagnostics


def test_wrapped_extractor_http_error_retains_status_only():
    response = requests.Response()
    response.status_code = 429
    response.url = "https://example.test/PRIVATE_URL?token=PRIVATE_TOKEN"
    cause = requests.HTTPError("PRIVATE_BODY", response=response)
    client = MagicMock()
    client.extract_info.side_effect = DownloadError("PRIVATE_BODY", exc_info=(type(cause), cause, None))

    result = collect_link_diagnostics(YDL.extract_data, response.url, client)

    assert result.value is None
    assert [(item.reason, item.http_status) for item in result.diagnostics] == [(LinkReason.HTTP_ERROR, 429)]
    assert "PRIVATE" not in repr(result.diagnostics)


@pytest.mark.parametrize(
    "info,reason",
    [
        (None, LinkReason.UNSUPPORTED),
        ("malformed response", LinkReason.INVALID_RESPONSE),
        ({"extractor": "generic"}, LinkReason.UNSUPPORTED),
        ({"_type": "playlist"}, LinkReason.UNSUPPORTED),
        ({"formats": []}, LinkReason.EMPTY),
    ],
)
def test_scoped_unusable_metadata_is_distinguished_without_changing_results(info, reason):
    client = MagicMock()
    client.extract_info.return_value = info
    result = collect_link_diagnostics(YDL.extract, "https://example.test/source", client)
    assert result.value is None
    assert result.diagnostics[-1].stage is LinkStage.EXTRACT
    assert result.diagnostics[-1].reason is reason


def test_head_failure_diagnostics_retain_links_and_omit_signed_download_urls(monkeypatch):
    response = requests.Response()
    response.status_code = 403
    response.url = "https://cdn.example.test/PRIVATE_PATH?signature=PRIVATE_SIGNATURE"
    client = MagicMock()
    client.__enter__.return_value = client
    client.head.side_effect = [requests.HTTPError("PRIVATE_BODY", response=response), requests.Timeout("PRIVATE_BODY")]
    monkeypatch.setattr(requests, "Session", lambda: client)
    links = [(response.url, "one", 100, 100), ("https://cdn.example.test/PRIVATE_SECOND", "two", 100, 100)]

    result = collect_link_diagnostics(YDL.post_process_links, links)

    assert result.value == (links, None)
    assert [(item.stage, item.reason, item.http_status) for item in result.diagnostics] == [
        (LinkStage.REQUEST, LinkReason.HTTP_ERROR, 403),
        (LinkStage.REQUEST, LinkReason.TIMEOUT, None),
    ]
    assert "PRIVATE" not in repr(result.diagnostics)


def test_song_gets_url_and_viewer_gets_dimensions_with_escaped_text(monkeypatch):
    video = "https://example.test/video?a=1&b=2"
    preview = (video, 640, 480)
    extract = MagicMock(return_value=("<clip & music>", [(video, "A&B", 640, 480)], preview))
    monkeypatch.setattr(YDL, "extract", extract)
    assert YDL.preview("https://example.test/page") == video
    extract.assert_called_once_with("https://example.test/page")
    result = text_with_preview("https://example.test/page")
    extract.assert_called_with("https://example.test/page", require_youtube_video=True)
    assert result is not None
    text, returned = result
    assert returned == preview
    assert "&lt;clip &amp; music&gt;" in text and ">A&amp;B</a>" in text
    assert 'href="https://example.test/video?a=1&amp;b=2"' in text


@pytest.mark.parametrize("extractor,has_video", [("YouTube", False), ("youtube", True), ("vimeo", False)])
async def test_generic_route_suppresses_youtube_links_without_video_only(monkeypatch, extractor, has_video):
    from types import SimpleNamespace

    from aiogram.methods import SendMessage, SendVideo

    from msu_hub_bot.telegram.middlewares.settings import Settings
    from msu_hub_bot.telegram.middlewares.viewer import ViewerMiddleware
    from telegram_helpers import make_bot, make_message

    url = "https://www.youtube-nocookie.com/embed/abcdefghijk" if extractor.lower() == "youtube" else "https://vimeo.com/1234"
    media_url = "https://cdn.example.test/PRIVATE_VIDEO?signature=PRIVATE_SIGNATURE"
    info = {
        "extractor": extractor,
        "title": "Synthetic video",
        "formats": [{"url": media_url, "format_id": "muxed", "acodec": "aac", "vcodec": "h264", "width": 640, "height": 480}],
    }
    monkeypatch.setattr(YDL, "extract_data", lambda *args: info)
    preview = (media_url, 640, 480) if has_video else None
    monkeypatch.setattr(YDL, "post_process_links", lambda links: (links, preview))
    # Default extraction still serves explicit callers and /song's media adapter.
    assert YDL.extract(url) == ("Synthetic video", [(media_url, "muxed", 640, 480)], preview)
    assert YDL.preview(url) == (media_url if has_video else None)

    results = []

    async def run(function, *args, timeout=None):
        result = function(*args)
        results.append(result)
        return result, False

    bot = make_bot()
    message = make_message(bot, text=url, entities=[dict(type="url", offset=0, length=len(url))])
    viewer = ViewerMiddleware(bot, SimpleNamespace(), SimpleNamespace(run=run))
    await viewer.view(message, Settings())
    assert len(results) == 1
    if extractor.lower() == "youtube" and not has_video:
        assert results[0].value is None and bot.session.methods == []
        diagnostic = results[0].diagnostics[-1]
        assert (diagnostic.stage, diagnostic.reason) == (LinkStage.ADAPTER, LinkReason.UNAVAILABLE)
        assert "PRIVATE" not in repr(results[0].diagnostics)
    else:
        assert len(bot.session.methods) == 1
        assert isinstance(bot.session.methods[0], SendVideo if has_video else SendMessage)


@pytest.mark.parametrize("enabled,timed_out", [(False, False), (True, False), (True, True)])
async def test_generic_video_viewer_preserves_preference_timeout_topic_and_video_delivery(enabled, timed_out):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from aiogram.methods import SendVideo
    from aiogram.types import URLInputFile
    from telegram_helpers import make_bot, make_message

    from msu_hub_bot.telegram.middlewares.settings import Settings
    from msu_hub_bot.telegram.middlewares.viewer import ViewerMiddleware

    bot = make_bot()
    url = "https://vimeo.com/1234"
    message = make_message(
        bot, text=url, entities=[dict(type="url", offset=0, length=len(url))], is_topic_message=True, message_thread_id=9
    )
    preview = ("https://example.test/video.mp4", 640, 480)
    executor = SimpleNamespace(run=AsyncMock(return_value=(LinkExtraction(("Synthetic caption", preview), ()), timed_out)))
    viewer = ViewerMiddleware(bot, SimpleNamespace(), executor)
    await viewer.view(message, Settings(auto_video_links=enabled))
    if not enabled:
        executor.run.assert_not_awaited()
    else:
        executor.run.assert_awaited_once_with(collect_link_diagnostics, text_with_preview, url, timeout=60)
    if enabled and not timed_out:
        method = bot.session.methods[-1]
        assert isinstance(method, SendVideo) and isinstance(method.video, URLInputFile)
        assert method.video.url == preview[0] and (method.width, method.height) == preview[1:]
        assert method.message_thread_id == 9 and method.reply_parameters.message_id == message.message_id
        assert method.caption == "Synthetic caption"
    else:
        assert bot.session.methods == []
