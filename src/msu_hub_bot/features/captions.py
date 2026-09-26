"""Photo and video captions with declared inputs and application media admission."""

import io
from collections.abc import Callable
from contextlib import ExitStack

from PIL import Image
from aiogram.enums import ChatAction, ChatType
from aiogram.types import Animation, BufferedInputFile, Document, Message, Sticker, Video, VideoNote
from teleforge import Feature
from teleforge.context import MessageContext
from teleforge.inputs import MediaInput, TextInput

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.features.command import command as hub_command
from msu_hub_bot.media.caption_layout import CaptionLayoutError, CaptionStyle, caption_image
from msu_hub_bot.media.caption_video import caption_video
from msu_hub_bot.telegram.chat_actioner import ChatActioner
from msu_hub_bot.telegram.files import DownloadableMedia
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telegram.keyboards import rate_keyboard
from msu_hub_bot.telegram.media_jobs import DownloadUnavailable, run_downloaded
from msu_hub_bot.utils import image_bytes_io

_NAMES: dict[CaptionStyle, tuple[str, ...]] = {
    "lobster": ("lobster", "l", "л", "лобстер"),
    "demotivator": ("demotivator", "de", "д", "де"),
    "meme": ("meme",),
}
_OPTIONS = {
    "text": TextInput(),
    "media": MediaInput(kinds=("video", "image"), avatar=True),
    # The renderer bounds video at 50 MiB; the complete response also includes controls.
    "max_output_bytes": 64 * 1024 * 1024,
}


class Captions(Feature, key="captions"):
    # Keep router precedence when a message contains triggers for several styles.
    @hub_command(*_NAMES["lobster"], flags={"handler_key": "process_lobster", "fsm_release": True}, **_OPTIONS)
    @hub_command(*_NAMES["demotivator"], flags={"handler_key": "process_demotivator", "fsm_release": True}, **_OPTIONS)
    @hub_command(*_NAMES["meme"], flags={"handler_key": "process_meme", "fsm_release": True}, **_OPTIONS)
    async def caption(self, ctx: MessageContext, text: str, media: DownloadableMedia, *, meta: MetaInfo, cpu_executor: TPExecutor) -> None:
        assert isinstance(ctx.message, Message)
        style = next(style for style, names in _NAMES.items() if meta.keyword.lower() in names)
        video = (
            isinstance(media, (Video, Animation, VideoNote))
            or isinstance(media, Sticker)
            and media.is_video
            or isinstance(media, Document)
            and (media.mime_type or "").startswith("video/")
        )
        renderer: Callable[[io.BytesIO, str, CaptionStyle], Image.Image | io.BytesIO | None]
        renderer = caption_video if video else caption_image
        async with ChatActioner(ctx.message, ChatAction.UPLOAD_VIDEO if video else ChatAction.UPLOAD_PHOTO):
            with ExitStack() as resources:
                try:
                    result, timed_out = await run_downloaded(cpu_executor, media, renderer, text, style, bot=ctx.bot)
                except DownloadUnavailable:
                    error = "🤷🏻‍♂️ Не удалось скачать файл. Попробуй прислать его ещё раз."
                except CaptionLayoutError as issue:
                    error = str(issue)
                else:
                    if result is not None:
                        resources.callback(result.close)
                    if timed_out:
                        error = "🤷🏻‍♂️ Обработка заняла слишком много времени. Попробуй файл поменьше."
                    elif result is None:
                        error = "🤷🏻‍♂️ Не удалось обработать видео" if video else "🤷🏻‍♂️ Не удалось обработать картинку"
                    else:
                        keyboard = rate_keyboard() if ctx.message.chat.type != ChatType.PRIVATE else None
                        if isinstance(result, io.BytesIO):
                            await ctx.reply(
                                video=BufferedInputFile(result.getvalue(), filename=f"{style}.mp4"),
                                reply_markup=keyboard,
                                supports_streaming=True,
                            )
                        else:
                            output = resources.enter_context(image_bytes_io(result, ext="png"))
                            await ctx.reply(photo=BufferedInputFile(output.getvalue(), filename=f"{style}.png"), reply_markup=keyboard)
                        return
                await ctx.reply(error, to=ctx.message)
