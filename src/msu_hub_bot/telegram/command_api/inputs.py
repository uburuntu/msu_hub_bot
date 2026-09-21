"""Explicit acquisition rules for typed command parameters."""

from dataclasses import dataclass
from typing import Literal

from msu_hub_bot.media.limits import MAX_DOWNLOAD_BYTES


@dataclass(frozen=True, slots=True)
class Argument:
    """Parse a positional token; strict rejects invalid tokens instead of using a default."""

    strict: bool = False
    clamp: tuple[int | float, int | float] | None = None

    def __post_init__(self) -> None:
        if self.clamp is not None and self.clamp[0] > self.clamp[1]:
            raise ValueError("Argument clamp bounds are reversed")


@dataclass(frozen=True, slots=True)
class TextInput:
    """Use the remaining invocation text, then an allowed reply or text document."""

    reply: bool = True
    document: bool = False
    max_chars: int | None = None
    max_bytes: int = MAX_DOWNLOAD_BYTES

    def __post_init__(self) -> None:
        if self.max_chars is not None and self.max_chars <= 0:
            raise ValueError("max_chars must be positive")
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")


@dataclass(frozen=True, slots=True)
class ImageInput:
    reply: bool = True
    avatar: bool = False
    max_bytes: int = MAX_DOWNLOAD_BYTES

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")


@dataclass(frozen=True, slots=True)
class VideoInput:
    reply: bool = True
    max_bytes: int = MAX_DOWNLOAD_BYTES

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")


@dataclass(frozen=True, slots=True)
class DocumentInput:
    reply: bool = True
    max_bytes: int = MAX_DOWNLOAD_BYTES

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")


@dataclass(frozen=True, slots=True)
class MediaInput:
    """Prefer allowed attached media over reply media; profile photos are an opt-in fallback."""

    reply: bool = True
    avatar: bool = False
    max_bytes: int = MAX_DOWNLOAD_BYTES
    kinds: tuple[Literal["image", "video", "document", "audio"], ...] = ("image", "video", "document", "audio")

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if not self.kinds or any(kind not in {"image", "video", "document", "audio"} for kind in self.kinds):
            raise ValueError("MediaInput kinds must select image, video, document or audio")
        if self.avatar and "image" not in self.kinds:
            raise ValueError("Avatar fallback requires the image kind")


type MediaDeclaration = ImageInput | VideoInput | DocumentInput | MediaInput
type InputDeclaration = TextInput | MediaDeclaration
type Declaration = Argument | InputDeclaration


class InputError(ValueError):
    """Safe, brief guidance for a selected command's missing or unusable input."""
