from dataclasses import replace

from aiogram.types import Message
from aiogram.utils.markdown import hcode
from gtts import gTTS, gTTSError
from gtts.lang import tts_langs

from msu_hub_bot.commands.language import LanguageDetector
from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.telegram.files import input_file
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.utils import valid_filename, FakeBytesIO


VOICES = frozenset(tts_langs())
# gTTS keeps older or regional codes for a few ISO 639-1 languages.
VOICE_ALIASES = {"he": "iw", "zh": "zh-CN"}


def voice(code: str | None) -> str | None:
    if code is None:
        return None
    code = VOICE_ALIASES.get(code.lower(), code.lower())
    return next((name for name in VOICES if name.lower() == code), None)


def to_speech(text: str, lang: str) -> FakeBytesIO | None:
    mp3 = FakeBytesIO()
    try:
        r = gTTS(text, lang=lang, lang_check=False)
        r.write_to_fp(mp3)
    except gTTSError:
        return None
    mp3.name = f"tts_{valid_filename(text, 20).lower()}.mp3"
    mp3.seek(0)
    return mp3


async def tts(
    message: Message,
    meta: MetaInfo,
    cpu_executor: TPExecutor,
    lang: str | None = None,
    language_detector: LanguageDetector | None = None,
) -> Message | bool:
    target, text = meta.extract_text()
    if not text:
        return True
    if lang is None and language_detector is not None:
        # Languages gTTS cannot voice keep the Russian default.
        lang = voice(await language_detector.detect_text(text))
    lang = lang or "ru"

    mp3, timeouted = await cpu_executor.run(to_speech, text, lang)
    if timeouted:
        return await message.reply(hcode("🤷🏻‍♂️ Timeout"))
    if not mp3:
        return await message.reply(hcode("🤷🏻‍♂️ Не удалось выполнить запрос"))

    return await target.reply_audio(input_file(mp3, mp3.name), performer=f"TTS {lang.title()}", title=text[:20])


async def process_tts(
    message: Message, meta: MetaInfo, cpu_executor: TPExecutor, language_detector: LanguageDetector | None = None
) -> Message | bool:
    args = meta.arguments
    lang = voice(args[0]) if args else None
    if args and lang is None:
        # The first word was captured as an argument but is part of the text.
        meta = replace(meta, arguments=[], text=f"{args[0]} {meta.text}".strip())
    return await tts(message, meta, cpu_executor, lang, language_detector)
