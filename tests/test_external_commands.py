"""Provider replies are synthetic; no Telegram or provider requests are sent."""

import asyncio
import io
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientError

from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.commands import lingvanex


@pytest.fixture
def external_handlers():
    path = Path(__file__).resolve().parents[1] / "src/msu_hub_bot/commands/externals.py"
    # Each test owns its provider stubs without changing other imported handlers.
    namespace = {}
    exec(compile(path.read_text(), str(path), "exec"), namespace)

    @asynccontextmanager
    async def no_chat_action(*args):
        yield

    namespace["ChatActioner"] = no_chat_action

    async def reply_album(target, media):
        return await target.reply_media_group(media)

    namespace["reply_album"] = reply_album
    return namespace


@pytest.mark.parametrize("count", [0, 1, 2, 3, 5])
async def test_anime_results_use_valid_delivery(external_handlers, count):
    target = SimpleNamespace(reply=AsyncMock(), reply_video=AsyncMock(), reply_media_group=AsyncMock())
    message = SimpleNamespace(chat=object())
    external_handlers["extract_image"] = AsyncMock(return_value=(target, object()))
    external_handlers["download"] = AsyncMock(return_value=io.BytesIO(b"image"))
    external_handlers["which_anime"] = AsyncMock(
        return_value={
            "result": [
                {"filename": f"Episode <{i}>", "anilist": i, "similarity": 0.95, "video": f"https://example.org/{i}.mp4"}
                for i in range(count)
            ]
        }
    )

    await external_handlers["process_which_anime"](message)

    if count == 0:
        target.reply.assert_awaited_once()
        assert "Не удалось найти аниме" in target.reply.call_args.args[0]
        target.reply_video.assert_not_awaited()
        target.reply_media_group.assert_not_awaited()
    elif count == 1:
        target.reply.assert_not_awaited()
        target.reply_video.assert_awaited_once()
        target.reply_media_group.assert_not_awaited()
        assert "Episode &lt;0&gt;" in target.reply_video.call_args.kwargs["caption"]
    else:
        target.reply.assert_not_awaited()
        target.reply_video.assert_not_awaited()
        target.reply_media_group.assert_awaited_once()
        media = target.reply_media_group.call_args.args[0]
        assert len(media) == min(count, 3)
        assert "Episode &lt;0&gt;" in media[0].caption
        assert all(not item.caption for item in media[1:])


def translation_input(*, image=True, text="Original"):
    target = SimpleNamespace(reply=AsyncMock())
    meta = SimpleNamespace(
        arguments=["en", "ru"],
        extract_image_with_downloading=AsyncMock(return_value=(target, io.BytesIO(b"image") if image else None)),
        extract_text=lambda: (target, text),
    )
    return target, meta


@pytest.mark.parametrize("handler", ["process_en", "process_ru", "process_translate"])
@pytest.mark.parametrize("failure", [ExternalServiceError, ClientError, TimeoutError])
async def test_translation_failures_receive_one_reply(monkeypatch, handler, failure):
    target, meta = translation_input()
    monkeypatch.setattr(lingvanex, "translate_image", AsyncMock(side_effect=failure("synthetic provider failure")))
    monkeypatch.setattr(lingvanex, "translate", AsyncMock(side_effect=failure("synthetic provider failure")))

    await getattr(lingvanex, handler)(target, meta)

    target.reply.assert_awaited_once_with("Не удалось выполнить перевод. Попробуйте ещё раз позже.")
    lingvanex.translate_image.assert_awaited_once()
    lingvanex.translate.assert_awaited_once()


@pytest.mark.parametrize("failed", ["image", "text"])
async def test_translation_preserves_partial_result_and_names_failure(monkeypatch, failed):
    target, meta = translation_input()
    image = AsyncMock(return_value="Picture <text>")
    text = AsyncMock(return_value="Translated <text>")
    (image if failed == "image" else text).side_effect = ExternalServiceError("synthetic failure")
    monkeypatch.setattr(lingvanex, "translate_image", image)
    monkeypatch.setattr(lingvanex, "translate", text)

    await lingvanex.process_ru(target, meta)

    target.reply.assert_awaited_once()
    reply = target.reply.call_args.args[0]
    assert ("Translated &lt;text&gt;" if failed == "image" else "Picture &lt;text&gt;") in reply
    assert ("Не удалось перевести изображение." if failed == "image" else "Не удалось перевести текст.") in reply


async def test_translation_success_keeps_both_escaped_parts(monkeypatch):
    target, meta = translation_input()
    monkeypatch.setattr(lingvanex, "translate_image", AsyncMock(return_value="Picture <text>"))
    monkeypatch.setattr(lingvanex, "translate", AsyncMock(return_value="Translated & text"))

    await lingvanex.process_ru(target, meta)

    target.reply.assert_awaited_once_with("Picture &lt;text&gt;\n\nTranslated &amp; text")


async def test_translation_without_input_does_not_call_provider(monkeypatch):
    target, meta = translation_input(image=False, text="")
    monkeypatch.setattr(lingvanex, "translate_image", AsyncMock())
    monkeypatch.setattr(lingvanex, "translate", AsyncMock())

    await lingvanex.process_ru(target, meta)

    target.reply.assert_not_awaited()
    lingvanex.translate_image.assert_not_awaited()
    lingvanex.translate.assert_not_awaited()


@pytest.mark.parametrize("result", ["", " \n\t"])
async def test_empty_translation_result_does_not_send_blank_message(monkeypatch, result):
    target, meta = translation_input(text="")
    monkeypatch.setattr(lingvanex, "translate_image", AsyncMock(return_value=result))

    await lingvanex.process_ru(target, meta)

    target.reply.assert_awaited_once_with("Не удалось выполнить перевод. Попробуйте ещё раз позже.")


async def test_translation_cancellation_propagates(monkeypatch):
    target, meta = translation_input()
    monkeypatch.setattr(lingvanex, "translate_image", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await lingvanex.process_ru(target, meta)

    target.reply.assert_not_awaited()


async def test_pdf_timeout_has_actionable_command_reply(external_handlers):
    target = SimpleNamespace(reply=AsyncMock(), chat=object())
    document = SimpleNamespace(file_name="synthetic.txt", mime_type="text/plain")
    meta = SimpleNamespace(extract_doc=AsyncMock(return_value=(target, document)))
    external_handlers["download"] = AsyncMock(return_value=io.BytesIO(b"document"))
    external_handlers["convert_to_pdf"] = AsyncMock(side_effect=TimeoutError)
    await external_handlers["process_topdf"](target, meta)
    assert "Конвертация заняла слишком много времени" in target.reply.call_args.args[0]
    target.reply.assert_awaited_once()


async def test_urban_empty_result_has_clear_reply(external_handlers):
    target = SimpleNamespace(chat=object(), reply=AsyncMock())
    meta = SimpleNamespace(extract_text=lambda: (target, "synthetic"))
    external_handlers["urban_dictionary"] = AsyncMock(return_value=[])
    await external_handlers["process_ud"](target, meta)
    assert "ничего не нашлось" in target.reply.call_args.args[0]


@pytest.mark.parametrize("meaning", ["<&>" * 2500, "😀" * 3000])
async def test_urban_long_first_definition_keeps_valid_bounded_excerpt(external_handlers, meaning):
    import xml.etree.ElementTree as element_tree

    target = SimpleNamespace(chat=object(), reply=AsyncMock())
    meta = SimpleNamespace(extract_text=lambda: (target, "synthetic"))
    external_handlers["urban_dictionary"] = AsyncMock(
        return_value=[{"header": "Synthetic <&>", "meaning": meaning, "example": "<example>", "up": 5, "down": 2}]
    )
    await external_handlers["process_ud"](target, meta)
    text = target.reply.call_args.args[0]
    assert 0 < len(text.encode("utf-16-le")) // 2 <= 4096
    element_tree.fromstring("<root>" + text + "</root>")
    assert "Полное определение" in text
    assert "Synthetic &lt;&amp;&gt;" in text


async def test_search_provider_text_is_escaped(external_handlers):
    target = SimpleNamespace(chat=object(), reply=AsyncMock())
    meta = SimpleNamespace(extract_text=lambda: (target, "a & b"))
    external_handlers["duckduckgo"] = AsyncMock(
        return_value={
            "Redirect": "",
            "Heading": "<Heading>",
            "AbstractText": "<b>literal</b> & text",
            "AbstractURL": "https://example.org/%3Cvalue%3E",
            "Image": "",
        }
    )
    external_handlers["send_super_reply"] = AsyncMock()
    await external_handlers["process_duckduckgo"](target, meta)
    text = external_handlers["send_super_reply"].call_args.kwargs["text"]
    assert "&lt;Heading&gt;" in text
    assert "&lt;b&gt;literal&lt;/b&gt; &amp; text" in text
    assert "/&lt;value&gt;" in text


async def test_search_failure_offers_encoded_search_link(external_handlers):
    target = SimpleNamespace(chat=object(), reply=AsyncMock())
    meta = SimpleNamespace(extract_text=lambda: (target, "a & b"))
    external_handlers["duckduckgo"] = AsyncMock(side_effect=ExternalServiceError("private provider detail"))
    await external_handlers["process_duckduckgo"](target, meta)
    text = target.reply.call_args.args[0]
    assert "Поиск сейчас недоступен" in text
    assert "q=a+%26+b" in text
    assert "private provider detail" not in text


@pytest.mark.parametrize(
    "command,provider,expected",
    [
        ("process_imgur", "imgur_upload", "Не удалось загрузить файл на Imgur"),
    ],
)
async def test_upload_provider_failure_has_feature_message(external_handlers, command, provider, expected):
    target = SimpleNamespace(chat=object(), reply=AsyncMock())
    external_handlers["extract_image"] = AsyncMock(return_value=(target, object()))
    external_handlers["download"] = AsyncMock(return_value=io.BytesIO(b"image"))
    external_handlers[provider] = AsyncMock(side_effect=ExternalServiceError("private provider detail"))
    await external_handlers[command](target)
    text = target.reply.call_args.args[0]
    assert expected in text
    assert "private provider detail" not in text


@pytest.mark.parametrize("source", ["<text> & " * 2000, "😀" * 5000])
async def test_long_translation_preserves_all_text_in_safe_replies(monkeypatch, source):
    import html

    target, meta = translation_input(image=False)
    monkeypatch.setattr(lingvanex, "translate", AsyncMock(return_value=source))
    await lingvanex.process_ru(target, meta)
    texts = [call.args[0] for call in target.reply.call_args_list]
    assert len(texts) > 1
    assert all(0 < len(text.encode("utf-16-le")) // 2 <= 4096 for text in texts)
    assert "".join(html.unescape(text) for text in texts) == source


async def test_language_list_is_escaped_and_split(monkeypatch):
    target = SimpleNamespace(reply=AsyncMock())
    languages = [{"code_alpha_1": str(i), "full_code": f"en_{i}", "englishName": f"Language <{i}>"} for i in range(500)]
    monkeypatch.setattr(lingvanex, "languages_list", AsyncMock(return_value=languages))
    await lingvanex.process_langs(target)
    texts = [call.args[0] for call in target.reply.call_args_list]
    assert len(texts) > 1
    assert all(0 < len(text) <= 4096 for text in texts)
    assert all(f"Language &lt;{i}&gt;" in "".join(texts) for i in range(500))
    assert "Использование:" in texts[-1]


async def test_anime_caption_fits_with_long_unicode_filenames(external_handlers):
    import xml.etree.ElementTree as element_tree

    target = SimpleNamespace(reply_media_group=AsyncMock())
    message = SimpleNamespace(chat=object())
    external_handlers["extract_image"] = AsyncMock(return_value=(target, object()))
    external_handlers["download"] = AsyncMock(return_value=io.BytesIO(b"image"))
    external_handlers["which_anime"] = AsyncMock(
        return_value={
            "result": [
                {"filename": "😀<&>" * 500, "anilist": i, "similarity": 0.95, "video": f"https://example.org/{i}.mp4"} for i in range(3)
            ]
        }
    )
    await external_handlers["process_which_anime"](message)
    media = target.reply_media_group.call_args.args[0]
    caption = media[0].caption
    parsed = "".join(element_tree.fromstring("<root>" + caption + "</root>").itertext())
    assert len(parsed.encode("utf-16-le")) // 2 <= 1024
    assert "…" in parsed


async def test_gpt2_prompt_is_escaped_once(external_handlers):
    target = SimpleNamespace(chat=object(), reply=AsyncMock())
    meta = SimpleNamespace(extract_text=lambda: (target, "<Prompt> &"))
    external_handlers["porfirevich"] = AsyncMock(return_value=" <continuation>")
    await external_handlers["process_porfirevich"](target, meta)
    assert target.reply.call_args.args[0] == "<b>&lt;Prompt&gt; &amp;</b> &lt;continuation&gt;"


async def test_search_excerpt_survives_legacy_sender_splitting(external_handlers):
    import xml.etree.ElementTree as element_tree
    from msu_hub_bot.utils import cut_long_text

    target = SimpleNamespace(chat=object(), reply=AsyncMock())
    meta = SimpleNamespace(extract_text=lambda: (target, "synthetic"))
    external_handlers["duckduckgo"] = AsyncMock(
        return_value={
            "Redirect": "",
            "Heading": "Synthetic",
            "AbstractText": "&" * 1000,
            "AbstractURL": "https://example.org",
            "Image": "",
        }
    )
    external_handlers["send_super_reply"] = AsyncMock()
    await external_handlers["process_duckduckgo"](target, meta)
    text = external_handlers["send_super_reply"].call_args.kwargs["text"]
    chunks = cut_long_text(text)
    assert len(chunks) == 1
    element_tree.fromstring("<root>" + chunks[0] + "</root>")
    assert "…" in text


async def test_gpt2_long_unicode_prompt_keeps_valid_length_and_continuation(external_handlers):
    import xml.etree.ElementTree as element_tree

    target = SimpleNamespace(chat=object(), reply=AsyncMock())
    meta = SimpleNamespace(extract_text=lambda: (target, "😀" * 2000))
    external_handlers["porfirevich"] = AsyncMock(return_value="synthetic " * 60)
    await external_handlers["process_porfirevich"](target, meta)
    text = target.reply.call_args.args[0]
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    element_tree.fromstring("<root>" + text + "</root>")
    assert text.endswith("synthetic " * 60)
    assert "…" in text
