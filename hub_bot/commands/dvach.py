from msu_hub_bot.settings import settings

from contextlib import suppress
from enum import IntEnum, auto
from typing import Iterable
from typing import Tuple

from aiogram.exceptions import TelegramBadRequest
from aiogram.enums import ChatType
from common.externals import dvach as api2ch
from common.caching import cached_async
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.markdown import hbold, hcode, hitalic, hlink

from common.tg.wrapper import BotWrapper
from common.tg.callbacks import CallbackCommandBase
from common.utils import PriorityQueue, megabytes, one_liner


class DvachCallback(CallbackData, prefix="thread", sep="_"):
    board: str
    url: str


class Dvach(CallbackCommandBase):
    callback_data = DvachCallback

    @staticmethod
    def restriction_filter(event: Message | CallbackQuery) -> bool:
        message = event if isinstance(event, Message) else event.message
        return message is not None and (message.chat.type == ChatType.PRIVATE or message.chat.id in settings.dvach_chat_ids)

    @classmethod
    def keyboard(cls, board: str, urls: Iterable[str]) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardBuilder().row(
            InlineKeyboardButton(text='Обновить 🔄', callback_data=DvachCallback(board=board, url='update').pack())
        ).row()
        numbered = InlineKeyboardBuilder()
        for i, url in enumerate(urls, start=1):
            button = InlineKeyboardButton(text=f'{i}', callback_data=DvachCallback(board=board, url=url).pack())
            numbered.add(button)
        numbered.adjust(7)
        keyboard.attach(numbered)
        return InlineKeyboardMarkup(inline_keyboard=keyboard.export())

    @classmethod
    @cached_async(ttl=3 * 60, noself=True)
    async def board_top(cls, board: str, dvach: api2ch.Api2chAsync, count: int = 21) -> Tuple[str, InlineKeyboardMarkup]:
        response = await dvach.threads(board)
        threads = response.sorted_by_views()[:count]

        text = f'📝 Топ тредов доски {hcode(board)}:\n\n'
        for i, t in enumerate(threads, start=1):
            text += f'{i:>2}. {(hlink(t.header, t.url(board)))} | {hbold(t.posts_count)} 💬\n\n'

        reply_markup = cls.keyboard(board, [t.url(board) for t in threads])
        return text, reply_markup

    @classmethod
    async def process(cls, message: Message, dvach: api2ch.Api2chAsync) -> Message:
        args = one_liner(message.text or message.caption or "").split()[1:]
        board = args[0] if len(args) > 0 else 'b'

        if board not in api2ch.BOARDS:
            return await message.reply(f'🤷🏻‍♂️ Доска {hcode(board)} недоступна')

        text, reply_markup = await cls.board_top(board, dvach)
        return await message.reply(text, reply_markup=reply_markup, disable_web_page_preview=True)

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: DvachCallback, dvach: api2ch.Api2chAsync, bot: BotWrapper) -> Message | bool | None:
        m = query.message
        if not isinstance(m, Message):
            return await query.answer("Эта кнопка уже недоступна.")
        board, url = callback_data.board, callback_data.url

        if url == 'update':
            if board in api2ch.BOARDS:
                await query.answer(text='✅', cache_time=3 * 60)
                text, reply_markup = await cls.board_top(board, dvach)
                with suppress(TelegramBadRequest):
                    return await m.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)

        valid, board, thread_id = api2ch.parse_url(url)
        if not valid:
            return await query.answer()

        try:
            thread = await dvach.thread(board, thread_id)
        except api2ch.Api2chError:
            return await query.answer(text='🤷🏻‍♂️ Тред недоступен', cache_time=cls.cache_time_10m)

        await query.answer(text='✅', cache_time=cls.cache_time_10m)

        text, web_preview, photos_urls, video_urls = for_publish(thread.posts[0], thread)

        full_text = hbold('🗞 Тред ') + hlink(f'{board}/{thread_id}', url) + '\n' + text
        if m.chat.type != ChatType.PRIVATE:
            full_text += f'\n\n— Запрос участника {query.from_user.mention_html()}'

        return await bot.send_super_message(full_text, web_preview, photos_urls, video_urls, m.chat.id, m.message_id, message_thread_id=m.message_thread_id)

    @classmethod
    def link_filter(cls, message: Message) -> bool:
        url = message.text or message.caption or ""
        return bool(url and api2ch.parse_url(url)[0])

    @classmethod
    async def process_link(cls, message: Message, dvach: api2ch.Api2chAsync, bot: BotWrapper) -> Message | bool | None:
        url = message.text or message.caption or ""

        valid, board, thread_id = api2ch.parse_url(url)
        if not valid:
            return True

        try:
            thread = await dvach.thread(board, thread_id)
        except api2ch.Api2chError:
            return True

        post = thread.posts[0]
        if '#' in url:
            post_id = url.split('#')[-1]
            if post_id.isdigit():
                parsed_post_id = int(post_id)
                for p in thread.posts:
                    if p.post_id == parsed_post_id:
                        post = p
                        break

        text, web_preview, photos_urls, video_urls = for_publish(post, thread)
        full_text = hbold('🗞 Тред ') + hlink(f'{board}/{thread_id}', url) + '\n' + text
        return await bot.send_super_message(full_text, web_preview, photos_urls, video_urls, message.chat.id, message.message_id, message_thread_id=message.message_thread_id)


def for_publish(post: api2ch.Post, thread: api2ch.ResponseThread) -> Tuple[str, str, list[str], list[str]]:
    class FileType(IntEnum):
        high_priority = auto()
        mp4 = auto()
        images = auto()
        webm = auto()

    def name_to_type(name: str) -> FileType:
        name = name.lower()
        if name.endswith('.mp4'):
            return FileType.mp4
        if name.endswith('.webm'):
            return FileType.webm
        return FileType.images

    time = post.dt().strftime('%d/%m/%y %H:%M')
    text = f'{hitalic(time)} | Пост №{hlink(str(post.post_id), post.url(thread.board))}:\n\n'
    text += f'{hbold(post.header)}\n' if thread.enable_subject else ''
    text += f'{post.body}\n\n'

    web_preview = ''
    photos_urls: list[str] = []
    video_urls: list[str] = []
    if post.files:
        pq = PriorityQueue[str]()

        links = []
        for f in post.files:
            url = f.url()

            ft = name_to_type(f.path)
            if ft == FileType.images:
                if f.size_bytes < megabytes(5):
                    photos_urls.append(url)
                pq.put(ft, url)
            elif ft == FileType.mp4:
                if f.size_bytes < megabytes(20):
                    video_urls.append(url)
                pq.put(ft, url)
            else:
                links.append(f'{hlink(f.original_name, url)}, {f.size_string}')

        if len(photos_urls) + len(video_urls) == 1:
            url = photos_urls[0] if photos_urls else video_urls[0]
            pq.put(FileType.high_priority, url)
            web_preview = pq.head() or ''
            photos_urls, video_urls = [], []

        elif len(photos_urls) == 1 and len(video_urls) == 1:
            pq.put(FileType.high_priority, video_urls[0])
            web_preview = pq.head() or ''
            video_urls = []

        if len(links):
            text += '— Файл' + ('ы' if len(links) > 1 else '') + ':\n' + '\n'.join(links)

    return text, web_preview, photos_urls, video_urls
