"""One bounded delivery path for returned command output and explicit replies."""

import asyncio
import io
import math
from collections.abc import AsyncIterator, Buffer, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import copy_context
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Literal, overload
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.methods import SendAudio, SendDocument, SendMessage, SendPhoto, SendRichMessage, SendVideo, TelegramMethod
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InputFile,
    InputMediaPhoto,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockUnion,
    InputRichMessage,
    LinkPreviewOptions,
    Message,
    MessageEntity,
    ReplyMarkupUnion,
    ReplyParameters,
)
from aiogram.utils.formatting import Text
from PIL import Image

from msu_hub_bot.media.limits import MediaDimensionsError, validate_dimensions
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.files import input_file
from msu_hub_bot.telegram.response_text import (
    FormattedText,
    ResponseError as ResponseError,
    ResponseLimitError as ResponseLimitError,
    TextNeedsFile,
    can_rich,
    format_text,
    rich_text,
    split_text,
)
from msu_hub_bot.telegram.wrapper import BotWrapper

type MediaSource = bytes | io.BytesIO | Image.Image | Path | str | InputFile
type OutputKind = Literal["photo", "video", "document", "audio"]
type MessageReference = tuple[int, int]


@dataclass(frozen=True, slots=True)
class ResponsePolicy:
    rich: bool = True
    soft_messages: int = 3
    max_output_bytes: int = 20 * 1024 * 1024
    output: OutputKind | None = None
    timeout: float = 90

    def __post_init__(self) -> None:
        if type(self.rich) is not bool or type(self.soft_messages) is not int or not 1 <= self.soft_messages <= 20:
            raise ValueError("Response policy needs a boolean rich flag and 1–20 soft messages")
        if type(self.max_output_bytes) is not int or not 1 <= self.max_output_bytes <= 50 * 1024 * 1024:
            raise ValueError("Response byte budget must be between 1 byte and 50 MiB")
        if self.output not in {None, "photo", "video", "document", "audio"}:
            raise ValueError("Unsupported response output kind")
        if isinstance(self.timeout, bool) or not math.isfinite(self.timeout) or not 0 < self.timeout <= 300:
            raise ValueError("Response timeout must be between 0 and 300 seconds")


@dataclass(slots=True)
class ResponseProgress:
    """Owned by the invocation; cancellation never requires the error router."""

    confirmed: tuple[MessageReference, ...] = ()
    attempted_part: int | None = None
    total_parts: int = 0
    phase: Literal["preparing", "waiting", "sending", "complete", "failed", "cancelled"] = "preparing"
    uncertain: bool = False


class ResponseDeliveryError(Exception):
    """Safe internal failure metadata; never stringify the underlying API response."""

    def __init__(self, progress: ResponseProgress, cause: Exception) -> None:
        super().__init__("Telegram response delivery was not completed")
        self.confirmed = progress.confirmed
        self.attempted_part = progress.attempted_part
        self.total_parts = progress.total_parts
        self.uncertain = progress.uncertain
        self.reason = "uncertain" if self.uncertain else "not_attempted" if self.attempted_part is None else "rejected"
        self._cause = cause


@dataclass(slots=True)
class _Lane:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


# Plain Bot is useful to callers/tests, but production must reuse BotWrapper's lane.
# Entries for a plain Bot are weak, and chat entries exist only while in use.
_plain_lanes: WeakKeyDictionary[Bot, dict[int, _Lane]] = WeakKeyDictionary()


@asynccontextmanager
async def _send_lane(bot: Bot, chat_id: int) -> AsyncIterator[None]:
    if isinstance(bot, BotWrapper):
        async with bot.serial_send(chat_id):
            yield
        return
    lanes = _plain_lanes.setdefault(bot, {})
    entry = lanes.setdefault(chat_id, _Lane())
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if not entry.users:
            del lanes[chat_id]
        if not lanes:
            del _plain_lanes[bot]


class _LimitedBuffer(io.BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    def write(self, value: Buffer, /) -> int:
        with memoryview(value) as view:
            if self.tell() + view.nbytes > self.limit:
                raise ResponseLimitError("Файл результата слишком большой. Уменьшите размер изображения.")
        return super().write(value)


def _read_path(path: Path, limit: int) -> bytes:
    if not path.is_file() or path.stat().st_size > limit:
        raise ResponseLimitError("Файл результата недоступен или слишком большой.")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ResponseLimitError("Файл результата слишком большой.")
    return data


def _image_bytes(image: Image.Image, limit: int) -> bytes:
    with image, _LimitedBuffer(limit) as destination:
        image.save(destination, format="PNG")
        return destination.getvalue()


async def _prepare_bytes(prepare: Callable[[], bytes]) -> bytes:
    # Loop shutdown cancels every Task, including a to_thread wrapper. Keep the
    # executor Future itself so cancellation cannot erase our join handle.
    context = copy_context()
    worker = asyncio.get_running_loop().run_in_executor(None, lambda: context.run(prepare))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Finish bounded file/image preparation before releasing its caller's
        # resource scope. Telegram mutations are never shielded.
        while True:
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                if worker.done():
                    break
                continue
            except Exception:
                break
            else:
                break
        raise


async def _media(source: MediaSource, kind: OutputKind, limit: int, *, allow_remote_media: bool) -> tuple[str | InputFile, int]:
    limit = min(limit, (10 if kind == "photo" else 50) * 1024 * 1024)
    name = {"photo": "image.png", "video": "video.mp4", "document": "document.bin", "audio": "audio.mp3"}[kind]
    if isinstance(source, str):
        if not source or len(source) > 4096 or any(char.isspace() for char in source):
            raise ResponseError("Передайте Telegram file_id или уже загруженный файл вместо URL.")
        if "://" in source:
            try:
                url = urlsplit(source)
                valid = url.scheme in {"http", "https"} and bool(url.hostname) and url.username is None and url.password is None
            except ValueError:
                valid = False
            if not allow_remote_media or not valid:
                raise ResponseError("Передайте Telegram file_id или уже загруженный файл вместо URL.")
            # Telegram fetches an already-approved provider URL. Its native
            # limits apply; local byte accounting cannot measure remote media.
        return source, 0
    if isinstance(source, Image.Image):
        if kind not in {"photo", "document"}:
            raise ResponseError("Изображение можно отправить как фото или документ.")
        try:
            validate_dimensions(*source.size)
        except MediaDimensionsError:
            raise ResponseLimitError("Изображение слишком большое: максимум 16 Мп и 8192 пикселя по стороне.") from None
        if kind == "photo" and (sum(source.size) > 10000 or max(source.size) > min(source.size) * 20):
            raise ResponseError("Пропорции изображения не подходят для фото. Отправьте его документом.")
        # A cancelled caller must not close an image still being encoded by the worker.
        try:
            owned = source.copy()
            data = await _prepare_bytes(lambda: _image_bytes(owned, limit))
        except (OSError, ValueError) as error:
            if isinstance(error, ResponseError):
                raise
            raise ResponseError("Не удалось подготовить изображение результата.") from None
        name = "image.png"
    elif isinstance(source, Path | FSInputFile):
        path = Path(source.path) if isinstance(source, FSInputFile) else source
        name = (source.filename or path.name) if isinstance(source, FSInputFile) else path.name
        try:
            data = await _prepare_bytes(lambda: _read_path(path, limit))
        except OSError:
            raise ResponseError("Не удалось прочитать файл результата.") from None
    elif isinstance(source, BufferedInputFile):
        data, name = source.data, source.filename or name
    elif isinstance(source, io.BytesIO):
        try:
            with source.getbuffer() as buffer:
                if buffer.nbytes > limit:
                    raise ResponseLimitError("Файл результата слишком большой.")
            data = source.getvalue()
        except ValueError as error:
            if isinstance(error, ResponseError):
                raise
            raise ResponseError("Файл результата уже закрыт.") from None
    elif isinstance(source, bytes):
        data = source
    else:
        raise ResponseError("Этот поток нельзя безопасно отправить. Сначала загрузите его в файл или bytes.")
    if not data or len(data) > limit:
        raise ResponseLimitError("Файл результата пустой или слишком большой.")
    return input_file(data, name), len(data)


def _text_method(text: FormattedText) -> SendMessage:
    return SendMessage(
        chat_id=0, text=text.text, entities=list(text.entities), parse_mode=None, link_preview_options=LinkPreviewOptions(is_disabled=True)
    )


def _media_method(
    kind: OutputKind,
    source: str | InputFile,
    text: FormattedText | None,
    *,
    width: int | None,
    height: int | None,
    duration: int | None,
    supports_streaming: bool | None,
) -> TelegramMethod[Message]:
    caption = text.text if text and text.text else None
    entities = list(text.entities) if text and text.text else None
    if kind == "photo":
        return SendPhoto(chat_id=0, photo=source, caption=caption, caption_entities=entities, parse_mode=None)
    if kind == "video":
        return SendVideo(
            chat_id=0,
            video=source,
            caption=caption,
            caption_entities=entities,
            parse_mode=None,
            width=width,
            height=height,
            duration=duration,
            supports_streaming=supports_streaming,
        )
    if kind == "audio":
        return SendAudio(chat_id=0, audio=source, caption=caption, caption_entities=entities, parse_mode=None, duration=duration)
    return SendDocument(chat_id=0, document=source, caption=caption, caption_entities=entities, parse_mode=None)


def _chunks(text: FormattedText, limit: int, soft: int, *, rich: bool = False) -> list[FormattedText] | None:
    try:
        result = list(islice(split_text(text, limit, max_bytes=limit * 4 if rich else 32768, max_entities=1000 if rich else 100), soft + 1))
    except TextNeedsFile:
        return None
    return result if len(result) <= soft else None


async def _plan(
    target: Message,
    text: str | Text | None,
    entities: Sequence[MessageEntity] | None,
    policy: ResponsePolicy,
    *,
    sources: Sequence[tuple[OutputKind, MediaSource | None]],
    fixed: bool,
    width: int | None,
    height: int | None,
    duration: int | None,
    supports_streaming: bool | None,
    allow_remote_media: bool,
) -> list[TelegramMethod[Message]]:
    if target.ephemeral_message_id is not None or target.message_id <= 0:
        raise ResponseError("На это сообщение нельзя отправить обычный ответ.")
    selected = [(kind, source) for kind, source in sources if source is not None]
    if len(selected) > 1:
        raise ResponseError("За один вызов передайте один медиафайл.")
    if (
        any(value is not None and (type(value) is not int or value <= 0) for value in (width, height))
        or (duration is not None and (type(duration) is not int or duration < 0))
        or (supports_streaming is not None and type(supports_streaming) is not bool)
    ):
        raise ResponseError("Некорректные размеры или длительность медиа.")
    value = format_text(text, entities, policy.max_output_bytes)
    if not selected and not value.text.strip():
        raise ResponseError("В ответе нет текста или файла.")
    # Strict output validates the whole caption before preparing or sending media.
    if fixed and not value.fits(1024 if selected else 4096):
        raise ResponseLimitError("Ответ не помещается в одно сообщение. Сократите текст или используйте страницы.")
    media: str | InputFile | None = None
    media_bytes = 0
    kind: OutputKind | None = None
    if selected:
        kind, source = selected[0]
        media, media_bytes = await _media(source, kind, policy.max_output_bytes, allow_remote_media=allow_remote_media)
    if value.size_bytes + media_bytes > policy.max_output_bytes:
        raise ResponseLimitError("Результат слишком большой. Уменьшите объём запроса.")

    def native(caption: FormattedText | None = None) -> TelegramMethod[Message]:
        assert kind is not None and media is not None
        return _media_method(kind, media, caption, width=width, height=height, duration=duration, supports_streaming=supports_streaming)

    if fixed:
        return [native(value) if selected else _text_method(value)]
    if kind == "photo" and policy.rich and value.text.strip() and can_rich(value):
        # Unsupported rich entities retain their exact native representation.
        rich_parts = _chunks(value, 32768, policy.soft_messages, rich=True)
        if rich_parts is not None:
            rendered = [rich_text(part) for part in rich_parts]
            if all(part is not None for part in rendered):
                result: list[TelegramMethod[Message]] = []
                for index, content in enumerate(rendered):
                    assert content is not None
                    blocks: list[InputRichBlockUnion] = [InputRichBlockParagraph(text=content)]
                    if index == 0:
                        assert media is not None
                        blocks.append(InputRichBlockPhoto(photo=InputMediaPhoto(media=media, parse_mode=None)))
                    result.append(SendRichMessage(chat_id=0, rich_message=InputRichMessage(blocks=blocks, skip_entity_detection=True)))
                return result
        else:
            # The rich layout itself exceeds the soft budget: preserve full text in a file.
            return _file_plan(value, media_bytes, policy, [native()])
    if selected and (not value.text or value.fits(1024)):
        return [native(value)]
    if not value.text:
        return [native()]
    # A separate media message consumes one slot; Rich includes its photo in
    # the first text-bearing message. Media + complete file may require two
    # messages even when soft_messages=1, so no supplied content is discarded.
    parts = _chunks(value, 4096, policy.soft_messages - int(bool(selected)))
    if parts is None:
        return _file_plan(value, media_bytes, policy, [native()] if selected else [])
    return ([native()] if selected else []) + [_text_method(part) for part in parts]


def _file_plan(
    value: FormattedText, media_bytes: int, policy: ResponsePolicy, prefix: list[TelegramMethod[Message]]
) -> list[TelegramMethod[Message]]:
    payload = value.file_bytes()
    if len(payload) + media_bytes > policy.max_output_bytes:
        raise ResponseLimitError("Полный результат слишком большой для файла. Уменьшите объём запроса.")
    return [*prefix, SendDocument(chat_id=0, document=BufferedInputFile(payload, "result.txt"), parse_mode=None)]


@overload
async def send_response(
    target: Message,
    text: str | Text | None = None,
    *,
    fixed: Literal[True],
    policy: ResponsePolicy = ...,
    photo: MediaSource | None = ...,
    video: MediaSource | None = ...,
    document: MediaSource | None = ...,
    audio: MediaSource | None = ...,
    entities: Sequence[MessageEntity] | None = ...,
    reply_markup: ReplyMarkupUnion | None = ...,
    width: int | None = ...,
    height: int | None = ...,
    duration: int | None = ...,
    supports_streaming: bool | None = ...,
    allow_sending_without_reply: bool = ...,
    allow_remote_media: bool = ...,
    request_timeout: int | None = ...,
    progress: ResponseProgress | None = ...,
) -> Message: ...


@overload
async def send_response(
    target: Message,
    text: str | Text | None = None,
    *,
    fixed: Literal[False] = False,
    policy: ResponsePolicy = ...,
    photo: MediaSource | None = ...,
    video: MediaSource | None = ...,
    document: MediaSource | None = ...,
    audio: MediaSource | None = ...,
    entities: Sequence[MessageEntity] | None = ...,
    reply_markup: ReplyMarkupUnion | None = ...,
    width: int | None = ...,
    height: int | None = ...,
    duration: int | None = ...,
    supports_streaming: bool | None = ...,
    allow_sending_without_reply: bool = ...,
    allow_remote_media: bool = ...,
    request_timeout: int | None = ...,
    progress: ResponseProgress | None = ...,
) -> list[Message]: ...


@overload
async def send_response(
    target: Message,
    text: str | Text | None = None,
    *,
    fixed: bool,
    policy: ResponsePolicy = ...,
    photo: MediaSource | None = ...,
    video: MediaSource | None = ...,
    document: MediaSource | None = ...,
    audio: MediaSource | None = ...,
    entities: Sequence[MessageEntity] | None = ...,
    reply_markup: ReplyMarkupUnion | None = ...,
    width: int | None = ...,
    height: int | None = ...,
    duration: int | None = ...,
    supports_streaming: bool | None = ...,
    allow_sending_without_reply: bool = ...,
    allow_remote_media: bool = ...,
    request_timeout: int | None = ...,
    progress: ResponseProgress | None = ...,
) -> Message | list[Message]: ...


async def send_response(
    target: Message,
    text: str | Text | None = None,
    *,
    policy: ResponsePolicy = ResponsePolicy(),
    photo: MediaSource | None = None,
    video: MediaSource | None = None,
    document: MediaSource | None = None,
    audio: MediaSource | None = None,
    entities: Sequence[MessageEntity] | None = None,
    reply_markup: ReplyMarkupUnion | None = None,
    fixed: bool = False,
    width: int | None = None,
    height: int | None = None,
    duration: int | None = None,
    supports_streaming: bool | None = None,
    allow_sending_without_reply: bool = False,
    allow_remote_media: bool = False,
    request_timeout: int | None = None,
    progress: ResponseProgress | None = None,
) -> Message | list[Message]:
    """Plan before sending; fixed sends never change the requested native kind."""
    if type(allow_remote_media) is not bool or (
        request_timeout is not None and (type(request_timeout) is not int or not 1 <= request_timeout <= 300)
    ):
        raise ResponseError("Некорректные параметры доставки ответа.")
    bot = bot_for(target)
    state = progress if progress is not None else ResponseProgress()
    if state.phase != "preparing" or state.confirmed or state.attempted_part is not None:
        raise ValueError("Response progress cannot be reused")
    sent: list[Message] = []
    try:
        async with asyncio.timeout(policy.timeout):
            plan = await _plan(
                target,
                text,
                entities,
                policy,
                sources=(("photo", photo), ("video", video), ("document", document), ("audio", audio)),
                fixed=fixed,
                width=width,
                height=height,
                duration=duration,
                supports_streaming=supports_streaming,
                allow_remote_media=allow_remote_media,
            )
            state.total_parts, state.phase = len(plan), "waiting"
            async with _send_lane(bot, target.chat.id):
                reply_to = target.message_id
                for index, method in enumerate(plan):
                    method = method.model_copy(
                        update={
                            "chat_id": target.chat.id,
                            "business_connection_id": target.business_connection_id,
                            "message_thread_id": target.message_thread_id if target.is_topic_message else None,
                            "direct_messages_topic_id": target.direct_messages_topic.topic_id if target.direct_messages_topic else None,
                            "reply_parameters": ReplyParameters(
                                message_id=reply_to, allow_sending_without_reply=allow_sending_without_reply
                            ),
                            "reply_markup": reply_markup if index == len(plan) - 1 else None,
                        }
                    )
                    state.attempted_part, state.phase, state.uncertain = index, "sending", True
                    try:
                        result = await bot(method, request_timeout=request_timeout)
                    except Exception as error:
                        state.uncertain = not isinstance(
                            error,
                            (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramUnauthorizedError, TelegramRetryAfter),
                        )
                        state.phase = "failed"
                        raise ResponseDeliveryError(state, error) from None
                    if not isinstance(result, Message) or result.message_id <= 0:
                        state.phase = "failed"
                        raise ResponseDeliveryError(state, ValueError("Response did not confirm a usable message")) from None
                    sent.append(result)
                    state.confirmed += ((result.chat.id, result.message_id),)
                    state.uncertain, state.phase = False, "waiting"
                    reply_to = result.message_id
                state.phase = "complete"
    except asyncio.CancelledError:
        state.phase = "cancelled"
        raise
    except TimeoutError as error:
        state.phase = "failed"
        raise ResponseDeliveryError(state, error) from None
    return sent[0] if fixed else sent
