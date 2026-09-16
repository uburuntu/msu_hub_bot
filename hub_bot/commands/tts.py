from aiogram.types import Message
from aiogram.utils.markdown import hcode
from gtts import gTTS, gTTSError

from common.executor import TPExecutor
from common.tg.files import input_file
from common.tg.filters import MetaInfo
from common.utils import valid_filename, FakeBytesIO


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


async def tts(message: Message, meta: MetaInfo, cpu_executor: TPExecutor, lang: str = "ru") -> Message | bool:
    target, text = meta.extract_text()
    if not text:
        return True

    mp3, timeouted = await cpu_executor.run(to_speech, text, lang)
    if timeouted:
        return await message.reply(hcode("🤷🏻‍♂️ Timeout"))
    if not mp3:
        return await message.reply(hcode("🤷🏻‍♂️ Не удалось выполнить запрос"))

    return await target.reply_audio(input_file(mp3, mp3.name), performer=f"TTS {lang.title()}", title=text[:20])


async def process_tts(message: Message, meta: MetaInfo, cpu_executor: TPExecutor) -> Message | bool:
    args = meta.arguments
    lang = args[0] if args else "ru"
    return await tts(message, meta, cpu_executor, lang)
