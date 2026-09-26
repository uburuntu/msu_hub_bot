"""Visible rich-message inputs for explicit text and media commands.

Do not use the flattened text for command detection or automatic link previews:
attribution and quoted content are inputs only when someone replies to them.
"""

from collections.abc import Iterator

from aiogram.types import (
    Animation,
    Document,
    Message,
    PhotoSize,
    RichBlockAnimation,
    RichBlockAudio,
    RichBlockBlockQuotation,
    RichBlockCaption,
    RichBlockCollage,
    RichBlockDetails,
    RichBlockDocument,
    RichBlockExpandableBlockQuotation,
    RichBlockFooter,
    RichBlockList,
    RichBlockListItem,
    RichBlockMap,
    RichBlockMathematicalExpression,
    RichBlockParagraph,
    RichBlockPhoto,
    RichBlockPreformatted,
    RichBlockPullQuotation,
    RichBlockSectionHeading,
    RichBlockSlideshow,
    RichBlockTable,
    RichBlockTableCell,
    RichBlockThinking,
    RichBlockUnion,
    RichBlockVideo,
    RichBlockVoiceNote,
    RichMessage,
    RichText,
    RichTextAnchor,
    RichTextButton,
    RichTextCustomEmoji,
    RichTextMathematicalExpression,
    RichTextUnion,
    Video,
)

type RichMedia = PhotoSize | Video | Animation | Document
type _Node = RichBlockUnion | RichTextUnion | RichBlockListItem | RichBlockTableCell | RichBlockCaption

# Defensive processing bounds also cover malformed locally constructed models.
# Text has room for separators added between Telegram's blocks and table cells.
_MAX_NODES = 65536
_MAX_DEPTH = 64
_MAX_TEXT_LENGTH = 65536


def _blocks(blocks: list[RichBlockUnion]) -> Iterator[_Node]:
    for block in blocks:
        yield block
        yield "\n\n"


def _children(node: _Node) -> Iterator[_Node]:
    if isinstance(node, list):
        yield from node
    elif isinstance(node, (RichBlockCollage, RichBlockSlideshow)):
        yield from _blocks(node.blocks)
        if node.caption:
            yield node.caption
    elif isinstance(node, RichBlockBlockQuotation):
        yield from _blocks(node.blocks)
        if node.credit:
            yield node.credit
    elif isinstance(node, RichBlockDetails):
        yield node.summary
        yield "\n\n"
        yield from _blocks(node.blocks)
    elif isinstance(node, RichBlockList):
        for item in node.items:
            yield item
            yield "\n"
    elif isinstance(node, RichBlockListItem):
        if node.label:
            yield node.label
            yield " "
        yield from _blocks(node.blocks)
    elif isinstance(node, RichBlockTable):
        if node.caption:
            yield node.caption
            yield "\n"
        for row in node.cells:
            for index, cell in enumerate(row):
                if index:
                    yield "\t"
                yield cell
            yield "\n"
    elif isinstance(node, RichBlockTableCell):
        if node.text:
            yield node.text
    elif isinstance(node, (RichBlockExpandableBlockQuotation, RichBlockPullQuotation, RichBlockCaption)):
        yield node.text
        if node.credit:
            yield "\n"
            yield node.credit
    elif isinstance(
        node, (RichBlockParagraph, RichBlockSectionHeading, RichBlockPreformatted, RichBlockFooter, RichBlockThinking)
    ):
        yield node.text
    elif isinstance(node, RichBlockMathematicalExpression):
        yield node.expression
    elif isinstance(
        node,
        (
            RichBlockAnimation,
            RichBlockAudio,
            RichBlockDocument,
            RichBlockMap,
            RichBlockPhoto,
            RichBlockVideo,
            RichBlockVoiceNote,
        ),
    ):
        if node.caption:
            yield node.caption
    elif isinstance(node, RichText):
        if isinstance(node, RichTextCustomEmoji):
            yield node.alternative_text
        elif isinstance(node, RichTextMathematicalExpression):
            yield node.expression
        elif isinstance(node, RichTextButton):
            yield node.button.text
        elif not isinstance(node, RichTextAnchor):
            # Formatting, references and links contribute their visible label,
            # never URLs, user IDs or other non-displayed attributes.
            yield node.text


def _walk(message: Message) -> Iterator[_Node]:
    rich = getattr(message, "rich_message", None)
    if not isinstance(rich, RichMessage):
        return
    stack = [iter(_blocks(rich.blocks))]
    visited = 0
    while stack and visited < _MAX_NODES:
        try:
            node = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        visited += 1
        yield node
        if not isinstance(node, str) and len(stack) < _MAX_DEPTH:
            stack.append(iter(_children(node)))


def rich_media(message: Message) -> Iterator[RichMedia]:
    """Yield reusable Telegram media objects in display order, including quotes."""
    for node in _walk(message):
        if isinstance(node, RichBlockPhoto) and node.photo:
            yield node.photo[-1]
        elif isinstance(node, RichBlockVideo):
            yield node.video
        elif isinstance(node, RichBlockAnimation):
            yield node.animation
        elif isinstance(node, RichBlockDocument):
            yield node.document


def rich_text(message: Message) -> str:
    """Flatten visible text without HTML, hidden attributes or recursive calls."""
    parts: list[str] = []
    remaining = _MAX_TEXT_LENGTH
    for node in _walk(message):
        if isinstance(node, str):
            parts.append(node[:remaining])
            remaining -= len(parts[-1])
            if not remaining:
                break
    return "".join(parts).strip()
