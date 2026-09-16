"""The bot's slash-command and inline-hashtag grammar, independent of Telegram I/O."""

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    command: str = ""
    hashtag: str = ""
    arguments: tuple[str, ...] = ()
    text: str = ""

    @property
    def keyword(self) -> str:
        return self.command or self.hashtag


class CommandParser:
    """Match one route; router order decides which matching route wins."""

    def __init__(self, *keywords: str, args: int | None = None) -> None:
        self.args = args
        self.commands = tuple(keyword.lower() for keyword in keywords)
        # The underscore tail contains positional hashtag arguments. Keep it separate
        # from the registered keyword, which may itself contain underscores.
        alternatives = "|".join(f"(?:{re.escape(keyword)})" for keyword in keywords)
        self.hashtags_pattern = re.compile(rf"#\b({alternatives})((?:_[\w\d]*)*)\b", re.IGNORECASE)

    def parse(self, text: str | None, *, username: str | None = None) -> ParsedCommand | None:
        if not text or not text.strip():
            return None
        return self._command(text, username) or self._hashtag(text)

    def _command(self, text: str, username: str | None) -> ParsedCommand | None:
        words = text.split()
        full_command = words[0]
        command, _, mention = full_command[1:].partition("@")
        if full_command[0] != "/" or command.lower() not in self.commands:
            return None
        if mention and (username is None or mention.lower() != username.lower()):
            return None
        if self.args is None:
            arguments = words[1:]
            body = text.lstrip()[len(full_command) :]
        else:
            count = 1 + self.args
            arguments = words[1:count]
            parts = text.split(maxsplit=count)
            body = parts[count] if len(parts) > count else ""
        return ParsedCommand(command=command, arguments=tuple(arguments), text=body.strip())

    def _hashtag(self, text: str) -> ParsedCommand | None:
        match = self.hashtags_pattern.search(text)
        if match is None:
            return None
        hashtag, tail = match.groups()
        arguments = tuple(argument for argument in tail.split("_") if argument)[: self.args]
        return ParsedCommand(
            hashtag=hashtag,
            arguments=arguments,
            text=(text[: match.start()] + text[match.end() :]).strip(),
        )
