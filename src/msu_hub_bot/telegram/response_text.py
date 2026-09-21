"""Bounded literal text and native entities for command responses."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from aiogram.enums import MessageEntityType
from aiogram.types import (
    MessageEntity,
    RichTextBold,
    RichTextCode,
    RichTextCustomEmoji,
    RichTextItalic,
    RichTextSpoiler,
    RichTextStrikethrough,
    RichTextTextMention,
    RichTextUnderline,
    RichTextUnion,
    RichTextUrl,
)
from aiogram.utils.formatting import Text


class ResponseError(ValueError):
    """A locally rejected response, with safe guidance for its author."""


class ResponseLimitError(ResponseError):
    """The complete response cannot fit its configured work or size budget."""


class TextNeedsFile(ResponseError):
    """Splitting would change an indivisible entity or produce empty messages."""


MAX_ENTITIES = 10_000
MESSAGE_ENTITIES = 100
FIELD_BYTES = 32768
_SPLITTABLE = frozenset(
    {
        "bold",
        "italic",
        "underline",
        "strikethrough",
        "spoiler",
        "code",
        "pre",
        "blockquote",
        "expandable_blockquote",
        "text_link",
        "text_mention",
    }
)
_RICH_ENTITIES = frozenset(
    {"bold", "italic", "underline", "strikethrough", "spoiler", "code", "text_link", "text_mention", "custom_emoji", "url"}
)


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True, slots=True)
class FormattedText:
    text: str
    entities: tuple[MessageEntity, ...] = ()

    @property
    def size_bytes(self) -> int:
        return len(self.text.encode()) + sum(len(entity.model_dump_json().encode()) for entity in self.entities)

    def fits(self, limit: int, *, max_bytes: int = FIELD_BYTES, max_entities: int = MESSAGE_ENTITIES) -> bool:
        return units(self.text) <= limit and len(self.text.encode()) <= max_bytes and len(self.entities) <= max_entities

    def file_bytes(self) -> bytes:
        # Literal text is unchanged; retain destinations that only existed in entities.
        links = dict.fromkeys(entity.url for entity in self.entities if entity.type == "text_link" and entity.url)
        suffix = "\n\nСсылки:\n" + "\n".join(links) if links else ""
        return (self.text + suffix).encode("utf-8")


def format_text(value: str | Text | None, entities: Sequence[MessageEntity] | None, max_bytes: int) -> FormattedText:
    if isinstance(value, Text):
        if entities is not None:
            raise ResponseError("Передайте форматирование через Text или entities, но не одновременно.")
        # Bound nesting/work before calling the upstream recursive renderer.
        pending = [(value, 0)]
        count = length = 0
        while pending:
            node, depth = pending.pop()
            count += 1
            if count > MAX_ENTITIES or depth > 32:
                raise ResponseLimitError("Слишком сложное форматирование. Упростите результат.")
            for child in node:
                count += 1
                if count > MAX_ENTITIES:
                    raise ResponseLimitError("Слишком сложное форматирование. Упростите результат.")
                if isinstance(child, Text):
                    pending.append((child, depth + 1))
                else:
                    try:
                        length += len(str(child).encode())
                    except UnicodeError:
                        raise ResponseError("В ответе есть некорректный Unicode.") from None
                    if length > max_bytes:
                        raise ResponseLimitError("Результат слишком большой. Уменьшите объём запроса.")
        try:
            text, rendered = value.render()
        except ValueError:
            raise ResponseError("Некорректное форматирование ответа.") from None
        entities = rendered
    elif value is None or isinstance(value, str):
        text = value or ""
    else:
        raise ResponseError("Текст ответа должен быть строкой или aiogram Text.")
    if len(text) > max_bytes:
        raise ResponseLimitError("Результат слишком большой. Уменьшите объём запроса.")
    try:
        encoded = text.encode("utf-8")
        size = units(text)
    except UnicodeError:
        raise ResponseError("В ответе есть некорректный Unicode.") from None
    if len(encoded) > max_bytes:
        raise ResponseLimitError("Результат слишком большой. Уменьшите объём запроса.")
    if entities is not None and len(entities) > MAX_ENTITIES:
        raise ResponseLimitError("Слишком много форматирования. Упростите результат.")
    # Telegram objects are frozen; avoid copying a user's bound Bot/session.
    copied = tuple(sorted((entity.model_copy() for entity in entities or ()), key=lambda entity: (entity.offset, -entity.length)))
    wanted: set[int] = set()
    stack: list[int] = []
    metadata_bytes = 0
    for entity in copied:
        end = entity.offset + entity.length
        if entity.type not in MessageEntityType or entity.offset < 0 or entity.length <= 0 or end > size:
            raise ResponseError("Некорректные границы форматирования ответа.")
        if (
            (entity.type == "text_link" and not entity.url)
            or (entity.type == "text_mention" and entity.user is None)
            or (entity.type == "custom_emoji" and not entity.custom_emoji_id)
        ):
            raise ResponseError("В форматировании ответа не хватает данных.")
        while stack and entity.offset >= stack[-1]:
            stack.pop()
        if stack and end > stack[-1]:
            raise ResponseError("Границы форматирования ответа пересекаются.")
        stack.append(end)
        if len(stack) > 32:
            raise ResponseLimitError("Слишком сложное форматирование. Упростите результат.")
        wanted.update((entity.offset, end))
        metadata_bytes += len(entity.model_dump_json().encode())
        if len(encoded) + metadata_bytes > max_bytes:
            raise ResponseLimitError("Слишком много форматирования. Упростите результат.")
    position = 0
    wanted.discard(0)
    if wanted:
        for char in text:
            position += 2 if ord(char) > 0xFFFF else 1
            wanted.discard(position)
            if not wanted:
                break
    if wanted:
        raise ResponseError("Форматирование разрывает символ Unicode.")
    return FormattedText(text, copied)


def split_text(
    value: FormattedText, limit: int = 4096, *, max_bytes: int = FIELD_BYTES, max_entities: int = MESSAGE_ENTITIES
) -> Iterator[FormattedText]:
    """Preserve every character and rebase UTF-16 entities at codepoint boundaries."""
    start = start_units = 0
    text = value.text
    while start < len(text):
        end = start
        width = byte_size = 0
        word: tuple[int, int] | None = None
        while end < len(text):
            char = text[end]
            char_units = 2 if ord(char) > 0xFFFF else 1
            char_bytes = len(char.encode())
            if width + char_units > limit or byte_size + char_bytes > max_bytes:
                break
            width += char_units
            byte_size += char_bytes
            end += 1
            if char.isspace():
                word = end, width
        if end < len(text) and word is not None and word[1] >= width // 2:
            end, width = word
        boundary = start_units + width
        # Recheck after each reduction: a count limit can land inside a URL,
        # and a nested entity can move the boundary into its atomic parent.
        while True:
            previous = boundary
            for entity in value.entities:
                if entity.type not in _SPLITTABLE and entity.offset < boundary < entity.offset + entity.length:
                    boundary = entity.offset
            overlapping = [entity for entity in value.entities if entity.offset < boundary and entity.offset + entity.length > start_units]
            if len(overlapping) > max_entities:
                boundary = min(boundary, overlapping[max_entities].offset)
            if boundary == previous:
                break
        if boundary <= start_units:
            raise TextNeedsFile("Фрагмент форматирования не помещается в одно сообщение.")
        # A revised entity boundary is necessarily a validated UTF-16 boundary.
        if boundary != start_units + width:
            end = start
            width = 0
            while start_units + width < boundary:
                width += 2 if ord(text[end]) > 0xFFFF else 1
                end += 1
        part = text[start:end]
        if not part.strip():
            raise TextNeedsFile("Пустые фрагменты можно сохранить только в файле.")
        adjusted = tuple(
            entity.model_copy(
                update={
                    "offset": max(entity.offset, start_units) - start_units,
                    "length": min(entity.offset + entity.length, boundary) - max(entity.offset, start_units),
                }
            )
            for entity in value.entities
            if entity.offset < boundary and entity.offset + entity.length > start_units
        )
        yield FormattedText(part, adjusted)
        start, start_units = end, boundary


def rich_text(value: FormattedText) -> RichTextUnion | None:
    """Translate only entities whose meaning has an exact supported Rich equivalent."""
    if not can_rich(value):
        return None
    encoded = value.text.encode("utf-16-le")
    entities = value.entities
    index = 0

    def literal(start: int, end: int) -> str:
        return encoded[start * 2 : end * 2].decode("utf-16-le")

    def build(start: int, end: int) -> list[RichTextUnion]:
        nonlocal index
        result: list[RichTextUnion] = []
        cursor = start
        while index < len(entities) and entities[index].offset < end:
            entity = entities[index]
            if entity.offset > cursor:
                result.append(literal(cursor, entity.offset))
            index += 1
            stop = entity.offset + entity.length
            content: RichTextUnion = build(entity.offset, stop)
            match entity.type:
                case "bold":
                    content = RichTextBold(text=content)
                case "italic":
                    content = RichTextItalic(text=content)
                case "underline":
                    content = RichTextUnderline(text=content)
                case "strikethrough":
                    content = RichTextStrikethrough(text=content)
                case "spoiler":
                    content = RichTextSpoiler(text=content)
                case "code":
                    content = RichTextCode(text=content)
                case "text_link":
                    content = RichTextUrl(text=content, url=entity.url or "")
                case "url":
                    content = RichTextUrl(text=content, url=literal(entity.offset, stop))
                case "text_mention":
                    assert entity.user is not None
                    content = RichTextTextMention(text=content, user=entity.user)
                case "custom_emoji":
                    content = RichTextCustomEmoji(
                        custom_emoji_id=entity.custom_emoji_id or "", alternative_text=literal(entity.offset, stop)
                    )
            result.append(content)
            cursor = stop
        if cursor < end:
            result.append(literal(cursor, end))
        return result

    return build(0, len(encoded) // 2)


def can_rich(value: FormattedText) -> bool:
    for index, entity in enumerate(value.entities):
        if entity.type not in _RICH_ENTITIES:
            return False
        if (
            entity.type == "custom_emoji"
            and index + 1 < len(value.entities)
            and value.entities[index + 1].offset < entity.offset + entity.length
        ):
            # Rich custom emoji only accept literal alternative text; preserve
            # any formatting nested inside that range through native entities.
            return False
    return True
