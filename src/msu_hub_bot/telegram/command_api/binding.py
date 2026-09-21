"""Bind typed commands inside aiogram's normal handler lifecycle."""

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints
from types import UnionType

from PIL import Image
from aiogram.dispatcher.event.handler import CallableObject
from aiogram.dispatcher.event.telegram import TelegramEventObserver
from aiogram.dispatcher.flags import extract_flags_from_object
from aiogram.types import InputFile, Message
from aiogram.utils.formatting import Text
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from msu_hub_bot.media.limits import MAX_DOWNLOAD_BYTES
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.filters import MetaCommand as CommandFilter
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telegram.responses import MediaSource, ResponseError, ResponsePolicy, send_response

from .acquisition import acquire_media, acquire_text, check_text, representation
from .inputs import Argument, Declaration, DocumentInput, ImageInput, InputError, MediaInput, TextInput, VideoInput

_MISSING = object()
_ATTRIBUTE = "__meta_command__"
_MEDIA = (ImageInput, VideoInput, DocumentInput, MediaInput)


@dataclass(frozen=True, slots=True)
class _Parameter:
    name: str
    annotation: object
    default: object
    declaration: Declaration | None
    adapter: TypeAdapter[object] | None


@dataclass(frozen=True, slots=True)
class _Command:
    keywords: tuple[str, ...]
    parameters: tuple[_Parameter, ...]
    policy: ResponsePolicy
    resolver: CallableObject | None
    context_messages: int
    guidance: str | None

    def usage(self) -> str:
        if self.guidance:
            return self.guidance
        parts = [f"Использование: /{self.keywords[0]}"]
        for parameter in self.parameters:
            if parameter.declaration is not None:
                name = parameter.name
                parts.append(f"<{name}>" if parameter.default is _MISSING else f"[{name}]")
        return " ".join(parts)


def _ordinary(annotation: object) -> bool:
    if annotation in (str, int, float, bool):
        return True
    origin = get_origin(annotation)
    if origin is Annotated:
        return _ordinary(get_args(annotation)[0])
    if origin is Literal:
        return True
    if origin in (Union, UnionType):
        return all(arg is type(None) or _ordinary(arg) for arg in get_args(annotation))
    return isinstance(annotation, type) and issubclass(annotation, Enum)


def _adapter(annotation: Any) -> TypeAdapter[object]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return TypeAdapter(annotation)
    return TypeAdapter(annotation, config=ConfigDict(arbitrary_types_allowed=True))


class MetaCommand:
    """Attach a command declaration while retaining the original typed callable.

    Register with register_command, or use invoke_command for internal dispatch.
    Services and MetaInfo are injected by name; ordinary scalar parameters consume tokens.
    """

    def __init__(
        self,
        *keywords: str,
        resolve: Callable[..., object] | None = None,
        context_messages: int = 0,
        rich: bool = True,
        soft_messages: int = 3,
        max_output_bytes: int = MAX_DOWNLOAD_BYTES,
        output: Literal["photo", "video", "document", "audio"] | None = None,
        guidance: str | None = None,
        **declarations: Declaration,
    ) -> None:
        if not keywords or any(not keyword or keyword.startswith(("/", "#")) for keyword in keywords):
            raise ValueError("MetaCommand requires bare command aliases")
        if not 0 <= context_messages <= 20:
            raise ValueError("context_messages must be between 0 and 20")
        if soft_messages <= 0 or max_output_bytes <= 0:
            raise ValueError("Output limits must be positive")
        self.keywords = keywords
        self.declarations = declarations
        self.resolver = CallableObject(resolve) if resolve is not None else None
        self.context_messages = context_messages
        self.guidance = guidance
        self.policy = ResponsePolicy(rich=rich, soft_messages=soft_messages, max_output_bytes=max_output_bytes, output=output)

    def __call__[**P, R](self, function: Callable[P, R]) -> Callable[P, R]:
        if not bool(inspect.iscoroutinefunction(function)):
            raise TypeError("MetaCommand handlers must be async; offload blocking work through the injected executor")
        if hasattr(function, _ATTRIBUTE):
            raise ValueError("Use one MetaCommand with all aliases")
        signature = inspect.signature(function)
        annotations = get_type_hints(function, include_extras=True)
        unknown = set(self.declarations) - signature.parameters.keys()
        if unknown:
            raise ValueError(f"Declarations have no matching parameters: {', '.join(sorted(unknown))}")
        parameters = []
        text_count = media_count = 0
        for name, parameter in signature.parameters.items():
            if parameter.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
                raise TypeError("Command parameters must accept named arguments")
            annotation = annotations.get(name, parameter.annotation)
            if annotation is inspect.Parameter.empty:
                raise TypeError(f"Command parameter {name} requires a type annotation")
            declaration = self.declarations.get(name)
            if declaration is None and name not in ("meta", "message") and _ordinary(annotation):
                declaration = Argument()
            if declaration is not None and not isinstance(declaration, (Argument, TextInput, *_MEDIA)):
                raise TypeError(f"Invalid declaration for {name}")
            if isinstance(declaration, TextInput):
                text_count += 1
                if representation(annotation) is not str:
                    raise TypeError("TextInput requires str or str | None")
            elif isinstance(declaration, _MEDIA):
                media_count += 1
            if isinstance(declaration, Argument) and declaration.clamp is not None and representation(annotation) not in (int, float):
                raise TypeError("Argument clamping requires int or float")
            adapter = _adapter(annotation) if declaration is not None else None
            default = _MISSING if parameter.default is inspect.Parameter.empty else parameter.default
            if adapter is not None and default is not _MISSING:
                try:
                    default = adapter.validate_python(default)
                except ValidationError:
                    raise TypeError(f"Invalid default for command parameter {name}") from None
            parameters.append(_Parameter(name, annotation, default, declaration, adapter))
        if text_count > 1 or media_count > 1:
            raise TypeError("One text input and one media input per command keep extraction and reply selection unambiguous")
        setattr(
            function,
            _ATTRIBUTE,
            _Command(self.keywords, tuple(parameters), self.policy, self.resolver, self.context_messages, self.guidance),
        )
        return function


def _command(function: Callable[..., object]) -> _Command:
    command = getattr(function, _ATTRIBUTE, None)
    if not isinstance(command, _Command):
        raise TypeError("Function must be decorated with MetaCommand")
    return command


def register_command(
    observer: TelegramEventObserver,
    function: Callable[..., object],
    *filters: Callable[..., Any],
    flags: dict[str, Any] | None = None,
) -> Callable[..., Awaitable[object]]:
    """Register an event-first adapter in the caller's existing ordered router slot."""
    command = _command(function)
    selected_filters = filters
    if not any(isinstance(item, CommandFilter) for item in filters):
        count = sum(isinstance(parameter.declaration, Argument) for parameter in command.parameters)
        if not any(isinstance(parameter.declaration, TextInput) for parameter in command.parameters):
            count = 0
        selected_filters = (CommandFilter(*command.keywords, args=count or None), *filters)

    async def adapter(message: Message, **data: Any) -> object:
        return await invoke_command(function, message, **data)

    # aiogram unwraps callbacks before inspecting them: do not set __wrapped__.
    adapter.__name__ = function.__name__
    adapter.__qualname__ = function.__qualname__
    adapter.__module__ = function.__module__
    adapter.__doc__ = function.__doc__
    handler_flags = {"handler_key": function.__qualname__, **extract_flags_from_object(function), **(flags or {})}
    observer.register(adapter, *selected_filters, flags=handler_flags)
    return adapter


def _validate(parameter: _Parameter, value: object) -> object:
    if parameter.adapter is None:
        return value
    try:
        result = parameter.adapter.validate_python(value)
    except ValidationError:
        return _MISSING
    if isinstance(parameter.declaration, Argument) and parameter.declaration.clamp is not None:
        low, high = parameter.declaration.clamp
        if isinstance(result, (int, float)):
            result = max(low, min(high, result))
            if representation(parameter.annotation) is int:
                result = int(result)
    return result


def _parse(command: _Command, meta: MetaInfo) -> dict[str, object]:
    values: dict[str, object] = {}
    scalar = [parameter for parameter in command.parameters if isinstance(parameter.declaration, Argument)]
    consumed = 0
    complete_prefix = True
    for index, parameter in enumerate(scalar):
        raw: object = meta.arguments[index] if index < len(meta.arguments) else _MISSING
        value = _validate(parameter, raw) if raw is not _MISSING else _MISSING
        if value is _MISSING:
            complete_prefix = False
            if raw is not _MISSING and isinstance(parameter.declaration, Argument) and parameter.declaration.strict:
                raise InputError(command.usage())
        else:
            values[parameter.name] = value
            if complete_prefix:
                consumed += 1
    if not meta.hashtag:
        raw_text = meta.raw_text or ""
        tokens = list(re.finditer(r"\S+", raw_text))
        # Internal callers may supply arguments separately from their request text.
        if all(index < len(tokens) and tokens[index].group() == meta.arguments[index] for index in range(consumed)):
            meta.text = raw_text[tokens[consumed - 1].end() :].lstrip() if consumed else raw_text
    return values


def _finish(parameter: _Parameter, values: dict[str, object], command: _Command) -> None:
    value = values.get(parameter.name, _MISSING)
    if value is not _MISSING:
        value = _validate(parameter, value)
        if value is _MISSING and (
            isinstance(parameter.declaration, _MEDIA) or isinstance(parameter.declaration, Argument) and parameter.declaration.strict
        ):
            raise InputError(command.usage())
    if value is _MISSING:
        if parameter.default is _MISSING:
            raise InputError(command.usage())
        value = parameter.default
    if isinstance(parameter.declaration, TextInput) and isinstance(value, str):
        check_text(value, parameter.declaration)
    values[parameter.name] = value


def _update_text_cache(command: _Command, meta: MetaInfo, values: Mapping[str, object]) -> None:
    for parameter in command.parameters:
        if isinstance(parameter.declaration, TextInput):
            text = values.get(parameter.name)
            if isinstance(text, str):
                source = meta.input_sources.get(parameter.name, meta.message)
                meta._text_input = (source, text, None)
                meta.input_sources[parameter.name] = source


async def _prepare(command: _Command, meta: MetaInfo, data: Mapping[str, object], resources: ExitStack) -> dict[str, object]:
    values = _parse(command, meta)
    for parameter in command.parameters:
        if isinstance(parameter.declaration, TextInput):
            source, text = await acquire_text(meta, parameter.declaration, resources)
            meta.input_sources[parameter.name] = source
            meta._text_input = (source, text, None)
            if text:
                values[parameter.name] = text
    meta.resolved = values
    _update_text_cache(command, meta, values)
    if command.resolver is not None:
        resolver_values = {
            parameter.name: values.get(parameter.name)
            for parameter in command.parameters
            if isinstance(parameter.declaration, (Argument, TextInput))
        }
        resolved = await command.resolver.call(**{**data, **resolver_values, "meta": meta, "message": meta.message})
        if not isinstance(resolved, Mapping) or any(not isinstance(name, str) for name in resolved):
            raise TypeError("Command resolver must return a mapping of parameter names to values")
        allowed = {parameter.name for parameter in command.parameters if isinstance(parameter.declaration, (Argument, TextInput))}
        if resolved.keys() - allowed:
            raise TypeError("Command resolver returned undeclared argument names")
        values.update(resolved)
    # Required text and scalar validation precedes profile lookups and downloads.
    for parameter in command.parameters:
        if isinstance(parameter.declaration, (Argument, TextInput)):
            _finish(parameter, values, command)
    _update_text_cache(command, meta, values)
    for parameter in command.parameters:
        if isinstance(parameter.declaration, _MEDIA):
            source, value = await acquire_media(meta, parameter.declaration, parameter.annotation, resources)
            meta.input_sources[parameter.name] = source
            if value is not None:
                values[parameter.name] = value
            _finish(parameter, values, command)
        elif parameter.declaration is None:
            if parameter.name == "meta":
                values[parameter.name] = meta
            elif parameter.name == "message":
                values[parameter.name] = meta.message
            elif parameter.name in data:
                values[parameter.name] = data[parameter.name]
            elif parameter.default is not _MISSING:
                values[parameter.name] = parameter.default
            else:
                raise RuntimeError(f"Missing command dependency: {parameter.name}")
    # Media is the transformed object even when a caption came from the invocation.
    ordered = sorted(command.parameters, key=lambda parameter: not isinstance(parameter.declaration, _MEDIA))
    meta._response_target = next(
        (meta.input_sources[p.name] for p in ordered if p.name in meta.input_sources and values.get(p.name) not in (None, "")),
        meta.message,
    )
    meta.resolved = {parameter.name: values[parameter.name] for parameter in command.parameters if parameter.declaration is not None}
    return values


async def _deliver(meta: MetaInfo, result: object, policy: ResponsePolicy) -> object:
    if result is None or isinstance(result, (bool, Message)):
        return result
    if isinstance(result, list) and all(isinstance(item, Message) for item in result):
        return result
    if isinstance(result, (str, Text)):
        return await meta.reply(result)
    import io
    from pathlib import Path

    if isinstance(result, (bytes, io.BytesIO, Path, InputFile, Image.Image)):
        source: MediaSource = result
        match policy.output:
            case "photo":
                return await meta.reply(photo=source)
            case "video":
                return await meta.reply(video=source)
            case "document":
                return await meta.reply(document=source)
            case "audio":
                return await meta.reply(audio=source)
            case _:
                raise TypeError("Returned media requires the command output option")
    raise TypeError("Command result must be text, declared media, sent messages, or None")


async def invoke_command(function: Callable[..., object], message: Message, **data: Any) -> object:
    """Prepare, call and deliver once, including internal command invocations."""
    command = _command(function)
    meta = data.get("meta")
    if not isinstance(meta, MetaInfo):
        selected = await CommandFilter(*command.keywords)(message, bot=data.get("bot") or bot_for(message))
        meta = (
            selected["meta"]
            if isinstance(selected, dict)
            else MetaInfo(message, command=command.keywords[0], text=message.text or message.caption or "")
        )
    meta.context_messages = command.context_messages
    meta._response_policy = command.policy
    with ExitStack() as resources:
        try:
            values = await _prepare(command, meta, data, resources)
            result = await CallableObject(function).call(**values)
            return await _deliver(meta, result, command.policy)
        except (InputError, ResponseError) as error:
            return await send_response(message, str(error), policy=ResponsePolicy(rich=False, soft_messages=1))
