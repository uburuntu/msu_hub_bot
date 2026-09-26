"""Rich replies remain usable without making embedded content dispatch commands."""

import io
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from aiogram.types import RichBlockBlockQuotation, RichBlockParagraph, RichMessage, Update, UserProfilePhotos
from teleforge import App

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.features import captions
from msu_hub_bot.telegram.extraction import Extractor, SimpleExtractor
from msu_hub_bot.telegram.filters import MetaCommand, MetaInfo, SlashCommand
from msu_hub_bot.telegram.rich_input import rich_media, rich_text
from telegram_helpers import make_bot, make_message

PHOTO = {"file_id": "photo", "file_unique_id": "photo-unique", "width": 100, "height": 80}
VIDEO = {"file_id": "video", "file_unique_id": "video-unique", "width": 100, "height": 80, "duration": 3}
DOCUMENT = {"file_id": "document", "file_unique_id": "document-unique", "mime_type": "text/plain"}


@pytest.fixture
async def caption_app(monkeypatch):
    async def render(executor, media, renderer, text, style, **kwargs):
        result = io.BytesIO(b"video") if renderer is captions.caption_video else Image.new("RGB", (20, 20))
        return result, False

    downloaded = AsyncMock(side_effect=render)
    monkeypatch.setattr(captions, "run_downloaded", downloaded)
    executor = TPExecutor(1)
    app = App(data={"cpu_executor": executor}).include(captions.Captions())
    try:
        yield app, downloaded
    finally:
        await app.aclose()
        executor.shutdown(wait=False)


def test_visible_text_preserves_inline_lists_and_excludes_hidden_attributes():
    message = make_message(
        rich_message={
            "blocks": [
                {
                    "type": "paragraph",
                    "text": [
                        "Hello ",
                        {"type": "bold", "text": ["bold ", {"type": "italic", "text": "world"}]},
                        " ",
                        {"type": "url", "text": "source", "url": "https://example.com/hidden-command"},
                        " ",
                        {"type": "custom_emoji", "custom_emoji_id": "12345", "alternative_text": "😺"},
                        {"type": "anchor", "name": "hidden-anchor"},
                    ],
                },
                {"type": "pre", "text": "print('<&>')", "language": "python"},
                {"type": "mathematical_expression", "expression": "x^2"},
                {"type": "footer", "text": {"type": "url", "text": "Original", "url": "https://x.com/user/status/12345"}},
            ]
        }
    )

    assert rich_text(message) == "Hello bold world source 😺\n\nprint('<&>')\n\nx^2\n\nOriginal"


def test_text_in_details_lists_tables_quotes_and_captions_keeps_reading_order():
    message = make_message(
        rich_message={
            "blocks": [
                {"type": "heading", "text": "Title", "size": 1},
                {
                    "type": "details",
                    "summary": "Summary",
                    "blocks": [
                        {"type": "list", "items": [{"label": "1.", "blocks": [{"type": "paragraph", "text": "First"}]}]},
                        {
                            "type": "table",
                            "caption": "Results",
                            "cells": [
                                [{"align": "left", "valign": "top", "text": "Cat"}, {"align": "right", "valign": "top", "text": "75%"}]
                            ],
                        },
                    ],
                },
                {"type": "blockquote", "blocks": [{"type": "paragraph", "text": "Quoted"}], "credit": "Author"},
                {"type": "photo", "photo": [PHOTO], "caption": {"text": "Photo caption", "credit": "Photographer"}},
            ]
        }
    )

    text = rich_text(message)
    assert text.split() == [
        "Title",
        "Summary",
        "1.",
        "First",
        "Results",
        "Cat",
        "75%",
        "Quoted",
        "Author",
        "Photo",
        "caption",
        "Photographer",
    ]
    assert "Cat\t75%" in text


def test_media_walk_visits_nested_containers_in_display_order():
    message = make_message(
        rich_message={
            "blocks": [
                {
                    "type": "collage",
                    "blocks": [
                        {"type": "photo", "photo": [{**PHOTO, "file_id": "small"}, PHOTO]},
                        {"type": "slideshow", "blocks": [{"type": "video", "video": VIDEO}]},
                    ],
                },
                {"type": "blockquote", "blocks": [{"type": "animation", "animation": {**VIDEO, "file_id": "animation"}}]},
                {"type": "details", "summary": "Files", "blocks": [{"type": "document", "document": DOCUMENT}]},
                {
                    "type": "list",
                    "items": [{"label": "•", "blocks": [{"type": "photo", "photo": [{**PHOTO, "file_id": "listed"}]}]}],
                },
            ]
        }
    )

    assert [media.file_id for media in rich_media(message)] == ["photo", "video", "animation", "document", "listed"]


@pytest.mark.parametrize("command", ["meme", "lobster", "demotivator"])
@pytest.mark.parametrize("kind", ["photo", "video", "animation"])
async def test_caption_commands_choose_rich_reply_media_before_bot_profile(monkeypatch, caption_app, command, kind):
    bot = make_bot()
    profile = AsyncMock(side_effect=AssertionError("Selected media must not request the bot avatar"))
    monkeypatch.setattr(bot, "get_user_profile_photos", profile)
    media = {"photo": [PHOTO], "video": VIDEO, "animation": {**VIDEO, "file_id": "animation"}}[kind]
    reply = make_message(
        bot,
        message_id=2,
        from_user={"id": bot.id, "is_bot": True, "first_name": "Bot"},
        rich_message={"blocks": [{"type": "collage", "blocks": [{"type": kind, kind: media}]}]},
    )
    message = make_message(bot, text=f"/{command} Some text", reply_to_message=reply)
    app, downloaded = caption_app
    await app.feed_update(bot, Update(update_id=1, message=message))

    assert downloaded.call_args.args[1].file_id == kind
    assert downloaded.call_args.args[2] is (captions.caption_image if kind == "photo" else captions.caption_video)
    assert bot.session.methods[-1].reply_parameters.message_id == reply.message_id
    profile.assert_not_awaited()


async def test_explicit_origin_media_and_ordinary_photo_keep_precedence(caption_app):
    bot = make_bot()
    reply = make_message(bot, rich_message={"blocks": [{"type": "video", "video": VIDEO}]})
    message = make_message(bot, photo=[PHOTO], caption="/meme Label", reply_to_message=reply)
    app, downloaded = caption_app
    await app.feed_update(bot, Update(update_id=1, message=message))
    assert downloaded.call_args.args[1].file_id == "photo"
    assert downloaded.call_args.args[2] is captions.caption_image
    assert bot.session.methods[-1].reply_parameters.message_id == message.message_id

    message = make_message(photo=[PHOTO], rich_message={"blocks": [{"type": "photo", "photo": [{**PHOTO, "file_id": "rich-photo"}]}]})
    assert (await SimpleExtractor.image(message)).file_id == "photo"


async def test_image_and_document_extractors_use_nested_documents():
    image_document = {**DOCUMENT, "mime_type": "image/png", "file_id": "image-document"}
    message = make_message(
        rich_message={"blocks": [{"type": "document", "document": DOCUMENT}, {"type": "document", "document": image_document}]}
    )

    assert (await SimpleExtractor.document(message)).file_id == "document"
    assert (await SimpleExtractor.image(message)).file_id == "image-document"


async def test_rich_without_media_still_uses_existing_profile_fallback(monkeypatch):
    bot = make_bot()
    profile = AsyncMock(return_value=UserProfilePhotos(total_count=1, photos=[[PHOTO]]))
    monkeypatch.setattr(bot, "get_user_profile_photos", profile)
    reply = make_message(bot, rich_message={"blocks": [{"type": "paragraph", "text": "Plain post"}]})
    message = make_message(bot, text="/filter", reply_to_message=reply)

    target, selected = await Extractor.image(message, with_profile_photo=True)

    assert target == reply
    assert selected.file_id == "photo"
    profile.assert_awaited_once()


async def test_rich_reply_text_is_explicit_input_and_never_a_command():
    bot = make_bot()
    reply = make_message(
        bot,
        rich_message={
            "blocks": [
                {"type": "paragraph", "text": {"type": "bot_command", "text": "/tr Words", "bot_command": "/tr"}},
                {"type": "footer", "text": {"type": "url", "text": "Source", "url": "https://example.com/#tr"}},
            ]
        },
    )
    assert await MetaCommand("tr")(reply, bot) is False
    assert await SlashCommand("tr")(reply, bot) is False

    message = make_message(bot, text="/tr", reply_to_message=reply)
    parsed = await MetaCommand("tr")(message, bot)
    assert parsed["meta"].extract_text() == (reply, "/tr Words\n\nSource")

    message = make_message(bot, text="/tr My own text", reply_to_message=reply)
    parsed = await MetaCommand("tr")(message, bot)
    assert parsed["meta"].extract_text() == (message, "My own text")


def test_rich_text_document_preserves_explicit_document_input_policy():
    reply = make_message(rich_message={"blocks": [{"type": "document", "document": DOCUMENT, "caption": {"text": "Caption"}}]})
    message = make_message(text="/tr", reply_to_message=reply)
    meta = MetaInfo(message)

    assert meta.extract_text() == (reply, "Caption")
    target, text, document = meta.extract_text_with_doc()
    assert target == reply
    assert text == ""
    assert document.file_id == "document"


def test_deep_rich_content_cannot_recurse_or_hide_later_siblings():
    block = RichBlockParagraph(text="Deep content")
    for _ in range(200):
        block = RichBlockBlockQuotation.model_construct(blocks=[block])
    rich = RichMessage.model_construct(blocks=[block, RichBlockParagraph(text="Readable sibling")])
    message = make_message().model_copy(update={"rich_message": rich})

    assert rich_text(message) == "Readable sibling"


def test_cyclic_rich_text_is_bounded_and_later_siblings_remain_readable():
    cycle = []
    cycle.append(cycle)
    rich = RichMessage.model_construct(blocks=[RichBlockParagraph.model_construct(text=cycle), RichBlockParagraph(text="Readable sibling")])
    message = make_message().model_copy(update={"rich_message": rich})

    assert rich_text(message) == "Readable sibling"


@pytest.mark.parametrize("text", ["x" * 100000, [""] * 100000], ids=["long-string", "many-empty-nodes"])
def test_oversized_visible_text_and_empty_nodes_are_bounded(text):
    message = make_message().model_copy(
        update={"rich_message": RichMessage.model_construct(blocks=[RichBlockParagraph.model_construct(text=text)])}
    )

    assert len(rich_text(message)) <= 65536


def test_ordinary_message_has_no_rich_content():
    message = make_message(text="Unrelated plain message")
    assert rich_text(message) == ""
    assert list(rich_media(message)) == []
