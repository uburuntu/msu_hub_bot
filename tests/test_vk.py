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


def test_multiple_copied_records_keep_original_link_for_remaining_content():
    parsed = parse_post(
        copy_history=[
            {"id": 2, "owner_id": -20, "text": "First copied entry"},
            {"id": 3, "owner_id": -30, "text": "Other copied entry"},
        ]
    )
    assert "First copied entry" in parsed.render()
    assert "Все вложения — в VK" in parsed.render()


def test_untrusted_document_host_never_becomes_direct_media():
    parsed = parse_post(
        attachments=[attachment("doc", {"id": 1, "owner_id": -10, "url": "https://untrusted.test/file.gif", "ext": "gif", "size": 100})]
    )
    _, preview, photos, videos = parsed.for_publish(with_webpreview=False)
    assert not preview and not photos and not videos
    assert "https://vk.com/doc-10_1" in parsed.render()
