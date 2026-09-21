"""Public TikTok extraction preserves complete posts and rejects misleading results."""

import copy
import json

import pytest

from msu_hub_bot.providers import tiktok
from msu_hub_bot.providers.link_diagnostics import LinkReason, LinkStage, collect_link_diagnostics
from msu_hub_bot.providers.link_models import LinkAsset

POST_ID = "7598426984607173900"
PAGE = f"https://www.tiktok.com/@example/video/{POST_ID}"
PHOTO = f"https://www.tiktok.com/@example/photo/{POST_ID}"


def payload(**changes):
    return {
        "id": POST_ID,
        "author": {"nickname": "Космокот", "uniqueId": "example"},
        "desc": "Первая строка 🐱\n\nВторая строка",
        "video": {"duration": 10.5},
        **changes,
    }


def photo(index):
    return {"imageURL": {"urlList": [f"https://p16-common-sign.tiktokcdn-eu.com/{index}.jpg"]}}


def album(count=12):
    return payload(imagePost={"title": "Фотоальбом", "images": [photo(index) for index in range(count)]}, video={"duration": 0})


def html(item, *, status=0):
    data = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"statusCode": status, "itemInfo": {"itemStruct": item}}}}
    return (
        '<html><script type="text/javascript">irrelevant()</script>'
        "<script type='application/json' id='__UNIVERSAL_DATA_FOR_REHYDRATION__'>"
        + json.dumps(data, ensure_ascii=False)
        + "</script></html>"
    ).encode()


@pytest.fixture
def transport(monkeypatch):
    calls = {"pages": [], "videos": [], "images": []}

    def page(url, **options):
        calls["pages"].append((url, options))
        return url, html(payload())

    def video(url, **options):
        calls["videos"].append((url, options))
        return LinkAsset(kind="video", data=b"video", width=576, height=1024, duration=10.5)

    def image(url, **options):
        calls["images"].append((url, options))
        return LinkAsset(kind="photo", data=url.rsplit("/", 1)[-1].encode(), width=1350, height=1080)

    monkeypatch.setattr(tiktok, "request_page", page)
    monkeypatch.setattr(tiktok, "download_video", video)
    monkeypatch.setattr(tiktok, "download_image", image)
    return calls


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (f"http://TIKTOK.com/@example/video/{POST_ID}/?track=1#fragment", PAGE),
        (f"https://www.tiktok.com/@/video/{POST_ID}", f"https://www.tiktok.com/@/video/{POST_ID}"),
        (PHOTO + "?image_index=9", PHOTO),
        ("https://vt.tiktok.com/ABC12/?track=1", "https://vt.tiktok.com/ABC12"),
        ("https://vm.tiktok.com/ABC12/", "https://vm.tiktok.com/ABC12"),
        ("https://tiktok.com/t/ABC12/?track=1", "https://www.tiktok.com/t/ABC12"),
    ],
)
def test_post_and_short_url_normalization(source, expected):
    assert tiktok.normalize_tiktok_url(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "https://tiktok.com.attacker.example/@example/video/123",
        "https://attacker@www.tiktok.com/@example/video/123",
        "https://www.tiktok.com:443/@example/video/123",
        "https://www.tiktok.com./@example/video/123",
        "https://www.tiktok.com\\@example/video/123",
        "ftp://www.tiktok.com/@example/video/123",
        "https://www.tiktok.com/@example/video/0123",
        "https://www.tiktok.com/@example/video/0",
        "https://www.tiktok.com/@example/video/123/more",
        "https://www.tiktok.com/@example/video/123\n",
        "https://www.tiktok.com/@example",
        "https://www.tiktok.com/@example/live",
        "https://www.tiktok.com/music/song-123",
        "https://vm.tiktok.com/ABC/extra",
        "https://www.tiktok.com/t/",
        "https://www.tiktok.com/t/ABC/extra",
    ],
)
def test_unsupported_or_ambiguous_urls_make_no_requests(source, transport):
    assert tiktok.normalize_tiktok_url(source) is None
    assert tiktok.fetch_tiktok(source) is None
    assert not any(transport.values())


def test_video_keeps_full_text_and_uses_a_shared_deadline(transport, monkeypatch):
    long_text = "Абзац 🐱 <знаки> & ссылка https://example.org/\n\n" * 300
    original = tiktok.request_page

    def page(url, **options):
        original(url, **options)
        return url, html(payload(desc=long_text))

    monkeypatch.setattr(tiktok, "request_page", page)
    result = tiktok.fetch_tiktok(PAGE + "?tracking=private")
    assert result and result.text == long_text and result.author == "Космокот"
    assert result.username == "example" and result.author_url == "https://www.tiktok.com/@example"
    assert result.url == PAGE and result.assets[0].duration == 10.5
    page_url, options = transport["pages"][0]
    assert page_url == PAGE and options["allowed_hosts"] == tiktok._PAGE_HOSTS
    assert transport["videos"][0][1]["deadline"] == options["deadline"]
    assert transport["videos"][0][1]["max_duration"] == 180


@pytest.mark.parametrize("host_path", ["vt.tiktok.com/ABCD", "vm.tiktok.com/ABCD", "www.tiktok.com/t/ABCD"])
def test_short_links_accept_blank_handle_final_posts(host_path, transport, monkeypatch):
    calls = transport["pages"]

    def page(url, **options):
        calls.append((url, options))
        if len(calls) == 1:
            return f"https://www.tiktok.com/@/video/{POST_ID}?tracking=1", b""
        return url, html(payload())

    monkeypatch.setattr(tiktok, "request_page", page)
    result = tiktok.fetch_tiktok("https://" + host_path)
    assert result and result.url == PAGE
    assert calls[1][0] == f"https://www.tiktok.com/@_/video/{POST_ID}"
    assert calls[0][1]["deadline"] == calls[1][1]["deadline"]


@pytest.mark.parametrize("destination", ["https://www.tiktok.com/", "https://www.tiktok.com/login", "https://attacker.example/@/video/123"])
def test_short_links_to_non_posts_are_quiet(destination, transport, monkeypatch):
    monkeypatch.setattr(tiktok, "request_page", lambda *args, **kwargs: (destination, b""))
    assert tiktok.fetch_tiktok("https://vt.tiktok.com/ABCD") is None
    assert not transport["videos"] and not transport["images"]


def test_photo_album_uses_video_shaped_page_and_keeps_every_slide(transport, monkeypatch):
    item = album()
    item["contents"] = [{"desc": "Первый абзац"}, {"desc": "Второй абзац"}]
    calls = transport["pages"]

    def page(url, **options):
        calls.append((url, options))
        return url, html(item)

    monkeypatch.setattr(tiktok, "request_page", page)
    result = tiktok.fetch_tiktok(PHOTO + "?image_index=9")
    assert result and result.url == PHOTO and result.title == "Фотоальбом"
    assert result.text == "Первый абзац\n\nВторой абзац"
    assert [asset.data for asset in result.assets] == [f"{index}.jpg".encode() for index in range(12)]
    assert calls[0][0] == PAGE and not transport["videos"]
    assert all(options["allowed_hosts"] == tiktok._IMAGE_HOSTS for _, options in transport["images"])
    assert all(options["deadline"] == calls[0][1]["deadline"] for _, options in transport["images"])


def test_video_shaped_photo_post_is_classified_by_payload(transport, monkeypatch):
    monkeypatch.setattr(tiktok, "request_page", lambda url, **kwargs: (url, html(album(2))))
    result = tiktok.fetch_tiktok(PAGE)
    assert result and result.url == PHOTO and len(result.assets) == 2
    assert not transport["videos"]


def test_an_unavailable_slide_never_becomes_a_partial_album(transport, monkeypatch):
    monkeypatch.setattr(tiktok, "request_page", lambda url, **kwargs: (url, html(album(3))))
    download = tiktok.download_image

    def image(url, **options):
        return None if url.endswith("/1.jpg") else download(url, **options)

    monkeypatch.setattr(tiktok, "download_image", image)
    assert tiktok.fetch_tiktok(PHOTO) is None
    assert len(transport["images"]) == 1


def test_photo_tries_one_alternate_without_reordering_slides(transport, monkeypatch):
    item = album(1)
    item["imagePost"]["images"][0]["imageURL"]["urlList"] = [
        "https://p16-common-sign.tiktokcdn-eu.com/fail.jpg",
        "https://p19-common-sign.tiktokcdn-eu.com/good.jpg",
        "https://p19-common-sign.tiktokcdn-eu.com/unused.jpg",
    ]
    monkeypatch.setattr(tiktok, "request_page", lambda url, **kwargs: (url, html(item)))
    calls = []

    def image(url, **options):
        calls.append(url)
        return None if url.endswith("/fail.jpg") else LinkAsset("photo", b"good", 100, 200)

    monkeypatch.setattr(tiktok, "download_image", image)
    result = tiktok.fetch_tiktok(PHOTO)
    assert result and result.assets[0].data == b"good" and len(calls) == 2


@pytest.mark.parametrize("field", ["privateItem", "secret", "forFriend", "isContentClassified"])
def test_access_restrictions_prevent_media_downloads(field, transport, monkeypatch):
    monkeypatch.setattr(tiktok, "request_page", lambda url, **kwargs: (url, html(payload(**{field: True}))))
    assert tiktok.fetch_tiktok(PAGE) is None
    assert not transport["videos"] and not transport["images"]


def test_private_author_and_mismatched_post_ids_are_rejected(transport, monkeypatch):
    for item in (payload(author={"privateAccount": True}), payload(id="123")):
        monkeypatch.setattr(tiktok, "request_page", lambda url, _item=item, **kwargs: (url, html(_item)))
        assert tiktok.fetch_tiktok(PAGE) is None
    assert not transport["videos"]


@pytest.mark.parametrize("status", [10204, 10216, 10222, None, False])
def test_error_or_missing_status_is_not_a_success(status, transport, monkeypatch):
    monkeypatch.setattr(tiktok, "request_page", lambda url, **kwargs: (url, html(payload(), status=status)))
    assert tiktok.fetch_tiktok(PAGE) is None
    assert not transport["videos"]


@pytest.mark.parametrize("body", [b"<html>login shell</html>", b"\xff", b"x" * (2 * 1024 * 1024 + 1), html(payload()) * 2])
def test_missing_malformed_ambiguous_and_oversized_hydration_is_quiet(body, transport, monkeypatch):
    monkeypatch.setattr(tiktok, "request_page", lambda url, **kwargs: (url, body))
    assert tiktok.fetch_tiktok(PAGE) is None
    assert not transport["videos"] and not transport["images"]


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"<html>PRIVATE_PAYLOAD login shell</html>", LinkReason.MISSING_HYDRATION),
        (b"\xff", LinkReason.SCHEMA_MISMATCH),
        (html(payload()) * 2, LinkReason.SCHEMA_MISMATCH),
        (html(payload(author="PRIVATE_PAYLOAD invalid author")), LinkReason.SCHEMA_MISMATCH),
        (html(payload(), status=False), LinkReason.SCHEMA_MISMATCH),
        (html(payload(), status=10204), LinkReason.UNAVAILABLE),
        (html(payload(privateItem=True)), LinkReason.PRIVATE),
        (html(payload(author={"privateAccount": True})), LinkReason.PRIVATE),
        (html(payload(id="123")), LinkReason.ID_MISMATCH),
        (b"x" * (2 * 1024 * 1024 + 1), LinkReason.TOO_LARGE),
    ],
    ids=["missing", "utf8", "duplicate", "schema", "status-shape", "unavailable", "private-post", "private-author", "id", "oversize"],
)
def test_hydration_failures_have_one_safe_diagnostic(body, reason, transport, monkeypatch):
    monkeypatch.setattr(tiktok, "request_page", lambda url, **kwargs: (url, body))
    result = collect_link_diagnostics(tiktok.fetch_tiktok, PAGE)
    assert result.value is None
    assert [(item.stage, item.reason) for item in result.diagnostics] == [(LinkStage.ADAPTER, reason)]
    assert not transport["videos"] and not transport["images"]
    assert "PRIVATE_PAYLOAD" not in repr(result.diagnostics) and POST_ID not in repr(result.diagnostics)


def test_redirected_post_has_a_distinct_identity_diagnostic(transport, monkeypatch):
    monkeypatch.setattr(tiktok, "request_page", lambda url, **kwargs: (PAGE.replace(POST_ID, "123"), html(payload())))
    result = collect_link_diagnostics(tiktok.fetch_tiktok, PAGE)
    assert result.value is None
    assert [(item.stage, item.reason) for item in result.diagnostics] == [(LinkStage.ADAPTER, LinkReason.ID_MISMATCH)]
    assert not transport["videos"] and not transport["images"]


@pytest.mark.parametrize("count", [0, 51])
def test_empty_or_over_limit_albums_do_not_download_any_slides(count, transport, monkeypatch):
    monkeypatch.setattr(tiktok, "request_page", lambda url, **kwargs: (url, html(album(count))))
    assert tiktok.fetch_tiktok(PHOTO) is None
    assert not transport["images"] and not transport["videos"]


def test_album_bytes_share_a_single_budget(transport, monkeypatch):
    monkeypatch.setattr(tiktok, "_MAX_MEDIA_BYTES", 10)
    monkeypatch.setattr(tiktok, "request_page", lambda url, **kwargs: (url, html(album(2))))
    budgets = []

    def image(url, **options):
        budgets.append(options["max_bytes"])
        return LinkAsset("photo", b"123456", 100, 200)

    monkeypatch.setattr(tiktok, "download_image", image)
    assert tiktok.fetch_tiktok(PHOTO) is None
    assert budgets == [10, 4]


def test_expired_short_resolution_does_not_start_another_request(transport, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(tiktok.time, "monotonic", lambda: clock[0])
    calls = []

    def page(url, **options):
        calls.append(url)
        clock[0] = options["deadline"]
        return PAGE, html(payload())

    monkeypatch.setattr(tiktok, "request_page", page)
    assert tiktok.fetch_tiktok("https://vt.tiktok.com/ABCD") is None
    assert len(calls) == 1 and not transport["videos"]


def test_redirected_metadata_must_still_describe_the_requested_post(transport, monkeypatch):
    monkeypatch.setattr(tiktok, "request_page", lambda *args, **kwargs: (PAGE.replace(POST_ID, "123"), html(payload())))
    assert tiktok.fetch_tiktok(PAGE) is None
    assert not transport["videos"]


def test_long_video_or_failed_download_leaves_original_link_alone(transport, monkeypatch):
    monkeypatch.setattr(tiktok, "request_page", lambda url, **kwargs: (url, html(payload(video={"duration": 181}))))
    assert tiktok.fetch_tiktok(PAGE) is None
    assert not transport["videos"]
    monkeypatch.setattr(tiktok, "request_page", lambda url, **kwargs: (url, html(payload())))
    monkeypatch.setattr(tiktok, "download_video", lambda *args, **kwargs: None)
    assert tiktok.fetch_tiktok(PAGE) is None


def test_invalid_optional_author_handle_cannot_become_an_external_profile_link(transport, monkeypatch):
    item = copy.deepcopy(payload())
    item["author"]["uniqueId"] = "attacker.example/@example"
    monkeypatch.setattr(tiktok, "request_page", lambda url, **kwargs: (url, html(item)))
    result = tiktok.fetch_tiktok(PAGE)
    assert result and result.author_url is None and result.username is None and result.url == PAGE
