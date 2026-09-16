from aiogram.types import Message
from aiogram.utils.markdown import hcode, quote_html, hbold
from aiohttp import ClientError

from common.externals.exceptions import ExternalServiceError
from common.externals.lingvanex import languages_list, translate, translate_image
from common.tg.filters import MetaInfo


async def tr(meta: MetaInfo, src: str, dest: str):
    translated = ''
    failed = []

    target, file = await meta.extract_image_with_downloading()
    if file is not None:
        try:
            result = await translate_image(file, src, dest)
            if result and result.strip():
                translated += result + '\n\n'
            else:
                failed.append('изображение')
        except (ExternalServiceError, ClientError, TimeoutError):
            failed.append('изображение')

    target, text = meta.extract_text()
    if text:
        try:
            result = await translate(text, src, dest)
            if result and result.strip():
                translated += result
            else:
                failed.append('текст')
        except (ExternalServiceError, ClientError, TimeoutError):
            failed.append('текст')

    if translated:
        if failed:
            translated = translated.rstrip() + '\n\nНе удалось перевести ' + ' и '.join(failed) + '.'
        return await target.reply(quote_html(translated))
    if failed:
        return await target.reply('Не удалось выполнить перевод. Попробуйте ещё раз позже.')


async def process_langs(message: Message):
    langs = await languages_list()
    text = hbold('Поддерживаемые языки') + '\n\n'
    for lang in langs:
        code = lang['code_alpha_1']
        full_code = lang['full_code']
        name = lang['englishName']
        text += f'• {name} — {code}, {full_code}\n'

    text += '\nИспользование: ' + hcode('/tr en ru') + ' — перевод с английского на русский'
    return await message.reply(text)


async def process_en(_message: Message, meta: MetaInfo):
    return await tr(meta, 'ru', 'en_GB')


async def process_ru(_message: Message, meta: MetaInfo):
    return await tr(meta, 'en_GB', 'ru')


async def process_translate(message: Message, meta: MetaInfo):
    args = meta.arguments
    if len(args) != 2:
        return await message.reply('Использование: ' + hcode('/tr en ru') + ' — перевод с английского на русский, '
                                                                            'полный список языков: /langs')
    src, dest = args[0], args[1]
    return await tr(meta, src, dest)
