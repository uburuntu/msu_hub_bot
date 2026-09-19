from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

from msu_hub_bot.providers.vk import publish
from msu_hub_bot.providers.vk.posts import best_photo
from msu_hub_bot.providers.vk.models import Photo
from msu_hub_bot.providers.vk.utils import href, prepare_vk_text, safe_url, utf16_length


import pytest

from msu_hub_bot.providers.vk.posts import VkPost


@pytest.mark.parametrize("with_header", [True, False])
@pytest.mark.parametrize("repost", [True, False])
def test_synthetic_vk_response_rendering(with_header, repost):
    post = {"id": 1, "owner_id": -10, "date": 1_700_000_000, "text": "Текст <unsafe> & [id20|пример]", "attachments": []}
    if repost:
        post["copy_history"] = [{"id": 2, "owner_id": 20, "date": 1_700_000_000, "text": "Synthetic repost", "attachments": []}]
    response = {
        "items": [post],
        "groups": [{"id": 10, "name": "Example group", "screen_name": "example"}],
        "profiles": [{"id": 20, "first_name": "Example", "last_name": "User", "screen_name": "example_user"}],
    }
    parsed = VkPost.from_response(response)[0]
    rendered = parsed.render(with_header=with_header)
    assert "<unsafe>" not in rendered
    assert "Synthetic repost" in rendered if repost else "Текст" in rendered
    assert parsed.owner_id == -10


class TelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.open = False
        self.text = ""
        self.links = []

    def handle_starttag(self, tag, attrs):
        assert tag == "a" and not self.open
        assert len(attrs) == 1 and attrs[0][0] == "href"
        assert safe_url(attrs[0][1])
        self.open = True
        self.links.append(attrs[0][1])

    def handle_endtag(self, tag):
        assert tag == "a" and self.open
        self.open = False

    def handle_data(self, text):
        self.text += text


def parse_post(**fields):
    return VkPost.from_response({"items": [{"id": 1, "owner_id": -10, "date": 100, **fields}]})[0]


def attachment(kind, data):
    return {"type": kind, kind: data}


def check_html(text):
    parser = TelegramHTML()
    parser.feed(text)
    parser.close()
    assert not parser.open
    return parser


def test_empty_history_missing_extended_and_unknown_fields_are_safe():
    parsed = parse_post(text="A", copy_history=[], new_provider_field={"anything": True})
    assert not parsed.is_repost
    assert "Группа 10" in parsed.render()
    assert "https://vk.com/club10" in parsed.render()


def test_bad_or_deleted_post_does_not_hide_healthy_neighbour():
    items = [{"id": 2}, {"id": 3, "owner_id": -10, "is_deleted": True}, {"id": 1, "owner_id": -10, "text": "OK"}]
    assert [post.id for post in VkPost.from_response({"items": items, "groups": [{"bad": True}]})] == [1]


def test_current_photo_chooses_largest_telegram_compatible_size():
    photo = Photo.model_validate(
        {
            "sizes": [
                {"type": "z", "width": 1080, "height": 720, "url": "https://sun9.userapi.com/large.jpg"},
                {"type": "y", "width": 807, "height": 600, "url": "https://sun9.userapi.com/small.jpg"},
                {"width": 9000, "height": 9000, "url": "https://sun9.userapi.com/too-big.jpg"},
            ],
            "orig_photo": {"width": 1600, "height": 1200, "url": "https://sun9.userapi.com/original.jpg"},
        }
    )
    assert best_photo(photo).endswith("original.jpg")


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://user:pass@vk.com/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "https://host.local/",
        'https://vk.com/a" onclick="oops',
        "https://vk.com/\nattack",
    ],
)
def test_unsafe_urls_cannot_become_html_links_or_media(url):
    assert not safe_url(url)
    assert "<a" not in href(url, "<name & value>")
    assert href(url, "<name & value>") == "&lt;name &amp; value&gt;"


def test_markup_escapes_raw_targets_labels_and_text_once():
    text = prepare_vk_text("A & B [id20|<Admin> & friend] [javascript:alert(1)|click] [https://vk.com/a?a=1&b=2|link]")
    parsed = check_html(text)
    assert parsed.text == "A & B <Admin> & friend click link"
    assert parsed.links == ["https://vk.com/id20", "https://vk.com/a?a=1&b=2"]
    assert "&amp;amp;" not in text


def test_attachment_rendering_escapes_all_user_fields_and_retains_source_fallback():
    parsed = parse_post(
        attachments=[
            attachment("video", {"id": 1, "owner_id": -10, "title": "<title & video>"}),
            attachment("doc", {"id": 2, "owner_id": -10, "title": "<doc>", "ext": "pdf"}),
            attachment("poll", {"id": 3, "owner_id": -10, "question": "Q <&>", "answers": [{"text": "<answer>"}]}),
            attachment("market", {"id": 4, "owner_id": -10, "title": "Item", "price": {"text": "<script>"}}),
            attachment("pretty_cards", {"cards": [{"link_url": "https://vk.com/card", "title": "Card", "price": "<&>"}]}),
            attachment("place", {"title": "Some place"}),
            attachment("photo", {"sizes": []}),
            attachment("market", False),
            {"type": "poll"},
            None,
            "invalid attachment",
        ]
    )
    rendered = parsed.render()
    assert "Все вложения — в VK" in rendered
    visible = check_html(rendered).text
    assert "<title & video>" in visible and "<script>" in visible and "<answer>" in visible


@pytest.mark.parametrize("text", ["😀" * 10_000, "<&>" * 10_000, "[id20|" + "x" * 50_000 + "]"])
def test_long_posts_are_intact_bounded_excerpts_with_source(text):
    rendered = parse_post(text=text).render()
    parsed = check_html(rendered)
    assert utf16_length(rendered) <= 3500
    assert utf16_length(parsed.text) <= 3500
    assert parsed.links[-1] == "https://vk.com/wall-10_1"
    assert parsed.text.endswith("Читать целиком в VK →")


def test_media_combined_count_and_repeated_publication_are_stable():
    photos = [attachment("photo", {"sizes": [{"url": f"https://sun9.userapi.com/{i}.jpg"}]}) for i in range(8)]
    docs = [
        attachment("doc", {"id": i + 1, "owner_id": -10, "url": f"https://vk.com/{i}.mp4", "ext": "mp4", "size": 100}) for i in range(8)
    ]
    parsed = parse_post(text="Example", attachments=photos + docs)
    first = parsed.for_publish()
    assert first == parsed.for_publish()
    assert len(first[2]) + len(first[3]) == 10
    assert "Все вложения" in first[0]


async def test_caption_budget_uses_utf16_not_python_character_count(monkeypatch):
    monkeypatch.setattr(publish, "settings", SimpleNamespace(vk_default_chat_id=-10))
    bot = SimpleNamespace(send_super_message=AsyncMock(), send_super_message_prefer_album=AsyncMock())
    parsed = parse_post(text="😀" * 600, attachments=[attachment("photo", {"sizes": [{"url": "https://sun9.userapi.com/image.jpg"}]})])
    await publish.publish_vk_post(parsed, bot, -10)
    bot.send_super_message.assert_awaited_once()
    bot.send_super_message_prefer_album.assert_not_awaited()


def test_multiple_copied_records_keep_each_body_and_original_source():
    parsed = parse_post(
        copy_history=[
            {"id": 2, "owner_id": -20, "text": "First copied entry"},
            {"id": 3, "owner_id": -30, "text": "Other copied entry"},
        ]
    )
    assert "First copied entry" in parsed.render()
    assert "Other copied entry" in parsed.render()
    assert "https://vk.com/wall-20_2" in parsed.render()
    assert "https://vk.com/wall-30_3" in parsed.render()


def test_untrusted_document_host_never_becomes_direct_media():
    parsed = parse_post(
        attachments=[attachment("doc", {"id": 1, "owner_id": -10, "url": "https://untrusted.test/file.gif", "ext": "gif", "size": 100})]
    )
    _, preview, photos, videos = parsed.for_publish(with_webpreview=False)
    assert not preview and not photos and not videos
    assert "https://vk.com/doc-10_1" in parsed.render()


@pytest.mark.parametrize(
    "kind,data,expected,media",
    [
        ("photo", {"sizes": [{"url": "https://sun9.userapi.com/photo.jpg"}]}, "photo.jpg", True),
        ("posted_photo", {"photo_100": "https://sun9.userapi.com/posted.jpg"}, "posted.jpg", True),
        ("graffiti", {"photo_100": "https://sun9.userapi.com/graffiti.jpg"}, "graffiti.jpg", True),
        ("app", {"photo_100": "https://sun9.userapi.com/app.jpg"}, "app.jpg", True),
        ("video", {"id": 1, "owner_id": -10, "title": "Video"}, "video-10_1", False),
        ("audio", {"artist": "Artist", "title": "Track"}, "Artist — Track", False),
        ("doc", {"id": 1, "owner_id": -10, "title": "Document", "ext": "pdf"}, "doc-10_1", False),
        ("link", {"url": "https://example.org/story", "title": "Story"}, "example.org/story", False),
        ("note", {"view_url": "https://vk.com/note1_2", "title": "Note"}, "note1_2", False),
        ("poll", {"id": 1, "owner_id": -10, "question": "Question", "answers": [{"text": "Answer"}]}, "Answer", False),
        ("page", {"view_url": "https://vk.com/page-10_1", "title": "Wiki"}, "page-10_1", False),
        ("album", {"id": 1, "owner_id": -10, "title": "Album"}, "album-10_1", False),
        ("photos_list", [], "Все вложения — в VK", False),
        ("market", {"id": 1, "owner_id": -10, "title": "Product"}, "product-10_1", False),
        ("market_album", {"id": 1, "owner_id": -10, "title": "Products"}, "section=album_1", False),
        ("pretty_cards", {"cards": [{"link_url": "https://example.org/card", "title": "Card"}]}, "example.org/card", False),
        ("event", {"id": 1}, "club1", False),
    ],
)
def test_all_legacy_attachment_categories_keep_media_text_or_source_fallback(kind, data, expected, media):
    parsed = parse_post(attachments=[attachment(kind, data)])
    text, _, photos, videos = parsed.for_publish(with_webpreview=False)
    check_html(text)
    assert expected in (" ".join(photos + videos) if media else text)


def test_video_playlist_and_attached_community_keep_links_labels_and_counts():
    parsed = parse_post(
        attachments=[
            attachment("video_playlist", {"owner_id": -10, "id": 12, "title": "<Playlist & clips>", "count": 7}),
            attachment("group", {"id": 20, "text": "<Community>", "status": "Friends & games", "size": 42}),
        ]
    )
    rendered = parsed.render()
    visible = check_html(rendered)
    assert "<Playlist & clips>" in visible.text and "видео: 7" in visible.text
    assert "<Community>" in visible.text and "Friends & games" in visible.text and "участников: 42" in visible.text
    assert "https://vk.com/video/playlist/-10_12" in visible.links
    assert "https://vk.com/club20" in visible.links
    assert "Все вложения" not in visible.text


@pytest.mark.parametrize(
    "geo",
    [
        {"coordinates": "55.5 37.5", "place": {"title": "<A & B>", "address": "Street 1"}},
        {"coordinates": {"latitude": 55.5, "longitude": 37.5}, "place": {"title": "<A & B>", "address": "Street 1"}},
        {"place": {"title": "<A & B>", "address": "Street 1", "latitude": 55.5, "longitude": 37.5}},
    ],
)
def test_geo_place_keeps_label_and_validated_map_link(geo):
    rendered = parse_post(text="Public location", geo=geo).render()
    visible = check_html(rendered)
    assert "<A & B>, Street 1" in visible.text
    assert "https://maps.google.com/maps?q=55.5,37.5&z=16" in visible.links


@pytest.mark.parametrize(
    "geo",
    [
        {"coordinates": "nan 37"},
        {"coordinates": "91 181"},
        {"coordinates": "55 37 100"},
        {"coordinates": {"latitude": 999, "longitude": 37}},
        {"coordinates": {"latitude": True, "longitude": 37}},
        "changed optional provider object",
    ],
)
def test_invalid_geo_does_not_discard_post_or_generate_map_link(geo):
    visible = check_html(parse_post(text="Keep this post", geo=geo).render())
    assert "Keep this post" in visible.text
    assert not any("maps.google.com" in url for url in visible.links)


def test_hidden_map_keeps_only_the_public_place_label():
    visible = check_html(parse_post(geo={"coordinates": "55.5 37.5", "showmap": 0, "place": {"title": "Place"}}).render())
    assert "Place" in visible.text and "55.5" not in visible.text
    assert not any("maps.google.com" in url for url in visible.links)


@pytest.mark.parametrize("field", ["from_id", "signer_id"])
@pytest.mark.parametrize("value", [{"id": 42}, "changed optional author", True, 0])
def test_malformed_optional_author_does_not_hide_public_text_or_media(field, value):
    parsed = parse_post(
        text="Keep the post",
        attachments=[attachment("photo", {"sizes": [{"url": "https://sun9.userapi.com/public.jpg"}]})],
        **{field: value},
    )
    rendered, _, photos, _ = parsed.for_publish(with_webpreview=False)
    assert "Keep the post" in check_html(rendered).text
    assert "— Автор:" not in rendered
    assert photos == ["https://sun9.userapi.com/public.jpg"]


def test_nested_and_sibling_copies_preserve_text_media_and_attribution():
    parsed = parse_post(
        text="Outer text",
        from_id=99,
        copy_history=[
            {
                "id": 2,
                "owner_id": -20,
                "text": "First copy",
                "signer_id": 21,
                "copy_history": [
                    {
                        "id": 3,
                        "owner_id": -30,
                        "text": "Original text",
                        "copyright": {"name": "Original credit", "link": "https://example.org/source"},
                        "attachments": [attachment("photo", {"sizes": [{"url": "https://sun9.userapi.com/original.jpg"}]})],
                    },
                ],
            },
            {"id": 4, "owner_id": -40, "text": "Second copy", "attachments": [attachment("audio", {"artist": "Artist", "title": "Song"})]},
        ],
    )
    rendered, _, photos, _ = parsed.for_publish(with_webpreview=False)
    visible = check_html(rendered)
    for text in ("Outer text", "First copy", "Original text", "Second copy", "Original credit", "Artist — Song"):
        assert text in visible.text
    assert visible.links.count("https://vk.com/wall-30_3") == 1
    assert {"https://vk.com/id99", "https://vk.com/id21", "https://example.org/source"} <= set(visible.links)
    assert photos == ["https://sun9.userapi.com/original.jpg"]
    assert "Все вложения" not in visible.text


def test_duplicate_copied_source_is_rendered_once_and_depth_is_bounded():
    deep = {"id": 5, "owner_id": -50, "text": "Too deep"}
    nested = {"id": 3, "owner_id": -30, "text": "Once only", "copy_history": [deep]}
    first = {"id": 2, "owner_id": -20, "text": "Parent", "copy_history": [nested]}
    parsed = parse_post(copy_history=[first, nested])
    visible = check_html(parsed.render())
    assert visible.text.count("Once only") == 1
    assert "Too deep" not in visible.text
    assert "Все вложения — в VK" in visible.text


def test_branching_history_and_media_stay_bounded_with_original_source_link():
    copies = [
        {
            "id": i,
            "owner_id": -i,
            "text": "Copy",
            "copy_history": [
                {
                    "id": i * 100 + j,
                    "owner_id": -i,
                    "text": "Nested",
                    "attachments": [attachment("photo", {"sizes": [{"url": f"https://sun9.userapi.com/{i}-{j}.jpg"}]})],
                }
                for j in range(10)
            ],
        }
        for i in range(2, 12)
    ]
    parsed = parse_post(copy_history=copies)
    rendered, _, photos, videos = parsed.for_publish(with_webpreview=False)
    assert len(parsed.history) == 10
    assert len(photos) + len(videos) <= 10
    assert utf16_length(rendered) <= 3500
    assert "Все вложения — в VK" in check_html(rendered).text


def test_restricted_copied_content_never_leaks_from_offline_parser():
    parsed = parse_post(
        copy_history=[
            {
                "id": 2,
                "owner_id": -20,
                "text": "private-canary",
                "friends_only": 1,
                "geo": {"coordinates": "55.5 37.5"},
                "attachments": [attachment("photo", {"sizes": [{"url": "https://sun9.userapi.com/private-canary.jpg"}]})],
            }
        ]
    )
    result = parsed.for_publish(with_webpreview=False)
    assert "private-canary" not in str(result)
    assert "maps.google.com" not in result[0]
    assert "Все вложения — в VK" in result[0]
