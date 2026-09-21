"""Language catalogue normalization and exact translation-body boundaries."""

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientError
from aiogram import Dispatcher, Router
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import StateFilter
from aiogram.methods import GetFile, SendMessage
from aiogram.types import PhotoSize, Update

from msu_hub_bot.commands import lingvanex
from msu_hub_bot.media.limits import MAX_DOWNLOAD_BYTES
from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.providers.jev import JevError, JevErrorReason, JevLanguages, MAX_LANGUAGE_TEXT
from msu_hub_bot.providers.language_resolution import LanguageCatalogue, LanguageResolutionError, translation_body
from msu_hub_bot.telegram.command_api import invoke_command, register_command
from msu_hub_bot.telegram.filters import MetaCommand as CommandFilter
from msu_hub_bot.telegram.recent_context import RecentMessages
from telegram_helpers import make_bot, make_message


LANGUAGES = [
    {"full_code": "en_GB", "code_alpha_1": "en", "codeName": "English", "englishName": "English (British)"},
    {"full_code": "en_US", "code_alpha_1": "en", "codeName": "American", "englishName": "English (American)"},
    {"full_code": "ru_RU", "code_alpha_1": "ru", "codeName": "Russian", "englishName": "Russian"},
]


def test_catalogue_normalizes_aliases_and_keeps_regional_choices():
    catalogue = LanguageCatalogue.from_provider(LANGUAGES)
    assert catalogue.normalize("EN") == "en_GB"
    assert catalogue.normalize("en-US") == "en_US"
    assert catalogue.normalize(" Russian ") == "ru_RU"
    assert catalogue.normalize("unsupported") is None
    assert catalogue.normalize(None) is None
    assert set(catalogue.choices) == {"en_GB", "en_US", "ru_RU"}


@pytest.mark.parametrize("data", [None, {}, [], ["bad"], [{"full_code": "bad code"}], [{"full_code": "none"}], LANGUAGES * 200])
def test_invalid_catalogue_is_a_fixed_error(data):
    with pytest.raises(LanguageResolutionError, match="^Language arguments could not be resolved$"):
        LanguageCatalogue.from_provider(data)


@pytest.mark.parametrize("instruction", ["на русский", "переведи на русский", "translate into Russian", "", "English Russian"])
def test_natural_request_uses_exact_reply_not_instruction_remainder(instruction):
    result = translation_body(instruction, "  Hello\nworld  ", known_source=False, known_target=False)
    assert result.text == "  Hello\nworld  " and result.from_reply
    assert result.request == instruction


@pytest.mark.parametrize(
    ("instruction", "text", "control"),
    [
        ("на русский: Hello\nworld  ", "Hello\nworld  ", "на русский"),
        ('переведи "  Hello  " на русский', "  Hello  ", "переведи  на русский"),
        ("переведи «Hello» на русский", "Hello", "переведи  на русский"),
        ("auto ru Hello\nworld", "Hello\nworld", "auto ru"),
    ],
)
def test_proven_inline_body_takes_precedence_over_reply(instruction, text, control):
    result = translation_body(instruction, "different replied text", known_source=False, known_target=False)
    assert result.text == text and result.request == control and not result.from_reply


def test_plain_inline_text_preserves_all_content():
    result = translation_body("Hello\nworld  ", None, known_source=False, known_target=False)
    assert result.text == "Hello\nworld  "
    result = translation_body("The cat sat on the mat", None, known_source=False, known_target=False)
    assert result.text == "The cat sat on the mat"


def test_one_known_language_still_requires_a_proven_inline_body_boundary():
    assert translation_body("en русский Hello", None, known_source=True, known_target=False) is None
    assert translation_body("en Hello world", None, known_source=True, known_target=False) is None
    result = translation_body("en русский: Hello world", None, known_source=True, known_target=False)
    assert result.text == "Hello world"


@pytest.mark.parametrize("instruction", ["на русский", "переведи Hello на русский", "translate Hello into Russian", ""])
def test_ambiguous_inline_instruction_is_never_used_as_translation_body(instruction):
    assert translation_body(instruction, None, known_source=False, known_target=False) is None


@pytest.fixture
async def translation(monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(lingvanex, "languages_list", AsyncMock(return_value=LANGUAGES))
    monkeypatch.setattr(lingvanex, "translate", AsyncMock(return_value="Translation <&>"))
    monkeypatch.setattr(lingvanex, "translate_image", AsyncMock(return_value="Image <&>"))
    client = SimpleNamespace(resolve_languages=AsyncMock(return_value=JevLanguages(source="en_GB", target="ru_RU")))
    yield bot, client
    await bot.session.close()


@pytest.mark.parametrize("keyword", ["tr", "translate"])
async def test_explicit_language_codes_bypass_jev_and_preserve_literal_inline_text(translation, keyword):
    bot, client = translation
    message = make_message(bot, text=f"/{keyword} EN ru Hello <b>&world</b>", message_id=20)
    await invoke_command(lingvanex.process_translate, message, jev=client)
    client.resolve_languages.assert_not_awaited()
    lingvanex.translate.assert_awaited_once_with("Hello <b>&world</b>", "en_GB", "ru_RU")
    sent = bot.session.methods[-1]
    assert isinstance(sent, SendMessage) and sent.text == "Translation <&>" and sent.parse_mode is None
    assert sent.reply_parameters.message_id == 20


@pytest.mark.parametrize("source", ["auto", "detect"])
@pytest.mark.parametrize("body", ["See https://example.com", "Note: keep this prefix", 'He said "hello" to me'])
async def test_detected_source_preserves_punctuation_in_the_complete_inline_body(translation, source, body):
    bot, client = translation
    reply = make_message(bot, text="different replied text", message_id=10)
    message = make_message(bot, text=f"/tr {source} ru {body}", message_id=20, reply_to_message=reply)
    await invoke_command(lingvanex.process_translate, message, jev=client)
    args, kwargs = client.resolve_languages.call_args
    assert args[0] == f"{source} ru"
    assert kwargs["text"] == body and kwargs["target"] == "ru_RU"
    lingvanex.translate.assert_awaited_once_with(body, "en_GB", "ru_RU")
    assert bot.session.methods[-1].reply_parameters.message_id == 20


async def test_natural_reply_request_keeps_exact_content_and_five_prior_human_messages(translation):
    bot, client = translation
    recent = RecentMessages()
    topic = {"message_thread_id": 7, "is_topic_message": True}
    for index in range(1, 7):
        recent.remember(make_message(bot, text=f"Разговор {index}", message_id=index, **topic))
    source_text = "  Hello\nworld & <things>  "
    source = make_message(bot, text=source_text, message_id=10, **topic)
    recent.remember(source)
    message = make_message(bot, text="/tr переведи на русский", message_id=20, reply_to_message=source, **topic)
    recent.remember(message)
    await invoke_command(lingvanex.process_translate, message, jev=client, recent_messages=recent)
    args, kwargs = client.resolve_languages.call_args
    assert args[0] == "переведи на русский"
    assert kwargs["text"] == source_text
    assert kwargs["recent_messages"] == tuple(f"Разговор {index}" for index in range(2, 7))
    assert kwargs["source"] is None and kwargs["target"] is None
    lingvanex.translate.assert_awaited_once_with(source_text, "en_GB", "ru_RU")
    sent = bot.session.methods[-1]
    assert sent.reply_parameters.message_id == 10 and sent.message_thread_id == 7


async def test_native_dispatch_keeps_raw_tail_when_existing_filter_consumes_two_arguments(translation):
    bot, client = translation
    router = Router()
    register_command(router.message, lingvanex.process_translate, CommandFilter("translate", "tr", args=2), StateFilter(None))
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.include_router(router)
    source = make_message(bot, text="Hello", message_id=10)
    message = make_message(bot, text="/translate переведи на русский", message_id=20, reply_to_message=source)
    await dispatcher.feed_update(bot, Update(update_id=1, message=message), jev=client)
    assert client.resolve_languages.call_args.args[0] == "переведи на русский"
    lingvanex.translate.assert_awaited_once_with("Hello", "en_GB", "ru_RU")
    assert len(bot.session.methods) == 1


async def test_inline_boundary_preserves_body_and_explicit_source(translation):
    bot, client = translation
    client.resolve_languages.return_value = JevLanguages(source="ru_RU", target="ru_RU")
    reply = make_message(bot, text="different replied text", message_id=10)
    message = make_message(bot, text="/tr en на русский: Hello\nworld", message_id=20, reply_to_message=reply)
    await invoke_command(lingvanex.process_translate, message, jev=client)
    kwargs = client.resolve_languages.call_args.kwargs
    assert kwargs["source"] == "en_GB" and kwargs["target"] is None and kwargs["text"] == "Hello\nworld"
    lingvanex.translate.assert_awaited_once_with("Hello\nworld", "en_GB", "ru_RU")
    assert bot.session.methods[-1].reply_parameters.message_id == 20


async def test_short_ordinary_words_are_not_consumed_as_language_codes(translation):
    bot, client = translation
    message = make_message(bot, text="/tr The cat sat on the mat", message_id=20)
    await invoke_command(lingvanex.process_translate, message, jev=client)
    assert client.resolve_languages.call_args.kwargs["text"] == "The cat sat on the mat"
    lingvanex.translate.assert_awaited_once_with("The cat sat on the mat", "en_GB", "ru_RU")


async def test_hashtag_body_is_not_reparsed_as_positional_control_text(translation):
    bot, client = translation
    message = make_message(bot, text="Hello #tr_en__auto world", message_id=20)
    await invoke_command(lingvanex.process_translate, message, jev=client)
    assert client.resolve_languages.call_args.kwargs["source"] == "en_GB"
    lingvanex.translate.assert_awaited_once_with("Hello  world", "en_GB", "ru_RU")


async def test_long_selected_text_is_sampled_only_for_detection_not_for_translation(translation):
    bot, client = translation
    full = "Hello " * 700
    source = make_message(bot, text=full, message_id=10)
    message = make_message(bot, text="/tr на русский", message_id=20, reply_to_message=source)
    await invoke_command(lingvanex.process_translate, message, jev=client)
    assert client.resolve_languages.call_args.kwargs["text"] == full[:MAX_LANGUAGE_TEXT]
    assert lingvanex.translate.call_args.args[0] == full


@pytest.mark.parametrize("failure", ["disabled", "unknown", "unsupported", "timeout", "unavailable", "no_body"])
async def test_unresolved_or_unavailable_resolution_gives_guidance_without_translation(translation, failure):
    bot, client = translation
    reply = make_message(bot, text="Hello", message_id=10)
    if failure == "unknown":
        client.resolve_languages.return_value = JevLanguages(source="en_GB", target=None)
    elif failure == "unsupported":
        client.resolve_languages.return_value = JevLanguages(source="en_GB", target="made_up")
    elif failure in {"timeout", "unavailable"}:
        client.resolve_languages.side_effect = JevError(JevErrorReason(failure))
    message = make_message(bot, text="/tr на русский", message_id=20, reply_to_message=None if failure == "no_body" else reply)
    await invoke_command(lingvanex.process_translate, message, jev=None if failure == "disabled" else client)
    lingvanex.translate.assert_not_awaited()
    lingvanex.translate_image.assert_not_awaited()
    assert len(bot.session.methods) == 1
    assert bot.session.methods[0].text == lingvanex.TRANSLATE_GUIDANCE
    assert bot.session.methods[0].reply_parameters.message_id == 20
    if failure == "no_body":
        client.resolve_languages.assert_not_awaited()


def photo():
    return PhotoSize(file_id="synthetic-photo", file_unique_id="synthetic-unique", width=100, height=100, file_size=100)


async def test_image_only_explicit_arguments_keep_image_path_and_reply_target(translation, monkeypatch):
    bot, client = translation
    stream = io.BytesIO(b"synthetic image")
    monkeypatch.setattr(lingvanex, "download", AsyncMock(return_value=stream))
    source = make_message(bot, photo=[photo()], message_id=10)
    message = make_message(bot, text="/tr en ru", message_id=20, reply_to_message=source)
    await invoke_command(lingvanex.process_translate, message, jev=client)
    client.resolve_languages.assert_not_awaited()
    lingvanex.translate.assert_not_awaited()
    lingvanex.translate_image.assert_awaited_once_with(stream, "en_GB", "ru_RU")
    assert stream.closed
    assert bot.session.methods[-1].reply_parameters.message_id == 10


async def test_oversized_image_does_not_discard_valid_text_translation(translation):
    bot, client = translation
    large = photo().model_copy(update={"file_size": MAX_DOWNLOAD_BYTES + 1})
    source = make_message(bot, photo=[large], message_id=10)
    message = make_message(bot, text="/tr en ru Hello", message_id=20, reply_to_message=source)
    await invoke_command(lingvanex.process_translate, message, jev=client)
    lingvanex.translate_image.assert_not_awaited()
    lingvanex.translate.assert_awaited_once_with("Hello", "en_GB", "ru_RU")
    assert len(bot.session.methods) == 1  # No getFile/download request.
    assert bot.session.methods[0].text == "Translation <&>\n\nНе удалось перевести изображение."


async def test_image_download_telegram_failure_preserves_text_result(translation, monkeypatch):
    bot, client = translation
    error = TelegramNetworkError(method=GetFile(file_id="synthetic-file"), message="synthetic private failure")
    monkeypatch.setattr(lingvanex, "download", AsyncMock(side_effect=error))
    source = make_message(bot, photo=[photo()], message_id=10)
    message = make_message(bot, text="/tr en ru Hello", message_id=20, reply_to_message=source)
    await invoke_command(lingvanex.process_translate, message, jev=client)
    lingvanex.translate.assert_awaited_once_with("Hello", "en_GB", "ru_RU")
    assert bot.session.methods[-1].text == "Translation <&>\n\nНе удалось перевести изображение."


@pytest.mark.parametrize("failed", ["image", "text", "both", "download"])
@pytest.mark.parametrize("failure", [ExternalServiceError, ClientError, TimeoutError])
async def test_migrated_translation_preserves_image_and_text_partial_success(translation, monkeypatch, failed, failure):
    bot, client = translation
    stream = io.BytesIO(b"synthetic image")
    downloader = AsyncMock(return_value=stream)
    if failed == "download":
        downloader.side_effect = failure("synthetic failure")
    monkeypatch.setattr(lingvanex, "download", downloader)
    if failed in {"image", "both"}:
        lingvanex.translate_image.side_effect = failure("synthetic failure")
    if failed in {"text", "both"}:
        lingvanex.translate.side_effect = failure("synthetic failure")
    source = make_message(bot, photo=[photo()], message_id=10)
    message = make_message(bot, text="/tr en ru Hello", message_id=20, reply_to_message=source)
    await invoke_command(lingvanex.process_translate, message, jev=client)
    sent = bot.session.methods[-1]
    assert sent.reply_parameters.message_id == 20 and sent.parse_mode is None
    if failed in {"image", "download"}:
        assert sent.text == "Translation <&>\n\nНе удалось перевести изображение."
    elif failed == "text":
        assert sent.text == "Image <&>\n\nНе удалось перевести текст."
    else:
        assert sent.text == "Не удалось выполнить перевод. Попробуйте ещё раз позже."
    if failed != "download":
        assert stream.closed
    else:
        stream.close()


async def test_image_language_is_never_guessed_from_pixels_or_history(translation):
    bot, client = translation
    client.resolve_languages.return_value = JevLanguages(source=None, target="ru_RU")
    source = make_message(bot, photo=[photo()], message_id=10)
    message = make_message(bot, text="/tr на русский", message_id=20, reply_to_message=source)
    await invoke_command(lingvanex.process_translate, message, jev=client)
    assert client.resolve_languages.call_args.kwargs["text"] == ""
    assert "synthetic-photo" not in str(client.resolve_languages.call_args)
    lingvanex.translate_image.assert_not_awaited()
    assert bot.session.methods[-1].text == lingvanex.TRANSLATE_GUIDANCE


async def test_cancellation_closes_image_and_never_becomes_guidance(translation, monkeypatch):
    bot, client = translation
    stream = io.BytesIO(b"synthetic image")
    monkeypatch.setattr(lingvanex, "download", AsyncMock(return_value=stream))
    lingvanex.translate_image.side_effect = asyncio.CancelledError
    source = make_message(bot, photo=[photo()], message_id=10)
    message = make_message(bot, text="/tr en ru", message_id=20, reply_to_message=source)
    with pytest.raises(asyncio.CancelledError):
        await invoke_command(lingvanex.process_translate, message, jev=client)
    assert stream.closed and not bot.session.methods


async def test_language_resolution_cancellation_never_translates_or_sends_guidance(translation):
    bot, client = translation
    client.resolve_languages.side_effect = asyncio.CancelledError
    source = make_message(bot, text="Hello", message_id=10)
    message = make_message(bot, text="/tr на русский", message_id=20, reply_to_message=source)
    with pytest.raises(asyncio.CancelledError):
        await invoke_command(lingvanex.process_translate, message, jev=client)
    lingvanex.translate.assert_not_awaited()
    assert not bot.session.methods
