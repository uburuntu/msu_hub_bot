"""Bounded Telegram writes with explicit targets and observable uncertainty."""

import asyncio
import io
import math
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from itertools import islice
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from aiogram import Bot
from aiogram.client.default import Default
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.methods import (
    EditMessageCaption,
    EditMessageMedia,
    EditMessageText,
    SendAnimation,
    SendAudio,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendRichMessage,
    SendVideo,
    TelegramMethod,
)
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InaccessibleMessage,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    InputRichBlockAnimation,
    InputRichBlockAudio,
    InputRichBlockDocument,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockUnion,
    InputRichBlockVideo,
    InputRichMessage,
    LinkPreviewOptions,
    Message,
    MessageEntity,
    ReplyMarkupUnion,
    ReplyParameters,
)
from aiogram.utils.formatting import Text
from pydantic import BaseModel

from .formatting import (
    FormattedText,
    ResponseError,
    ResponseLimitError,
    TextNeedsFile,
    can_rich,
    format_text,
    rich_text,
    split_text,
    units,
)

type MediaSource = bytes | io.BytesIO | Path | str | InputFile
type OutputKind = Literal["photo", "video", "audio", "document", "animation"]
type TargetKind = Literal["text", "rich", "photo", "video", "audio", "document", "animation"]
type NativeResult = Message | bool
type MessageReference = tuple[int, int]


@dataclass(frozen=True, slots=True)
class ResponsePolicy:
    rich: bool = True
    soft_messages: int = 3
    max_output_bytes: int = 20 * 1024 * 1024
    timeout: float = 90

    def __post_init__(self) -> None:
        if type(self.rich) is not bool or type(self.soft_messages) is not int or not 1 <= self.soft_messages <= 20:
            raise ValueError("Expected rich=True/False and 1–20 soft messages")
        if type(self.max_output_bytes) is not int or not 1 <= self.max_output_bytes <= 64 * 1024 * 1024:
            raise ValueError("Output budget must be between 1 byte and 64 MiB")
        if isinstance(self.timeout, bool) or not math.isfinite(self.timeout) or not 0 < self.timeout <= 300:
            raise ValueError("Delivery timeout must be between 0 and 300 seconds")


DEFAULT_POLICY = ResponsePolicy()


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    """An address, independent of the actor and of acquired command input."""

    chat_id: int | str | None = None
    message_id: int | None = None
    thread_id: int | None = None
    business_connection_id: str | None = None
    inline_message_id: str | None = None
    kind: TargetKind | None = None
    direct_messages_topic_id: int | None = None

    def __post_init__(self) -> None:
        if self.inline_message_id is not None:
            if not self.inline_message_id or self.chat_id is not None or self.message_id is not None:
                raise ResponseError("An inline target must have only its inline message address")
            if self.thread_id is not None or self.direct_messages_topic_id is not None:
                raise ResponseError("Inline messages do not have a known chat topic")
        elif type(self.chat_id) not in {int, str} or self.chat_id == 0 or self.chat_id == "":
            raise ResponseError("A delivery target needs a chat or inline message address")
        if self.message_id is not None and (type(self.message_id) is not int or self.message_id <= 0):
            raise ResponseError("A target message ID must be positive")
        if self.kind not in {None, "text", "rich", "photo", "video", "audio", "document", "animation"}:
            raise ResponseError("Unsupported target message kind")
        if any(
            value is not None and (type(value) is not int or value <= 0)
            for value in (self.thread_id, self.direct_messages_topic_id)
        ):
            raise ResponseError("Topic identifiers must be positive integers")

    @classmethod
    def from_message(cls, message: Message | InaccessibleMessage) -> DeliveryTarget:
        if isinstance(message, InaccessibleMessage):
            return cls(chat_id=message.chat.id, message_id=message.message_id)
        kind: TargetKind | None = None
        media_kinds: tuple[OutputKind, ...] = ("photo", "video", "animation", "audio", "document")
        for candidate in media_kinds:
            if getattr(message, candidate, None):
                kind = candidate
                break
        if message.rich_message is not None:
            kind = "rich"
        elif kind is None and message.text is not None:
            kind = "text"
        return cls(
            chat_id=message.chat.id,
            message_id=message.message_id,
            thread_id=message.message_thread_id if message.is_topic_message else None,
            business_connection_id=message.business_connection_id,
            direct_messages_topic_id=message.direct_messages_topic.topic_id if message.direct_messages_topic else None,
            kind=kind,
        )


type Target = DeliveryTarget | Message | InaccessibleMessage


def resolve_target(target: Target) -> DeliveryTarget:
    return target if isinstance(target, DeliveryTarget) else DeliveryTarget.from_message(target)


@dataclass(slots=True)
class DeliveryProgress:
    confirmed: tuple[MessageReference, ...] = ()
    attempted_part: int | None = None
    total_parts: int = 0
    phase: Literal["preparing", "waiting", "sending", "complete", "failed", "cancelled"] = "preparing"
    uncertain: bool = False
    confirmed_count: int | None = None


class DeliveryError(Exception):
    """Delivery metadata deliberately excludes raw API responses and content."""

    def __init__(self, progress: DeliveryProgress, cause: Exception) -> None:
        super().__init__("Telegram delivery was not completed")
        self.progress = replace(progress)
        self.confirmed = progress.confirmed
        self.attempted_part = progress.attempted_part
        self.total_parts = progress.total_parts
        self.uncertain = progress.uncertain
        self.reason = "uncertain" if self.uncertain else "not_attempted" if self.attempted_part is None else "rejected"
        self.cause = cause


ResponseDeliveryError = DeliveryError
ResponseProgress = DeliveryProgress


@dataclass(frozen=True, slots=True)
class CompletedResponse:
    """Confirmed result delivery, independent of a later status-message update."""

    result: NativeResult
    spilled: bool = False
    status_error: DeliveryError | None = None


def rejected(error: Exception) -> bool:
    """A received rejection differs from an interrupted or unconfirmed write."""
    return isinstance(
        error,
        (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramUnauthorizedError, TelegramRetryAfter),
    )


@dataclass(slots=True)
class _Lane:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


_lanes: WeakKeyDictionary[Bot, dict[tuple[object, ...], _Lane]] = WeakKeyDictionary()


@asynccontextmanager
async def _lane(bot: Bot, target: DeliveryTarget) -> AsyncIterator[None]:
    lanes = _lanes.setdefault(bot, {})
    key = (target.chat_id, target.business_connection_id, target.inline_message_id)
    entry = lanes.setdefault(key, _Lane())
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if not entry.users:
            del lanes[key]
        if not lanes:
            del _lanes[bot]


def _read_path(path: Path, limit: int) -> bytes:
    if not path.is_file() or path.stat().st_size > limit:
        raise ResponseLimitError("The output file is unavailable or exceeds its byte budget")
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ResponseLimitError("The output file exceeds its byte budget")
    return value


async def _path_snapshot(path: Path, limit: int) -> bytes:
    # Keep the executor future alive through cancellation so managed temporary
    # paths cannot be cleaned up while preparation is still reading them.
    future = asyncio.get_running_loop().run_in_executor(None, _read_path, path, limit)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - the original cancellation remains primary
                break
        if future.done() and not future.cancelled():
            future.exception()
        raise


async def prepare_media(
    source: MediaSource, kind: OutputKind, limit: int, *, allow_remote_media: bool = False
) -> tuple[str | InputFile, int]:
    """Snapshot finite local inputs before writes; never close caller-owned media."""
    name = {
        "photo": "image.jpg",
        "video": "video.mp4",
        "audio": "audio.mp3",
        "document": "document.bin",
        "animation": "animation.mp4",
    }[kind]
    if isinstance(source, str):
        if not source or len(source.encode()) > 8192:
            raise ResponseError("Expected a bounded Telegram file ID or approved URL")
        parsed = urlsplit(source)
        if (parsed.scheme or "://" in source) and (
            not allow_remote_media
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ResponseError("Remote media requires an explicitly approved HTTP(S) URL")
        return source, 0
    if isinstance(source, Path | FSInputFile):
        path = Path(source.path) if isinstance(source, FSInputFile) else source
        name = (source.filename or path.name) if isinstance(source, FSInputFile) else path.name
        try:
            value = await _path_snapshot(path, limit)
        except OSError:
            raise ResponseError("The output file could not be read") from None
    elif isinstance(source, BufferedInputFile):
        value, name = source.data, source.filename or name
    elif isinstance(source, io.BytesIO):
        try:
            with source.getbuffer() as buffer:
                if buffer.nbytes > limit:
                    raise ResponseLimitError("The output file exceeds its byte budget")
            value = source.getvalue()
        except ValueError as error:
            if isinstance(error, ResponseError):
                raise
            raise ResponseError("The output stream is closed") from None
    elif isinstance(source, bytes):
        value = source
    else:
        raise ResponseError("Unbounded streams are not supported; supply bytes, a path or BufferedInputFile")
    if not value or len(value) > limit:
        raise ResponseLimitError("The output file is empty or exceeds its byte budget")
    return BufferedInputFile(value, name), len(value)


async def _prepare_rich(
    rich: InputRichMessage,
    policy: ResponsePolicy,
    *,
    inline: bool,
    allow_remote_media: bool,
    reserved_bytes: int = 0,
) -> InputRichMessage:
    """Bound and snapshot one complete native rich replacement; never merge blocks."""
    if sum(item is not None for item in (rich.blocks, rich.html, rich.markdown)) != 1:
        raise ResponseError("A rich replacement needs exactly one of blocks, html or markdown")
    if not (rich.blocks or rich.html or rich.markdown):
        raise ResponseError("A rich replacement cannot be empty")
    nodes = string_units = 0
    byte_count = reserved_bytes

    async def copy(value: object, depth: int = 0, field_name: str = "") -> object:
        nonlocal nodes, byte_count, string_units
        nodes += 1
        if nodes > 10_000 or depth > 32:
            raise ResponseLimitError("Rich content exceeds its structural budget")
        if isinstance(value, Default):
            # Explicit rich content must not inherit a bot-wide parse mode or
            # silently borrow layout settings from unrelated native defaults.
            return None
        if isinstance(value, InputFile):
            if inline:
                raise ResponseError("Inline rich edits cannot upload new files")
            media, size = await prepare_media(value, "document", policy.max_output_bytes - byte_count)
            byte_count += size
            return media
        if isinstance(value, BaseModel):
            fields = {name: await copy(getattr(value, name), depth + 1, name) for name in type(value).model_fields}
            for name, extra in (value.model_extra or {}).items():
                fields[name] = await copy(extra, depth + 1, name)
            return value.model_copy(update=fields)
        if isinstance(value, list | tuple):
            return [await copy(item, depth + 1, field_name) for item in value]
        if isinstance(value, dict):
            return {key: await copy(item, depth + 1, str(key)) for key, item in value.items()}
        if isinstance(value, str):
            try:
                byte_count += len(value.encode())
                string_units += units(value)
            except UnicodeError:
                raise ResponseError("Rich content contains invalid Unicode") from None
            if (
                field_name in {"media", "photo", "video", "audio", "document", "animation", "cover", "thumbnail"}
                and urlsplit(value).scheme
            ):
                if inline:
                    raise ResponseError("Inline rich edits cannot fetch new media URLs")
                await prepare_media(value, "document", policy.max_output_bytes, allow_remote_media=allow_remote_media)
        elif value is not None and not isinstance(value, bool | int | float):
            raise ResponseError("Rich content contains an unsupported native value")
        if byte_count > policy.max_output_bytes or string_units > 32768:
            raise ResponseLimitError("The complete rich replacement exceeds its text or byte budget")
        return value

    return cast(InputRichMessage, await copy(rich))


def _prepare_markup(markup: ReplyMarkupUnion | None, limit: int) -> tuple[ReplyMarkupUnion | None, int]:
    """Bound and snapshot owned keyboard metadata before yielding to delivery."""
    if markup is None:
        return None, 0
    nodes = byte_count = 0

    def copy(value: object, depth: int = 0) -> object:
        nonlocal nodes, byte_count
        nodes += 1
        if nodes > 10_000 or depth > 32:
            raise ResponseLimitError("Reply markup exceeds its structural budget")
        if isinstance(value, BaseModel):
            fields = {
                name: copy(item, depth + 1)
                for name in type(value).model_fields
                if (item := getattr(value, name)) is not None
            }
            for name, extra in (value.model_extra or {}).items():
                copy(name, depth + 1)
                fields[name] = copy(extra, depth + 1)
            return value.model_copy(update=fields)
        if isinstance(value, list | tuple):
            return [copy(item, depth + 1) for item in value]
        if isinstance(value, dict):
            return {copy(key, depth + 1): copy(item, depth + 1) for key, item in value.items()}
        if isinstance(value, str):
            if len(value) > limit - byte_count:
                raise ResponseLimitError("Reply markup exceeds its byte budget")
            try:
                byte_count += len(value.encode())
            except UnicodeError:
                raise ResponseError("Reply markup contains invalid Unicode") from None
            if byte_count > limit:
                raise ResponseLimitError("Reply markup exceeds its byte budget")
        elif value is not None and not isinstance(value, bool | int | float):
            raise ResponseError("Reply markup contains an unsupported native value")
        return value

    prepared = cast(ReplyMarkupUnion, copy(markup))
    # Include field names, scalar metadata and containers, while leaving the
    # enclosing Telegram request envelope outside the owned-content budget.
    size = len(prepared.model_dump_json(exclude_none=True).encode())
    if size > limit:
        raise ResponseLimitError("Reply markup exceeds its byte budget")
    return prepared, size


def _text_method(value: FormattedText) -> SendMessage:
    return SendMessage(
        chat_id=1,
        text=value.text,
        entities=list(value.entities),
        parse_mode=None,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


def _media_method(
    kind: OutputKind, media: str | InputFile, value: FormattedText, options: dict[str, Any]
) -> TelegramMethod[Message]:
    common: dict[str, Any] = {
        "chat_id": 1,
        "caption": value.text or None,
        "caption_entities": list(value.entities),
        "parse_mode": None,
    }
    match kind:
        case "photo":
            return SendPhoto(photo=media, **common)
        case "video":
            return SendVideo(video=media, **common, **options)
        case "audio":
            return SendAudio(audio=media, **common, duration=options.get("duration"))
        case "animation":
            return SendAnimation(
                animation=media,
                **common,
                **{key: value for key, value in options.items() if key != "supports_streaming"},
            )
        case "document":
            return SendDocument(document=media, **common)


def _rich_media(kind: OutputKind, media: str | InputFile, options: dict[str, Any]) -> InputRichBlockUnion:
    match kind:
        case "photo":
            return InputRichBlockPhoto(photo=InputMediaPhoto(media=media, parse_mode=None))
        case "video":
            return InputRichBlockVideo(video=InputMediaVideo(media=media, parse_mode=None, **options))
        case "audio":
            return InputRichBlockAudio(
                audio=InputMediaAudio(media=media, parse_mode=None, duration=options.get("duration"))
            )
        case "animation":
            return InputRichBlockAnimation(
                animation=InputMediaAnimation(
                    media=media,
                    parse_mode=None,
                    **{key: value for key, value in options.items() if key != "supports_streaming"},
                )
            )
        case "document":
            return InputRichBlockDocument(document=InputMediaDocument(media=media, parse_mode=None))


def _chunks(value: FormattedText, limit: int, budget: int, *, rich: bool = False) -> list[FormattedText] | None:
    try:
        parts = list(
            islice(
                split_text(value, limit, max_bytes=limit * 4 if rich else 32768, max_entities=1000 if rich else 100),
                max(0, budget) + 1,
            )
        )
    except TextNeedsFile:
        return None
    return parts if len(parts) <= budget else None


def _file_plan(
    value: FormattedText, media_bytes: int, policy: ResponsePolicy, prefix: list[TelegramMethod[Message]]
) -> list[TelegramMethod[Message]]:
    payload = value.file_bytes()
    if len(payload) + media_bytes > policy.max_output_bytes:
        raise ResponseLimitError("The complete output exceeds the file budget")
    return [*prefix, SendDocument(chat_id=1, document=BufferedInputFile(payload, "result.txt"), parse_mode=None)]


async def _plan(
    text: str | Text | None,
    entities: Sequence[MessageEntity] | None,
    policy: ResponsePolicy,
    sources: Sequence[tuple[OutputKind, MediaSource | None]],
    *,
    fixed: bool,
    options: dict[str, Any],
    allow_remote_media: bool,
    reserved_bytes: int = 0,
) -> list[TelegramMethod[Message]]:
    selected = [(kind, source) for kind, source in sources if source is not None]
    if len(selected) > 1:
        raise ResponseError("Supply one media item per delivery")
    value = format_text(text, entities, policy.max_output_bytes - reserved_bytes)
    if not selected and not value.text.strip():
        raise ResponseError("The response contains no text or media")
    if fixed and not value.fits(1024 if selected else 4096):
        raise ResponseLimitError("A fixed response must fit one native message")
    media_bytes = 0
    media: str | InputFile | None = None
    kind: OutputKind | None = None
    if selected:
        kind, source = selected[0]
        assert source is not None
        media, media_bytes = await prepare_media(
            source,
            kind,
            policy.max_output_bytes - reserved_bytes - value.size_bytes,
            allow_remote_media=allow_remote_media,
        )
    if value.size_bytes + media_bytes + reserved_bytes > policy.max_output_bytes:
        raise ResponseLimitError("The complete response exceeds its byte budget")

    def native(caption: FormattedText | None = None) -> TelegramMethod[Message]:
        assert kind is not None and media is not None
        return _media_method(kind, media, caption if caption is not None else FormattedText(""), options)

    if fixed:
        return [native(value) if selected else _text_method(value)]
    if policy.rich and value.text.strip() and can_rich(value) and (selected or not value.fits(4096)):
        parts = _chunks(value, 32768, policy.soft_messages, rich=True)
        if parts is not None:
            plan: list[TelegramMethod[Message]] = []
            for index, part in enumerate(parts):
                rendered = rich_text(part)
                assert rendered is not None
                blocks: list[InputRichBlockUnion] = [InputRichBlockParagraph(text=rendered)]
                if index == 0 and kind is not None and media is not None:
                    blocks.append(_rich_media(kind, media, options))
                plan.append(
                    SendRichMessage(chat_id=1, rich_message=InputRichMessage(blocks=blocks, skip_entity_detection=True))
                )
            return plan
        return _file_plan(value, media_bytes + reserved_bytes, policy, [native()] if selected else [])
    if selected and (not value.text or value.fits(1024)):
        return [native(value)]
    parts = _chunks(value, 4096, policy.soft_messages - int(bool(selected)))
    if parts is None:
        return _file_plan(value, media_bytes + reserved_bytes, policy, [native()] if selected else [])
    return ([native()] if selected else []) + [_text_method(part) for part in parts]


def _options(
    width: int | None, height: int | None, duration: int | None, supports_streaming: bool | None
) -> dict[str, Any]:
    if (
        any(value is not None and (type(value) is not int or value <= 0) for value in (width, height))
        or (duration is not None and (type(duration) is not int or duration < 0))
        or (supports_streaming is not None and type(supports_streaming) is not bool)
    ):
        raise ResponseError("Invalid media dimensions, duration or streaming option")
    return {"width": width, "height": height, "duration": duration, "supports_streaming": supports_streaming}


def _new_progress(progress: DeliveryProgress | None) -> DeliveryProgress:
    result = progress if progress is not None else DeliveryProgress()
    if result.phase != "preparing" or result.confirmed or result.attempted_part is not None:
        raise ValueError("Delivery progress cannot be reused")
    return result


async def send_response(
    bot: Bot,
    target: Target,
    text: str | Text | None = None,
    *,
    policy: ResponsePolicy = DEFAULT_POLICY,
    photo: MediaSource | None = None,
    video: MediaSource | None = None,
    audio: MediaSource | None = None,
    document: MediaSource | None = None,
    animation: MediaSource | None = None,
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
    progress: DeliveryProgress | None = None,
) -> Message | list[Message]:
    """Plan the complete response before sending; never retry unconfirmed writes."""
    address = resolve_target(target)
    if address.chat_id is None:
        raise ResponseError("An inline message has no known chat for a new reply")
    options = _options(width, height, duration, supports_streaming)
    state = _new_progress(progress)
    sent: list[Message] = []
    try:
        async with asyncio.timeout(policy.timeout):
            reply_markup, markup_bytes = _prepare_markup(reply_markup, policy.max_output_bytes)
            plan = await _plan(
                text,
                entities,
                policy,
                (
                    ("photo", photo),
                    ("video", video),
                    ("audio", audio),
                    ("document", document),
                    ("animation", animation),
                ),
                fixed=fixed,
                options=options,
                allow_remote_media=allow_remote_media,
                reserved_bytes=markup_bytes,
            )
            state.total_parts, state.phase = len(plan), "waiting"
            async with _lane(bot, address):
                reply_to = address.message_id
                for index, method in enumerate(plan):
                    method = method.model_copy(
                        update={
                            "chat_id": address.chat_id,
                            "business_connection_id": address.business_connection_id,
                            "message_thread_id": address.thread_id,
                            "direct_messages_topic_id": address.direct_messages_topic_id,
                            "reply_parameters": ReplyParameters(
                                message_id=reply_to, allow_sending_without_reply=allow_sending_without_reply
                            )
                            if reply_to
                            else None,
                            "reply_markup": reply_markup if index == len(plan) - 1 else None,
                        }
                    )
                    state.attempted_part, state.phase, state.uncertain = index, "sending", True
                    try:
                        result = await bot(method, request_timeout=request_timeout)
                    except Exception as error:  # noqa: BLE001 - preserve every outbound-write failure and its uncertainty
                        state.uncertain, state.phase = not rejected(error), "failed"
                        raise DeliveryError(state, error) from None
                    if not isinstance(result, Message) or result.message_id <= 0:
                        state.phase = "failed"
                        raise DeliveryError(state, ValueError("Telegram did not confirm a message")) from None
                    sent.append(result)
                    state.confirmed += ((result.chat.id, result.message_id),)
                    state.uncertain, state.phase = False, "waiting"
                    reply_to = result.message_id
                state.phase = "complete"
    except asyncio.CancelledError:
        state.phase = "cancelled"
        raise
    except ResponseError:
        state.phase = "failed"
        raise
    except TimeoutError as error:
        state.phase = "failed"
        raise DeliveryError(state, error) from None
    return sent[0] if len(sent) == 1 else sent


async def edit_response(
    bot: Bot,
    target: Target,
    text: str | Text | None = None,
    *,
    policy: ResponsePolicy = DEFAULT_POLICY,
    kind: TargetKind | None = None,
    rich_message: InputRichMessage | None = None,
    photo: MediaSource | None = None,
    video: MediaSource | None = None,
    audio: MediaSource | None = None,
    document: MediaSource | None = None,
    animation: MediaSource | None = None,
    entities: Sequence[MessageEntity] | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    link_preview_options: LinkPreviewOptions | None = None,
    allow_remote_media: bool = False,
    request_timeout: int | None = None,
    progress: DeliveryProgress | None = None,
) -> NativeResult:
    """Edit one existing UI. Omitted/None markup clears; snapshots are never reused."""
    address = resolve_target(target)
    state = _new_progress(progress)
    try:
        async with asyncio.timeout(policy.timeout):
            prepared_markup, markup_bytes = _prepare_markup(reply_markup, policy.max_output_bytes)
            if address.inline_message_id is None and address.message_id is None:
                raise ResponseError("Editing requires an existing message address")
            sources: tuple[tuple[OutputKind, MediaSource | None], ...] = (
                ("photo", photo),
                ("video", video),
                ("audio", audio),
                ("document", document),
                ("animation", animation),
            )
            selected = [(media_kind, value) for media_kind, value in sources if value is not None]
            if len(selected) > 1:
                raise ResponseError("Supply one replacement media item")
            target_kind = kind or address.kind
            if kind is not None and address.kind is not None and kind != address.kind:
                raise ResponseError("An explicit kind cannot change the known existing message kind")
            if target_kind is None:
                raise ResponseError("The existing message kind is unknown; declare kind explicitly")
            if rich_message is not None and (
                target_kind != "rich" or text is not None or entities is not None or selected
            ):
                raise ResponseError(
                    "A complete rich replacement cannot be combined with text, entities or media arguments"
                )
            value = format_text(text, entities, policy.max_output_bytes - markup_bytes)
            if target_kind == "rich" and rich_message is None:
                raise ResponseError("Rich edits require an explicit complete native rich-message replacement")
            caption = target_kind in {"photo", "video", "audio", "document", "animation"}
            if not value.fits(1024 if caption else 4096):
                raise ResponseLimitError("An edit cannot split, repost or change message kind")
            common: dict[str, Any] = {
                "chat_id": address.chat_id,
                "message_id": address.message_id,
                "inline_message_id": address.inline_message_id,
                "business_connection_id": address.business_connection_id,
                "reply_markup": prepared_markup,
            }
            method: TelegramMethod[NativeResult]
            if rich_message is not None:
                prepared_rich = await _prepare_rich(
                    rich_message,
                    policy,
                    inline=address.inline_message_id is not None,
                    allow_remote_media=allow_remote_media,
                    reserved_bytes=markup_bytes,
                )
                method = EditMessageText(rich_message=prepared_rich, parse_mode=None, **common)
            elif selected:
                media_kind, source = selected[0]
                if media_kind != target_kind:
                    raise ResponseError("Changing the existing media kind requires a native Telegram operation")
                assert source is not None
                media, size = await prepare_media(
                    source,
                    media_kind,
                    policy.max_output_bytes - markup_bytes - value.size_bytes,
                    allow_remote_media=allow_remote_media,
                )
                if size + value.size_bytes + markup_bytes > policy.max_output_bytes:
                    raise ResponseLimitError("The complete response exceeds its byte budget")
                if address.inline_message_id is not None and isinstance(media, InputFile):
                    raise ResponseError("Inline media edits cannot upload a new file")
                media_type = {
                    "photo": InputMediaPhoto,
                    "video": InputMediaVideo,
                    "audio": InputMediaAudio,
                    "document": InputMediaDocument,
                    "animation": InputMediaAnimation,
                }[media_kind]
                replacement = media_type(
                    media=media, caption=value.text or None, caption_entities=list(value.entities), parse_mode=None
                )
                method = EditMessageMedia(media=replacement, **common)
            elif caption:
                method = EditMessageCaption(
                    caption=value.text, caption_entities=list(value.entities), parse_mode=None, **common
                )
            else:
                if not value.text.strip():
                    raise ResponseError("A text edit cannot be empty")
                method = EditMessageText(
                    text=value.text,
                    entities=list(value.entities),
                    parse_mode=None,
                    link_preview_options=link_preview_options or LinkPreviewOptions(is_disabled=True),
                    **common,
                )
            state.total_parts, state.phase = 1, "waiting"
            async with _lane(bot, address):
                state.attempted_part, state.phase, state.uncertain = 0, "sending", True
                try:
                    result = await bot(method, request_timeout=request_timeout)
                except TelegramBadRequest as error:
                    if "message is not modified" not in error.message.casefold():
                        raise
                    # The requested full edit is already present, including markup.
                    result = True
                if not isinstance(result, Message) and result is not True:
                    raise ValueError("Telegram did not confirm the edit")
                if isinstance(result, Message):
                    state.confirmed = ((result.chat.id, result.message_id),)
                state.phase, state.uncertain = "complete", False
                return result
    except asyncio.CancelledError:
        state.phase = "cancelled"
        raise
    except ResponseError:
        state.phase = "failed"
        raise
    except Exception as error:  # noqa: BLE001 - preserve every outbound-write failure and its uncertainty
        state.phase, state.uncertain = "failed", state.attempted_part is not None and not rejected(error)
        raise DeliveryError(state, error) from None


async def complete_response(
    bot: Bot,
    status: Target,
    text: str | Text,
    *,
    overflow_to: Target,
    overflow_notice: str | Text,
    policy: ResponsePolicy = DEFAULT_POLICY,
    entities: Sequence[MessageEntity] | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    link_preview_options: LinkPreviewOptions | None = None,
    request_timeout: int | None = None,
    progress: DeliveryProgress | None = None,
) -> CompletedResponse:
    """Complete a text status, spilling only preflighted overflow to one full file.

    ``progress`` describes the result write, including cancellation. A confirmed
    file is independent of its later status notice; that notice's delivery error
    is returned separately and never causes an upload retry.
    """
    state = _new_progress(progress)
    try:
        address = resolve_target(status)
        if address.kind != "text" or address.message_id is None or address.chat_id is None:
            raise ResponseError("Completion requires an existing chat text status")
        prepared_markup, markup_bytes = _prepare_markup(reply_markup, policy.max_output_bytes)
        if prepared_markup is not None and not isinstance(prepared_markup, InlineKeyboardMarkup):
            raise ResponseError("A text status accepts only inline controls")
        value = format_text(text, entities, policy.max_output_bytes - markup_bytes)
        if not value.text.strip():
            raise ResponseError("A completed response cannot be empty")
        if value.fits(4096):
            result = await edit_response(
                bot,
                address,
                value.text,
                entities=value.entities,
                reply_markup=prepared_markup,
                link_preview_options=link_preview_options,
                policy=policy,
                request_timeout=request_timeout,
                progress=state,
            )
            return CompletedResponse(result)
        destination = resolve_target(overflow_to)
        if destination.chat_id is None:
            raise ResponseError("A full result file needs an explicit chat target")
        notice = format_text(overflow_notice, None, policy.max_output_bytes - markup_bytes)
        if not notice.text.strip() or not notice.fits(4096):
            raise ResponseLimitError("The completion notice must fit one text message")
        payload = value.file_bytes()
        if len(payload) + notice.size_bytes + markup_bytes > policy.max_output_bytes:
            raise ResponseLimitError("The complete result and status notice exceed their byte budget")
        output = await send_response(
            bot,
            destination,
            document=BufferedInputFile(payload, "result.txt"),
            fixed=True,
            policy=policy,
            request_timeout=request_timeout,
            progress=state,
        )
        assert isinstance(output, Message)
        try:
            await edit_response(
                bot,
                address,
                notice.text,
                entities=notice.entities,
                reply_markup=prepared_markup,
                policy=policy,
                request_timeout=request_timeout,
            )
        except DeliveryError as error:
            return CompletedResponse(output, spilled=True, status_error=error)
        return CompletedResponse(output, spilled=True)
    except ResponseError:
        state.phase = "failed"
        raise
