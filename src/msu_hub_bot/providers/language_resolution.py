"""Supported language normalization and lossless translation-body selection."""

import re
from dataclasses import dataclass

from msu_hub_bot.providers.jev import MAX_LANGUAGE_CHOICES

_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_POSITIONAL_CODE = re.compile(r"(?:auto|detect|[A-Za-z]{2,3}(?:[-_][A-Za-z]{2,4})?)\Z", re.IGNORECASE)
_CONTROL = re.compile(r"(?:перев(?:еди|едите|ести|од|оди|одите)\b|на\s|с\s|translate\b|to\s|into\s|from\s)", re.IGNORECASE)
_QUOTED = re.compile(r'"([^"\n]+)"|«([^»\n]+)»')


class LanguageResolutionError(ValueError):
    def __init__(self) -> None:
        super().__init__("Language arguments could not be resolved")


@dataclass(frozen=True, slots=True)
class LanguageCatalogue:
    choices: dict[str, str]
    aliases: dict[str, str]

    @classmethod
    def from_provider(cls, data: object) -> "LanguageCatalogue":
        if not isinstance(data, list) or not 1 <= len(data) <= MAX_LANGUAGE_CHOICES * 2:
            raise LanguageResolutionError()
        choices: dict[str, str] = {}
        aliases: dict[str, str] = {}
        for row in data:
            if not isinstance(row, dict):
                raise LanguageResolutionError()
            code = row.get("full_code")
            if not isinstance(code, str) or _CODE.fullmatch(code) is None or code == "none":
                raise LanguageResolutionError()
            name = row.get("englishName")
            choices[code] = name[:160] if isinstance(name, str) and name.strip() else code
            for value in (code, row.get("code_alpha_1"), row.get("codeName")):
                if isinstance(value, str) and value.strip():
                    # Matches full_code's first-provider-match policy for aliases
                    # shared by regional variants. Jev sees canonical codes.
                    aliases.setdefault(value.strip().replace("-", "_").casefold(), code)
        if len(choices) > MAX_LANGUAGE_CHOICES:
            raise LanguageResolutionError()
        return cls(choices, aliases)

    def normalize(self, value: str | None) -> str | None:
        return self.aliases.get(value.strip().replace("-", "_").casefold()) if value else None


@dataclass(frozen=True, slots=True, repr=False)
class TranslationBody:
    text: str
    request: str
    from_reply: bool = False


def translation_body(raw: str, reply_text: str | None, *, known_source: bool, known_target: bool) -> TranslationBody | None:
    """Select original substrings, never ask a model to rewrite source content.

    Natural inline controls need a colon or quoted body. An explicit reply is
    otherwise the source; bare inline text is usable when there is no reply.
    """
    tokens = list(re.finditer(r"\S+", raw))
    if (
        len(tokens) > 2
        and (known_source or known_target or any(token.group().casefold() in {"auto", "detect"} for token in tokens[:2]))
        and all(_POSITIONAL_CODE.fullmatch(token.group()) for token in tokens[:2])
    ):
        # A positional prefix establishes the body boundary. Its URLs, colons
        # and quotations are source content, not another translation instruction.
        return TranslationBody(raw[tokens[2].start() :], raw[: tokens[1].end()])

    head, separator, body = raw.partition(":")
    if separator and body.strip() and (_CONTROL.search(head) or known_source or known_target):
        return TranslationBody(body.lstrip(), head)
    quoted = list(_QUOTED.finditer(raw))
    if len(quoted) == 1:
        match = quoted[0]
        instruction = raw[: match.start()] + raw[match.end() :]
        if _CONTROL.search(instruction) or known_source or known_target:
            return TranslationBody(next(part for part in match.groups() if part is not None), instruction.strip())

    if reply_text is not None:
        return TranslationBody(reply_text, raw, from_reply=True)
    if known_source or known_target:
        # With one explicit language, a following word may be either another
        # language name or source content. Require a proven body boundary.
        return None
    if raw.strip() and not _CONTROL.match(raw.strip()):
        return TranslationBody(raw, "")
    return None
