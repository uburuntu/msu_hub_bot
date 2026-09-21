"""Declarative typed commands over aiogram's existing ordered routing."""

from msu_hub_bot.telegram.filters import MetaInfo

from .binding import MetaCommand, invoke_command, register_command
from .inputs import Argument, DocumentInput, ImageInput, InputError, MediaInput, TextInput, VideoInput

__all__ = [
    "Argument",
    "DocumentInput",
    "ImageInput",
    "InputError",
    "MediaInput",
    "MetaCommand",
    "MetaInfo",
    "TextInput",
    "VideoInput",
    "invoke_command",
    "register_command",
]
