"""Translation and speech pick source languages without hard-coded guesses; no live providers."""

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from msu_hub_bot.commands import lingvanex, tts
from msu_hub_bot.commands.language import LanguageDetector
from msu_hub_bot.providers import jev
from msu_hub_bot.providers import lingvanex as provider
from msu_hub_bot.providers.jev import JevError, JevErrorReason
from msu_hub_bot.providers.lingvanex import Translation
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telemetry import Telemetry
from telegram_helpers import make_bot, make_message
from test_jev import Response, transport  # noqa: F401


def text_meta(text, *, arguments=(), body=None):
    message = make_message(make_bot(), text=text)
    return message, MetaInfo(message, arguments=list(arguments), text=body if body is not None else "")


def executor():
    async def run(function, *args, **kwargs):
        return function(*args), False

    return SimpleNamespace(run=AsyncMock(side_effect=run))


@pytest.fixture
def replies(monkeypatch):
    reply = AsyncMock()
    monkeypatch.setattr(type(make_message()), "reply", reply)
    monkeypatch.setattr(lingvanex, "is_language", AsyncMock(side_effect=lambda code: code in {"ru", "en", "de", "en_GB"}))
    return reply


async def test_ru_translates_any_detected_language(monkeypatch, replies):
    translate = AsyncMock(return_value=Translation("Доброе утро", "de"))
    monkeypatch.setattr(lingvanex, "translate", translate)
    message, meta = text_meta("/ru Guten Morgen", body="Guten Morgen")
    await lingvanex.process_ru(message, meta, executor())
    translate.assert_awaited_once_with("Guten Morgen", None, "ru")
    replies.assert_awaited_once_with("Доброе утро")


async def test_text_already_in_the_target_language_is_not_echoed(monkeypatch, replies):
    monkeypatch.setattr(lingvanex, "translate", AsyncMock(return_value=Translation("Привет", "ru")))
    message, meta = text_meta("/ru Привет", body="Привет")
    await lingvanex.process_ru(message, meta, executor())
    replies.assert_awaited_once_with("Текст уже на русском.")


@pytest.mark.parametrize(
    "source,calls,reply",
    [
        ("de", [("Hallo", None, "ru")], "Привет"),
        ("ru", [("Hallo", None, "ru"), ("Hallo", "ru", "en_GB")], "Hello"),
    ],
)
async def test_bare_tr_goes_to_russian_or_from_russian_to_english(monkeypatch, replies, source, calls, reply):
    translate = AsyncMock(side_effect=[Translation("Привет", source), Translation("Hello", "ru")])
    monkeypatch.setattr(lingvanex, "translate", translate)
    message, meta = text_meta("/tr Hallo", arguments=["Hallo"])
    await lingvanex.process_translate(message, meta, executor())
    assert [call.args for call in translate.await_args_list] == calls
    replies.assert_awaited_once_with(reply)


@pytest.mark.parametrize(
    "arguments,body,expected",
    [
        (["de", "hello"], "world", ("hello world", None, "de")),
        (["en", "ru"], "hello", ("hello", "en", "ru")),
        (["hello", "de"], "world", ("hello de world", None, "ru")),
    ],
)
async def test_tr_keeps_leading_codes_and_returns_other_words_to_the_text(monkeypatch, replies, arguments, body, expected):
    translate = AsyncMock(return_value=Translation("x", "fr"))
    monkeypatch.setattr(lingvanex, "translate", translate)
    message, meta = text_meta("/tr " + " ".join([*arguments, body]), arguments=arguments, body=body)
    await lingvanex.process_translate(message, meta, executor())
    assert translate.await_args_list[0].args == expected


async def test_bare_tr_without_input_explains_usage(replies):
    message, meta = text_meta("/tr")
    await lingvanex.process_translate(message, meta, executor())
    assert "/tr de" in replies.await_args.args[0]


def image_meta(message):
    return SimpleNamespace(
        extract_image_with_downloading=AsyncMock(return_value=(message, io.BytesIO(b"jpeg"))),
        extract_text=lambda: (message, ""),
    )


@pytest.mark.parametrize(
    "handler,detected,source",
    [("process_ru", "de", "de"), ("process_ru", None, "en_GB"), ("process_en", None, "ru"), ("process_en", "fr", "fr")],
)
async def test_image_source_comes_from_jev_or_the_previous_fixed_guess(monkeypatch, replies, handler, detected, source):
    image = AsyncMock(return_value="перевод")
    monkeypatch.setattr(lingvanex, "translate_image", image)
    detector = SimpleNamespace(detect_image=AsyncMock(return_value=detected))
    message = make_message(make_bot())
    await getattr(lingvanex, handler)(message, image_meta(message), executor(), detector)
    assert image.await_args.args[1] == source
    detector.detect_image.assert_awaited_once()


async def test_explicit_image_source_skips_detection(monkeypatch, replies):
    image = AsyncMock(return_value="перевод")
    monkeypatch.setattr(lingvanex, "translate_image", image)
    detector = SimpleNamespace(detect_image=AsyncMock())
    message = make_message(make_bot())
    meta = image_meta(message)
    meta.arguments = ["en", "ru"]
    await lingvanex.process_translate(message, meta, executor(), detector)
    assert image.await_args.args[1:] == ("en", "ru")
    detector.detect_image.assert_not_awaited()


def tts_executor():
    audio = io.BytesIO(b"mp3")
    audio.name = "tts.mp3"
    return SimpleNamespace(run=AsyncMock(return_value=(audio, False)))


@pytest.mark.parametrize(
    "arguments,body,detected,voice,text",
    [
        (["en"], "Hello", "de", "en", "Hello"),
        (["Hello"], "world", "en", "en", "Hello world"),
        (["Hallo"], "Welt", "de", "de", "Hallo Welt"),
        (["Сәлем"], "", "kk", "ru", "Сәлем"),
        (["Shalom"], "", "he", "iw", "Shalom"),
        (["Привет"], "", None, "ru", "Привет"),
    ],
)
async def test_tts_voice_is_explicit_detected_or_russian(arguments, body, detected, voice, text):
    bot = make_bot()
    message = make_message(bot, text="/tts " + " ".join([*arguments, body]))
    detector = SimpleNamespace(detect_text=AsyncMock(return_value=detected))
    run = tts_executor()
    await tts.process_tts(message, MetaInfo(message, arguments=arguments, text=body), run, detector)
    assert run.run.await_args.args[1:] == (text, voice)
    if arguments[0] == "en":
        detector.detect_text.assert_not_awaited()


def detector(probabilities=None, error=None):
    client = SimpleNamespace(language=AsyncMock(return_value=probabilities, side_effect=error))
    return LanguageDetector(client, telemetry=Telemetry()), client


@pytest.mark.parametrize(
    "probabilities,expected",
    [({"de": 0.9, "en": 0.1}, "de"), ({"de": 0.5, "en": 0.5}, None), ({"other": 0.9, "en": 0.1}, None)],
)
async def test_detector_needs_a_confident_listed_language(probabilities, expected):
    service, client = detector(probabilities)
    assert await service.detect_text("Guten Morgen " * 200) == expected
    assert len(client.language.await_args.kwargs["text"]) == jev.MAX_REQUEST_LENGTH


@pytest.mark.parametrize("error", [JevError(JevErrorReason.TIMEOUT), JevError(JevErrorReason.UNAVAILABLE), TimeoutError()])
async def test_detector_failures_keep_the_callers_default(error):
    service, _ = detector(error=error)
    assert await service.detect_text("Hallo") is None


async def test_image_detection_sends_one_jpeg_still():
    service, client = detector({"ja": 0.95, "zh": 0.05})
    data = io.BytesIO()
    Image.new("RGB", (800, 400), "white").save(data, format="PNG")
    assert await service.detect_image(data.getvalue(), executor()) == "ja"
    image = client.language.await_args.kwargs["image"]
    assert image.startswith(b"\xff\xd8\xff") and max(Image.open(io.BytesIO(image)).size) == 512


def language_payload(code):
    options = [*jev.LANGUAGES, "other"]
    return {
        "model": "typesafe/jev-1.13",
        "answers": {
            "language": {
                "type": "choice",
                "choice": code,
                "confidence": 1,
                "probabilities": {name: float(name == code) for name in options},
            }
        },
    }


async def test_jev_language_text_request_has_no_image_opt_in(transport):  # noqa: F811
    session, _ = transport
    session.responses = [Response(language_payload("de"))]
    client = jev.JevClient("synthetic-key")
    assert (await client.language(text="Guten Morgen"))["de"] == 1
    request = session.requests[0][1]
    assert request["json"]["state"] == {"text": "Guten Morgen"} and "assets" not in request["json"]
    assert "TypeSafe-Danger-Zone-Images" not in request["headers"]
    await client.close()


async def test_jev_language_image_request_sends_the_still_with_opt_in(transport):  # noqa: F811
    session, _ = transport
    session.responses = [Response(language_payload("ja"))]
    client = jev.JevClient("synthetic-key")
    await client.language(image=b"\xff\xd8\xffjpeg")
    request = session.requests[0][1]
    assert set(request["json"]["assets"]) == {"<image>"} and request["headers"]["TypeSafe-Danger-Zone-Images"] == "true"
    await client.close()


@pytest.mark.parametrize("arguments", [{}, {"text": "a", "image": b"\xff\xd8\xff"}, {"text": " "}, {"image": b"png"}])
async def test_jev_language_needs_exactly_one_valid_input(transport, arguments):  # noqa: F811
    session, _ = transport
    with pytest.raises(JevError):
        await jev.JevClient("synthetic-key").language(**arguments)
    assert not session.requests


@pytest.mark.parametrize(
    "first,second,expected", [("en_GB", "en", True), ("ru", "ru_RU", True), ("zh-CN", "zh", True), ("de", "en", False), (None, "ru", False)]
)
def test_language_codes_compare_without_regions(first, second, expected):
    assert provider.same_language(first, second) is expected


class LingvanexSession:
    def __init__(self, body):
        self.body, self.sent = body, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def post(self, url, **kwargs):
        self.sent.append(kwargs["json"])
        return _Response(self.body)


class _Response:
    status = 200

    def __init__(self, body):
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def json(self):
        return self.body


@pytest.mark.parametrize("src,sent_from", [(None, None), ("de", "de_DE")])
async def test_translation_without_a_source_lets_lingvanex_detect_it(monkeypatch, src, sent_from):
    session = LingvanexSession({"err": None, "result": "Доброе утро", "from": "de"})
    monkeypatch.setattr(provider.aiohttp, "ClientSession", lambda: session)
    monkeypatch.setattr(provider, "full_code", AsyncMock(side_effect=lambda code: {"de": "de_DE", "ru": "ru_RU"}[code]))
    assert await provider.translate("Guten Morgen", src, "ru") == Translation("Доброе утро", "de")
    assert session.sent[0].get("from") == sent_from and session.sent[0]["to"] == "ru_RU"
