from urllib import parse

from aiogram.types import ChatActions, Message, ContentType, InputFile, MediaGroup
from aiogram.utils.markdown import hitalic, quote_html, hbold, hlink, hcode
from yarl import URL

from common.constants import TELEGRAM_MESSAGE_MAX_LEN
from common.externals.exceptions import ExternalServiceError
from common.externals.moe import which_anime
from common.externals.other import remove_bg, porfirevich, imgur_upload, duckduckgo
from common.externals.topdf import convert_to_pdf
from common.externals.urbandictionary import urban_dictionary
from common.tg.chat_actioner import ChatActioner
from common.tg.filters import MetaInfo
from common.tg.utils import download, extract_image, action_by_type, send_super_reply
from common.utils import one_liner, prettify_bytes, cut_long_text


def _escaped_excerpt(text: str, limit: int, *, tail: bool = False) -> str:
    parts, size = [], 0
    for character in reversed(text) if tail else text:
        escaped = quote_html(character)
        width = len(escaped.encode('utf-16-le')) // 2
        if size + width > max(0, limit - 1):
            break
        parts.append(escaped)
        size += width
    clipped = len(parts) < len(text)
    if tail:
        return ('…' if clipped else '') + ''.join(reversed(parts))
    return ''.join(parts) + ('…' if clipped else '')


async def process_external(message: Message, function, output_type: str = ContentType.PHOTO, handler=lambda x: x, async_handler=None, error_text=None):
    target, dest = await extract_image(message, with_profile_photo=True)
    file = await download(dest)
    if file is None:
        return True

    try:
        async with ChatActioner(message.chat, action_by_type(output_type)):
            result = await function(file)
    except ExternalServiceError as e:
        return await message.reply(error_text or hitalic(f'🤷🏻‍♂️ {e.text}'))

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
            filename = anime['filename']
            if len(filename) > 100:
                filename = filename[:99] + '…'
            caption += f'— {hcode(filename)}, {link}, похожесть: {float(anime["similarity"]):.2}\n\n'
            files.append(anime['video'])

        if not files:
            return await target.reply('Не удалось найти аниме по этому кадру. Попробуйте другой.')

        if len(files) == 1:
            file = files[0]
            return await target.reply_video(InputFile.from_url(file, filename=URL(file).name), caption=caption)

        media = MediaGroup()
        for file in files:
            media.attach_video(InputFile.from_url(file, filename=URL(file).name), caption=caption)
            caption = ''

        return await target.reply_media_group(media)


async def process_bg(message: Message):
    return await process_external(message, remove_bg, output_type=ContentType.DOCUMENT,
                                  error_text='Не удалось убрать фон. Попробуйте другое фото или повторите позже.')


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
        search_url = 'https://duckduckgo.com/?' + parse.urlencode({'q': query})
        try:
            r = await duckduckgo(query)
        except ExternalServiceError:
            return await target.reply('Поиск сейчас недоступен. Попробуйте ' + hlink('DuckDuckGo', search_url),
                                      disable_web_page_preview=True)

        if r['Redirect']:
            return await target.reply(hlink(r['Redirect'], r['Redirect']), disable_web_page_preview=True)

        heading = '<b>' + _escaped_excerpt(r['Heading'], 500) + '</b>'
        abstract = _escaped_excerpt(r['AbstractText'], 2500)
        source = _escaped_excerpt(parse.unquote(r['AbstractURL']), 500)
        text = f'{heading}\n{lines(abstract)}\n{source}'.strip()

        if not text or text == '<b></b>':
            return await message.reply('🤷🏻‍♂️ Ничего не найдено\n\nИскать на ' + hlink('DuckDuckGo', search_url),
                                       disable_web_page_preview=False)

        preview = r['AbstractURL']
        if not preview and r['Image']:
            preview = 'https://api.duckduckgo.com/' + r['Image']

        return await send_super_reply(target, text=text, web_preview=preview)


async def process_imgur(message: Message):
    def handler(r: dict) -> str:
        return quote_html(r['link']) + ' | ' + str(r['width']) + 'x' + str(r['height']) + ' | ' + prettify_bytes(r['size'])

    return await process_external(message, imgur_upload, output_type=ContentType.TEXT, handler=handler,
                                  error_text='Не удалось загрузить файл на Imgur. Попробуйте позже.')


async def process_ud(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if text is None:
        return True

    try:
        async with ChatActioner(message.chat, action_by_type(ContentType.TEXT)):
            result = await urban_dictionary(text)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    if not result:
        return await target.reply('В Urban Dictionary ничего не нашлось. Попробуйте другое слово.')

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
        text_length = len(text.encode('utf-16-le')) // 2
        if total_len + text_length + bool(texts) > TELEGRAM_MESSAGE_MAX_LEN:
            if not texts:
                header = hbold(r['header'][:100]) + '\n\n'
                url = 'https://www.urbandictionary.com/define.php?' + parse.urlencode({'term': r['header'][:100]})
                footer = '\n\n' + hlink('Полное определение', url)
                # Escaping expands one character to at most five; keep the excerpt and HTML intact.
                overhead = len((header + footer).encode('utf-16-le')) // 2
                limit = max(1, (TELEGRAM_MESSAGE_MAX_LEN - overhead - 1) // 5)
                texts.append(header + quote_html(r['meaning'][:limit]) + '…' + footer)
            break
        texts.append(text)
        total_len += text_length + (len(texts) > 1)

    result = f"\n".join(texts)
    return await target.reply(result)


async def process_topdf(message: Message, meta: MetaInfo):
    target, dest = await meta.extract_doc()
    if dest is None:
        return True

    try:
        async with ChatActioner(message.chat, action_by_type(ContentType.DOCUMENT)):
            file = await download(dest)
            url, thumb, convert_name = await convert_to_pdf(file, dest.file_name, dest.mime_type)
    except TimeoutError:
        return await message.reply('Конвертация заняла слишком много времени. Попробуйте ещё раз позже.')
    except ExternalServiceError:
        return await message.reply('Не удалось преобразовать файл в PDF. Попробуйте позже.')

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

    continuation = _escaped_excerpt(result, 4000)
    remaining = TELEGRAM_MESSAGE_MAX_LEN - len(continuation.encode('utf-16-le')) // 2 - len('<b></b>')
    result = '<b>' + _escaped_excerpt(text, remaining, tail=True) + '</b>' + continuation
    return await target.reply(result)
