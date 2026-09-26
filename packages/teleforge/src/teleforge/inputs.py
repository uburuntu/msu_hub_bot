"""Typed invocation inputs with explicit provenance and bounded resource ownership."""

import asyncio
import inspect
import io
import re
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from types import UnionType
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeAliasType, Union, get_args, get_origin, get_type_hints

from aiogram.types import (
    Animation,
    Audio,
    Document,
    Message,
    MessageOriginUser,
    PhotoSize,
    Sticker,
    TelegramObject,
    Video,
    VideoNote,
    Voice,
)
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from .context import Context
from .issues import ConfigurationError as ConfigurationError
from .issues import InputError as InputError
from .rich_input import rich_media, rich_text

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
_MISSING = object()
type Downloadable = PhotoSize | Document | Video | Animation | VideoNote | Sticker | Audio | Voice


if TYPE_CHECKING:
    from .parameters import ParameterPlan


class _ValidatedPayload(dict[str, Any]):
    """Internal card-codec output; do not execute its field validators again."""


@contextmanager
def _owned_inputs() -> Iterator[ExitStack]:
    resources = ExitStack()
    try:
        yield resources
    except BaseException as primary:
        try:
            resources.close()
        except Exception as cleanup:  # noqa: BLE001 - retain the primary operation/cancellation failure
            # A retained BytesIO view can prevent close. Preserve the operation's
            # error while ExitStack still attempts every other resource cleanup.
            primary.add_note(f"Input cleanup also failed ({type(cleanup).__name__})")
        raise
    else:
        resources.close()


@dataclass(frozen=True, slots=True)
class Argument:
    strict: bool = False
    clamp: tuple[int | float, int | float] | None = None

    def __post_init__(self) -> None:
        if self.clamp is not None and self.clamp[0] > self.clamp[1]:
            raise ValueError("Argument clamp bounds are reversed")


@dataclass(frozen=True, slots=True)
class TextInput:
    """Acquire text, optionally preferring a document within each selected source."""

    reply: bool = True
    document: bool | Literal["prefer"] = False
    max_chars: int | None = None
    max_bytes: int = MAX_DOWNLOAD_BYTES

    def __post_init__(self) -> None:
        if type(self.document) is not bool and self.document != "prefer":
            raise ValueError("Text document policy must be False, True or 'prefer'")
        if self.max_bytes <= 0 or self.max_chars is not None and self.max_chars <= 0:
            raise ValueError("Text input limits must be positive")


@dataclass(frozen=True, slots=True)
class ImageInput:
    reply: bool = True
    avatar: bool = False
    max_bytes: int = MAX_DOWNLOAD_BYTES
    max_pixels: int = 16_000_000
    max_dimension: int = 8192

    def __post_init__(self) -> None:
        if min(self.max_bytes, self.max_pixels, self.max_dimension) <= 0:
            raise ValueError("Image input limits must be positive")


@dataclass(frozen=True, slots=True)
class VideoInput:
    reply: bool = True
    max_bytes: int = MAX_DOWNLOAD_BYTES

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("Video input limits must be positive")


@dataclass(frozen=True, slots=True)
class DocumentInput:
    reply: bool = True
    max_bytes: int = MAX_DOWNLOAD_BYTES

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("Document input limits must be positive")


@dataclass(frozen=True, slots=True)
class MediaInput:
    """Prefer attached media, trying kinds in declaration order within each message."""

    reply: bool = True
    avatar: bool = False
    max_bytes: int = MAX_DOWNLOAD_BYTES
    kinds: tuple[Literal["image", "video", "document", "audio"], ...] = ("image", "video", "document", "audio")

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("Media input limits must be positive")
        if not self.kinds or any(kind not in {"image", "video", "document", "audio"} for kind in self.kinds):
            raise ValueError("Media kinds must select image, video, document or audio")
        if self.avatar and "image" not in self.kinds:
            raise ValueError("Avatar fallback requires images")


type MediaDeclaration = ImageInput | VideoInput | DocumentInput | MediaInput
type Declaration = Argument | TextInput | MediaDeclaration
_MEDIA = (ImageInput, VideoInput, DocumentInput, MediaInput)


def representation(annotation: Any) -> Any:
    """Unwrap Annotated and one optional type without flattening meaningful unions."""
    if isinstance(annotation, TypeAliasType):
        return representation(annotation.__value__)
    if get_origin(annotation) is Annotated:
        return representation(get_args(annotation)[0])
    if get_origin(annotation) in (Union, UnionType):
        choices = [choice for choice in get_args(annotation) if choice is not type(None)]
        if len(choices) == 1:
            return representation(choices[0])
    return annotation


def ordinary(annotation: Any) -> bool:
    annotation = representation(annotation)
    if annotation in (str, int, float, bool):
        return True
    if get_origin(annotation) is Literal:
        choices = get_args(annotation)
        return any(choice is not None for choice in choices) and all(
            choice is None or type(choice) in (str, int, float, bool) or isinstance(choice, Enum) for choice in choices
        )
    if get_origin(annotation) in (Union, UnionType):
        return all(choice is type(None) or ordinary(choice) for choice in get_args(annotation))
    return isinstance(annotation, type) and issubclass(annotation, Enum)


def annotations_for(handler: Callable[..., Any]) -> dict[str, Any]:
    owner = getattr(handler, "__self__", None)
    namespace: dict[str, Any] = {}
    if owner is not None:
        for cls in reversed(type(owner).__mro__):
            namespace.update(vars(cls))
            namespace[cls.__name__] = cls
    return get_type_hints(handler, localns=namespace or None, include_extras=True)


def _adapter(annotation: Any) -> TypeAdapter[Any]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return TypeAdapter(annotation)
    return TypeAdapter(annotation, config=ConfigDict(arbitrary_types_allowed=True))


def _validate(annotation: Any, value: Any, *, strict: bool = False) -> Any:
    return _adapter(annotation).validate_python(value, strict=strict)


class _ScalarTokenError(ValueError):
    """The token has no type-exact member in a declared Literal."""


def _literal_choices(annotation: Any) -> tuple[Any, ...] | None:
    requested = representation(annotation)
    if get_origin(requested) is Literal:
        return get_args(requested)
    if get_origin(requested) in (Union, UnionType):
        members = [_literal_choices(choice) for choice in get_args(requested) if choice is not type(None)]
        if all(member is not None for member in members):
            return tuple(value for member in members if member is not None for value in member)
    return None


def _validated_literal(annotation: Any, value: Any) -> Any:
    validated = _validate(annotation, value)
    # Pydantic Literal matches by equality and can return True for a supplied 1.
    # The candidate was already matched by type; keep that distinction.
    return value if type(value) is not type(validated) and value == validated else validated


def _argument_value(annotation: Any, raw: str) -> Any:
    """String literal matches win, followed by int, float, bool and enum members.

    Membership compares both value and type, so bool/int equality cannot select
    a different literal. Annotation validators still run on the selected value.
    """
    choices = _literal_choices(annotation)
    if choices is None:
        return _validate(annotation, raw)
    if any(type(choice) is str and choice == raw for choice in choices):
        return _validated_literal(annotation, raw)
    kinds = dict.fromkeys((int, float, bool, *(type(choice) for choice in choices if isinstance(choice, Enum))))
    for kind in kinds:
        if not any(type(choice) is kind for choice in choices):
            continue
        try:
            value = _validate(kind, raw)
        except ValidationError:
            continue
        if any(type(value) is type(choice) and value == choice for choice in choices):
            return _validated_literal(annotation, value)
    raise _ScalarTokenError


def _default_value(annotation: Any, default: Any, name: str) -> Any:
    choices = _literal_choices(annotation)
    if (
        choices is not None
        and default is not None
        and not any(type(default) is type(choice) and default == choice for choice in choices)
    ):
        raise ConfigurationError(f"Input '{name}' has an invalid default")
    try:
        return _validated_literal(annotation, default) if choices is not None else _validate(annotation, default)
    except ValidationError:
        raise ConfigurationError(f"Input '{name}' has an invalid default") from None


def _targets(event: TelegramObject, reply: bool, selected: Message | None = None) -> tuple[Message, ...]:
    # A callback's card is never automatically interpreted as input content.
    origin = selected or (event if isinstance(event, Message) else None)
    if origin is None:
        return ()
    if reply and isinstance(origin.reply_to_message, Message):
        return origin, origin.reply_to_message
    return (origin,)


class _LimitedBuffer(io.BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    def write(self, data: Any) -> int:
        if self.tell() + len(data) > self.limit:
            raise InputError("attachment-too-large")
        return super().write(data)


async def _download(
    media: Downloadable,
    ctx: Context,
    limit: int,
    resources: ExitStack,
    downloads: dict[str, io.BytesIO],
) -> io.BytesIO:
    if (media.file_size or 0) > limit:
        raise InputError("attachment-too-large")
    if media.file_id in downloads:
        cached = downloads[media.file_id]
        if cached.getbuffer().nbytes > limit:
            raise InputError("attachment-too-large")
        cached.seek(0)
        return cached
    stream = _LimitedBuffer(limit)
    resources.callback(stream.close)
    await ctx.bot.download(media.file_id, destination=stream, timeout=30)
    stream.seek(0)
    downloads[media.file_id] = stream
    return stream


def _check_text(text: str, declaration: TextInput) -> str:
    if declaration.max_chars is not None and len(text) > declaration.max_chars:
        raise InputError("text-too-long", limit=declaration.max_chars)
    if len(text.encode("utf-8")) > declaration.max_bytes:
        raise InputError("text-too-large")
    return text


async def _text(
    event: TelegramObject,
    ctx: Context,
    declaration: TextInput,
    tail: str | None,
    resources: ExitStack,
    downloads: dict[str, io.BytesIO],
    selected: Message | None,
) -> tuple[Message | None, str]:
    targets = _targets(event, declaration.reply, selected)
    if tail and isinstance(event, Message) and declaration.document != "prefer":
        return event, _check_text(tail, declaration)

    async def document_text(target: Message) -> str | None:
        candidates = [target.document, *rich_media(target)]
        for item in candidates:
            if isinstance(item, Document) and (item.mime_type or "").startswith("text/"):
                stream = await _download(item, ctx, declaration.max_bytes, resources, downloads)
                try:
                    text = stream.getvalue().decode("utf-8")
                except UnicodeDecodeError:
                    raise InputError("text-encoding") from None
                return _check_text(text, declaration)
        return None

    for index, target in enumerate(targets):
        if declaration.document == "prefer" and (text := await document_text(target)) is not None:
            return target, text
        # For a selected command, None means a non-command event; an empty tail
        # must not fall back to the literal command token itself.
        text = target.text or target.caption or rich_text(target)
        if index == 0 and target is event and tail is not None:
            text = tail
        if text:
            return target, _check_text(text, declaration)
        if declaration.document is True and (text := await document_text(target)) is not None:
            return target, text
    return None, ""


def _pick_media(message: Message, kinds: tuple[str, ...]) -> Downloadable | None:
    candidates: list[Downloadable] = []
    if message.video or message.animation or message.video_note:
        candidates.append(message.video or message.animation or message.video_note)  # type: ignore[arg-type]
    if message.photo:
        candidates.append(message.photo[-1])
    if message.sticker:
        candidates.append(message.sticker)
    if message.document:
        candidates.append(message.document)
    if message.audio or message.voice:
        candidates.append(message.audio or message.voice)  # type: ignore[arg-type]
    candidates.extend(rich_media(message))
    for kind in kinds:
        for media in candidates:
            if isinstance(media, (Video, Animation, VideoNote)) and kind == "video":
                return media
            if isinstance(media, PhotoSize) and kind == "image":
                return media
            if isinstance(media, Sticker) and (
                media.is_video and kind == "video" or not (media.is_video or media.is_animated) and kind == "image"
            ):
                return media
            if isinstance(media, Document):
                mime = media.mime_type or ""
                base, _, subtype = mime.partition("/")
                if (
                    kind == "document"
                    or kind == "image"
                    and base == "image"
                    and subtype.endswith(("jpeg", "png", "tiff", "bmp", "gif", "webp"))
                    or kind == "video"
                    and mime.startswith("video/")
                ):
                    return media
            if isinstance(media, (Audio, Voice)) and kind == "audio":
                return media
    return None


async def _select_media(
    event: TelegramObject, ctx: Context, declaration: MediaDeclaration, selected: Message | None
) -> tuple[Message | None, Downloadable | None]:
    kinds = (
        declaration.kinds
        if isinstance(declaration, MediaInput)
        else ("image",)
        if isinstance(declaration, ImageInput)
        else ("video",)
        if isinstance(declaration, VideoInput)
        else ("document",)
    )
    targets = _targets(event, declaration.reply, selected)
    for target in targets:
        if media := _pick_media(target, kinds):
            return target, media
    if isinstance(declaration, (ImageInput, MediaInput)) and declaration.avatar:
        for target in reversed(targets):
            users = []
            if isinstance(target.forward_origin, MessageOriginUser):
                users.append(target.forward_origin.sender_user)
            if target.from_user is not None:
                users.append(target.from_user)
            for user in users:
                photos = await ctx.bot.get_user_profile_photos(user.id, limit=1)
                if photos.photos and photos.photos[0]:
                    return target, photos.photos[0][-1]
    return None, None


async def _decode(payload: bytes, declaration: MediaDeclaration, ctx: Context, resources: ExitStack) -> Any:
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        raise ConfigurationError("Image decoding requires the teleforge[media] extra.") from None
    max_pixels = declaration.max_pixels if isinstance(declaration, ImageInput) else 16_000_000
    max_dimension = declaration.max_dimension if isinstance(declaration, ImageInput) else 8192

    def load() -> Image.Image:
        with io.BytesIO(payload) as source:
            image = Image.open(source)
            try:
                width, height = image.size
                if width * height > max_pixels or max(width, height) > max_dimension:
                    raise InputError("image-dimensions")
                image.load()
            except BaseException:
                image.close()
                raise
            return image

    async def run() -> Image.Image:
        task = asyncio.create_task(asyncio.to_thread(load))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A worker owns its immutable bytes; join it before returning a live
            # image to nowhere. Repeated cancellation must not skip cleanup.
            while not task.cancelled():
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:  # noqa: BLE001 - preserve cancellation after joining the owned worker
                    break
                else:
                    result.close()
                    break
            raise

    try:
        semaphore = ctx.data.get("_teleforge_media_slots")
        if isinstance(semaphore, asyncio.Semaphore):
            async with semaphore:
                image = await run()
        else:
            image = await run()
    except UnidentifiedImageError, OSError, Image.DecompressionBombError:
        raise InputError("image-decode") from None
    resources.callback(image.close)
    return image


async def _media_value(
    media: Downloadable,
    annotation: Any,
    declaration: MediaDeclaration,
    ctx: Context,
    resources: ExitStack,
    downloads: dict[str, io.BytesIO],
) -> Any:
    if (media.file_size or 0) > declaration.max_bytes:
        raise InputError("attachment-too-large")
    requested = representation(annotation)
    is_image = getattr(requested, "__module__", "") == "PIL.Image" and getattr(requested, "__name__", "") == "Image"
    if requested not in (bytes, io.BytesIO, Path) and not is_image:
        return _validate(annotation, media)
    stream = await _download(media, ctx, declaration.max_bytes, resources, downloads)
    if requested is bytes:
        return stream.getvalue()
    if requested is io.BytesIO:
        return stream
    if requested is Path:
        directory = Path(resources.enter_context(TemporaryDirectory(prefix="teleforge-input-")))
        suffix = Path(getattr(media, "file_name", None) or "input").suffix
        suffix = suffix if len(suffix) <= 16 and suffix.removeprefix(".").isalnum() else ""
        path = directory / ("input" + suffix)
        path.write_bytes(stream.getvalue())
        return path
    return await _decode(stream.getvalue(), declaration, ctx, resources)


@asynccontextmanager
async def prepare_arguments(
    handler: Callable[..., Any],
    event: TelegramObject,
    ctx: Context,
    data: Mapping[str, Any],
    declarations: Mapping[str, Declaration],
    *,
    tail: str | None = None,
    payload: BaseModel | Mapping[str, Any] | None = None,
    plan: ParameterPlan | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Acquire the compiled values and own their resources through delivery.

    Direct callers may omit the plan; entrypoint adapters always pass their
    compiled plan. Middleware values never change a parameter's selected source.
    """
    from .declarations import Declaration as HandlerDeclaration
    from .parameters import checked_dependency, compile_parameters

    payload_values = (
        {name: getattr(payload, name) for name in type(payload).model_fields}
        if isinstance(payload, BaseModel)
        else dict(payload or {})
    )
    if plan is None:
        hints = annotations_for(handler)
        kind: Literal["command", "callback", "event"] = (
            "command" if tail is not None else "callback" if payload is not None else "event"
        )
        plan, issues = compile_parameters(
            handler,
            HandlerDeclaration(kind=kind, inputs=declarations),
            annotations=hints,
            payload_fields={name: hints.get(name, Any) for name in payload_values},
        )
        if issues:
            raise ConfigurationError("; ".join(issue.message for issue in issues))
    # Native event/filter data may contain a CommandObject without making this
    # event declaration a command. Only the compiled command plan consumes it.
    command_tail = (tail or "") if plan.command else None
    tokens = list(re.finditer(r"\S+", command_tail or ""))
    position = consumed = 0
    contiguous = True
    values: dict[str, Any] = {}
    text_parameters: list[tuple[str, Any, Any, TextInput]] = []
    media_parameters: list[tuple[str, Any, Any, MediaDeclaration]] = []
    source_text: Message | None = None
    source_media: Message | None = None
    with _owned_inputs() as resources:
        downloads: dict[str, io.BytesIO] = {}
        for parameter in plan.parameters:
            name, annotation, default = parameter.name, parameter.annotation, parameter.default
            declaration = parameter.declaration
            source = parameter.source
            if source == "text":
                assert isinstance(declaration, TextInput)
                text_parameters.append((name, annotation, default, declaration))
            elif source == "media":
                assert isinstance(declaration, _MEDIA)
                media_parameters.append((name, annotation, default, declaration))
            elif source == "context":
                values[name] = checked_dependency(annotation, ctx, name)
            elif source == "event":
                values[name] = checked_dependency(annotation, event, name)
            elif source == "bot":
                values[name] = checked_dependency(annotation, ctx.bot, name)
            elif source == "callback_payload":
                if name not in payload_values:
                    raise InputError("callback-invalid")
                if isinstance(payload, BaseModel | _ValidatedPayload):
                    # CallbackData and managed codecs have already run validators;
                    # reading their exact values must not transform them a second time.
                    values[name] = payload_values[name]
                else:
                    try:
                        values[name] = _validate(annotation, payload_values[name], strict=True)
                    except ValidationError:
                        raise InputError("callback-invalid") from None
            elif source == "argument":
                rule = declaration if isinstance(declaration, Argument) else Argument()
                raw = tokens[position].group() if position < len(tokens) else _MISSING
                position += 1
                parsed: Any = _MISSING
                if isinstance(raw, str):
                    try:
                        parsed = _argument_value(annotation, raw)
                    except ValidationError, _ScalarTokenError:
                        if rule.strict:
                            raise InputError("argument-invalid", parameter=name) from None
                if parsed is _MISSING:
                    contiguous = False
                    if default is inspect.Parameter.empty:
                        raise InputError("argument-missing", parameter=name)
                    parsed = _default_value(annotation, default, name)
                elif contiguous:
                    consumed = tokens[position - 1].end()
                if rule.clamp is not None:
                    if type(parsed) not in (int, float):
                        raise ConfigurationError(f"Argument '{name}' requires numeric values for its clamp")
                    parsed = max(rule.clamp[0], min(rule.clamp[1], parsed))
                    try:
                        parsed = _validate(annotation, parsed)
                    except ValidationError:
                        raise ConfigurationError(f"Argument '{name}' has an incompatible clamp") from None
                values[name] = parsed
            else:
                if name in data:
                    value = data[name]
                elif default is not inspect.Parameter.empty:
                    value = default
                else:
                    raise ConfigurationError(f"Missing injected dependency '{name}' for {handler.__qualname__}")
                values[name] = checked_dependency(annotation, value, name)

        remaining = (
            (command_tail[consumed:].lstrip() if consumed else command_tail) if command_tail is not None else None
        )
        if plan.command and "_teleforge_text" in data:
            # Custom grammars can keep argument tokens separate from body text.
            remaining = data["_teleforge_text"]
        for name, annotation, default, declaration_text in text_parameters:
            source_message, value = await _text(
                event, ctx, declaration_text, remaining, resources, downloads, ctx.input_sources.get(name)
            )
            if not value:
                if default is inspect.Parameter.empty:
                    raise InputError("text-missing", parameter=name)
                values[name] = _default_value(annotation, default, name)
            else:
                try:
                    values[name] = _validate(annotation, value)
                except ValidationError:
                    raise InputError("text-invalid", parameter=name) from None
            if source_message is not None:
                ctx.input_sources[name] = source_message
                source_text = source_message
        for name, annotation, default, declaration_media in media_parameters:
            source_message, media = await _select_media(event, ctx, declaration_media, ctx.input_sources.get(name))
            if media is None:
                if default is inspect.Parameter.empty:
                    raise InputError("media-missing", parameter=name)
                value_media = _default_value(annotation, default, name)
            else:
                try:
                    value_media = await _media_value(media, annotation, declaration_media, ctx, resources, downloads)
                except ValidationError:
                    raise InputError("media-type", parameter=name) from None
            values[name] = value_media
            if source_message is not None:
                ctx.input_sources[name] = source_message
                source_media = source_message
        if isinstance(event, Message) and (selected_source := source_media or source_text) is not None:
            ctx.response_target = selected_source
        yield values
