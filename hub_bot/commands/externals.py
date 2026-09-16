from urllib import parse

from aiogram.types import ChatActions, Message, ContentType, InputFile, MediaGroup
from aiogram.utils.markdown import hitalic, quote_html, hbold, hlink, hcode
from yarl import URL

from app import cpu_executor
from common.constants import TELEGRAM_MESSAGE_MAX_LEN
from common.externals.exceptions import ExternalServiceError
from common.externals.fakeyou import fake_you, Voices
from common.externals.lingvanex import translate
from common.externals.moe import which_anime
from common.externals.other import remove_bg, porfirevich, imgur_upload, duckduckgo
from common.externals.topdf import convert_to_pdf
from common.externals.urbandictionary import urban_dictionary
from common.tg.chat_actioner import ChatActioner
from common.tg.filters import MetaInfo
from common.tg.utils import download, extract_image, action_by_type, send_super_reply
from common.utils import one_liner, prettify_bytes, cut_long_text, download_content, FakeBytesIO
from utils.ffmpeg import to_ogg_opus


async def process_external(message: Message, function, output_type: str = ContentType.PHOTO, handler=lambda x: x, async_handler=None):
    target, dest = await extract_image(message, with_profile_photo=True)
    file = await download(dest)
    if file is None:
        return True

    try:
        async with ChatActioner(message.chat, action_by_type(output_type)):
            result = await function(file)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    result = handler(result)
    if async_handler:
        result = await async_handler(result)

    if output_type == ContentType.TEXT:
        return await target.reply(result)

    if output_type == ContentType.DOCUMENT:
        return await target.reply_document(result)

    if output_type == ContentType.VIDEO:
        return await target.reply_video(result)

    if output_type == ContentType.ANIMATION:
        return await target.reply_animation(result)

    if output_type == 'list[photo]':
        media = MediaGroup()
        for b_io in result:
            media.attach_photo(b_io)
        return await target.reply_media_group(media)

    return await target.reply_photo(result)


async def process_which_anime(message: Message):
    target, dest = await extract_image(message, with_profile_photo=True)
    file = await download(dest)
    if file is None:
        return True

    async with ChatActioner(message.chat, action_by_type(ContentType.TEXT)):
        try:
            result = await which_anime(file)
        except ExternalServiceError as e:
            return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

        caption = ''
        files = []
        for anime in result['result'][:3]:
            link = hlink('Anilist', f'https://anilist.co/anime/{anime["anilist"]}')
            caption += f'— {hcode(anime["filename"])}, {link}, похожесть: {float(anime["similarity"]):.2}\n\n'
            files.append(anime['video'])

        media = MediaGroup()
        for file in files:
            media.attach_video(InputFile.from_url(file, filename=URL(file).name), caption=caption)
            caption = ''

        return await target.reply_media_group(media)


async def process_bg(message: Message):
    return await process_external(message, remove_bg, output_type=ContentType.DOCUMENT)


async def process_duckduckgo(message: Message, meta: MetaInfo):
    target, query = meta.extract_text()

    query = one_liner(cut_long_text(query, hard_max_len=100)[0]).strip().replace('\u200b', '')

    if not query:
        return True

    def lines(t: str) -> str:
        if t:
            return '\n' + t + '\n'
        return ''

    async with ChatActioner(message.chat, action_by_type(ContentType.TEXT)):
        r = await duckduckgo(query)

        if r['Redirect']:
            return await target.reply(hlink(r['Redirect'], r['Redirect']), disable_web_page_preview=True)

        text = f'''{hbold(r['Heading'])}\n{lines(r['AbstractText'])}\n{parse.unquote(r['AbstractURL'])}'''.strip()

        if not text or text == '<b></b>':
            return await message.reply('🤷🏻‍♂️ Ничего не найдено\n\nИскать на ' + hlink('DuckDuckGo', f'https://duckduckgo.com/?q={query}'),
                                       disable_web_page_preview=False)

        preview = r['AbstractURL']
        if not preview and r['Image']:
            preview = 'https://api.duckduckgo.com/' + r['Image']

        return await send_super_reply(target, text=text, web_preview=preview)


async def process_imgur(message: Message):
    def handler(r: dict) -> str:
        if e := r.get('error'):
            return f'🤷🏻‍♂️ {e}'
        return r['link'] + ' | ' + str(r['width']) + 'x' + str(r['height']) + ' | ' + prettify_bytes(r['size'])

    return await process_external(message, imgur_upload, output_type=ContentType.TEXT, handler=handler)


async def process_ud(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if text is None:
        return True

    try:
        async with ChatActioner(message.chat, action_by_type(ContentType.TEXT)):
            result = await urban_dictionary(text)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    texts = []
    prev_header, total_len = None, 0
    for r in result[:3]:
        text = ''
        text += f"{hbold(r['header'])}\n\n" if r['header'].casefold() != prev_header else ''
        text += f"{quote_html(r['meaning'])}\n\n"
        text += f"Example:\n{hitalic(r['example'])}\n\n"
        text += f"👍🏻 {hbold(r['up'])} 👎🏻 {hbold(r['down'])}\n"
        text += f"{hbold('———')}\n"

        prev_header = r['header'].casefold()
        total_len += len(text)
        if total_len > TELEGRAM_MESSAGE_MAX_LEN:
            break
        texts.append(text)

    result = f"\n".join(texts)
    return await target.reply(result)


async def process_fake_voice(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if not text:
        return True

    try:
        async with ChatActioner(message.chat, action_by_type(ContentType.AUDIO)):
            text = await translate(text, 'ru', 'en')
            wav_url = await fake_you(text, voice=Voices[meta.keyword.lower()].value)
            file = FakeBytesIO(await download_content(wav_url))
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    voice, timeouted = await cpu_executor.run(to_ogg_opus, file)
    if timeouted:
        return await message.reply(hcode('🤷🏻‍♂️ Timeout'))
    if not voice:
        return await message.reply(hcode('🤷🏻‍♂️ Не удалось выполнить запрос'))

    return await target.reply_voice(voice)


async def process_topdf(message: Message, meta: MetaInfo):
    target, dest = await meta.extract_doc()
    if dest is None:
        return True

    try:
        async with ChatActioner(message.chat, action_by_type(ContentType.DOCUMENT)):
            file = await download(dest)
            url, thumb, convert_name = await convert_to_pdf(file, dest.file_name, dest.mime_type)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    return await target.reply_document(InputFile.from_url(url, convert_name), thumb=InputFile.from_url(thumb))


async def process_porfirevich(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if not text:
        return True

    try:
        async with ChatActioner(message.chat, ChatActions.TYPING):
            result = await porfirevich(text)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    skip_length = max(0, len(text) + len(result) - TELEGRAM_MESSAGE_MAX_LEN)
    result = hbold(quote_html(text[skip_length:])) + quote_html(result)
    return await target.reply(result)
