"""Safe input issues are distinct from application configuration failures."""

import re
from collections.abc import Mapping
from types import MappingProxyType


class ConfigurationError(TypeError):
    """A declared invocation contract or supplied dependency is unusable."""


_MESSAGES: dict[str, tuple[str, str | None]] = {
    "attachment-too-large": ("The attachment is too large. Please send a smaller file.", None),
    "text-too-long": ("Text is too long; use at most {limit} characters.", "limit"),
    "text-too-large": ("Text is too large. Please send less text.", None),
    "text-encoding": ("Please send a UTF-8 text file.", None),
    "image-dimensions": ("The image dimensions are too large. Please resize it.", None),
    "image-decode": ("Could not decode the image. Please send another image.", None),
    "argument-invalid": ("Invalid value for '{parameter}'.", "parameter"),
    "argument-missing": ("Provide a valid value for '{parameter}'.", "parameter"),
    "text-missing": ("Provide text for '{parameter}'.", "parameter"),
    "text-invalid": ("The text for '{parameter}' is not valid. Please check it.", "parameter"),
    "media-missing": ("Attach or reply to media for '{parameter}'.", "parameter"),
    "media-type": ("The attachment has the wrong media type for '{parameter}'.", "parameter"),
    "callback-invalid": ("This button is no longer valid. Please open the feature again.", None),
}


class InputError(ValueError):
    """A localizable acquisition issue containing only declared names or limits.

    Raw user text, file identifiers and rejected values never belong in params.
    Application failures remain ordinary exceptions; explicit handler guidance
    can use the same issue through ``ctx.guide``.
    """

    code: str
    params: Mapping[str, str | int]

    def __init__(self, code: str, **params: str | int) -> None:
        definition = _MESSAGES.get(code)
        if definition is None:
            raise ConfigurationError(f"Unknown input issue code {code!r}")
        template, parameter = definition
        if params.keys() != ({parameter} if parameter is not None else set()):
            raise ConfigurationError(f"Invalid parameters for input issue {code!r}")
        if parameter == "parameter" and (
            not isinstance(params[parameter], str) or re.fullmatch(r"[^\W\d]\w*", str(params[parameter])) is None
        ):
            raise ConfigurationError("An input issue parameter must be a declared Python name")
        if parameter == "limit" and (type(params[parameter]) is not int or int(params[parameter]) <= 0):
            raise ConfigurationError("An input issue limit must be a positive integer")
        self.code = code
        self.params = MappingProxyType(dict(params))
        super().__init__(template.format_map(params))
