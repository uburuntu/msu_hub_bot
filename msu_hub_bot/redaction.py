"""Redact configured values from local logs, tracebacks, and output streams."""

import io
import json
import logging
import re
import sys
import traceback
from urllib.parse import quote, quote_plus, urlsplit

from msu_hub_bot.settings import settings

MASK = "[REDACTED]"
_sensitive_key = re.compile(r"token|password|secret|authorization|cookie|api.?key|credential|dsn", re.I)
_patterns = (
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{25,})\b"),
    re.compile(r"(?<=://)[^\s/@]+:[^\s/@]+@"),
    re.compile(r"(?i)((?:authorization|password|api_key|access_token)\s*[:=]\s*)[^\s,;}]+"),
)


def _strings(value, sensitive=False):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, sensitive or bool(_sensitive_key.search(str(key))))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item, sensitive)
    elif isinstance(value, str) and value and (sensitive or len(value) >= 8):
        yield value
        if "://" in value:
            try:
                password = urlsplit(value).password
                if password:
                    yield password
            except ValueError:
                pass
    elif isinstance(value, int) and abs(value) >= 1_000_000:
        yield str(value)


def redact(value: object) -> str:
    text = str(value)
    variants = set()
    for secret in _strings(settings.dict()):
        variants.update(
            (secret, quote(secret, safe=""), quote_plus(secret), json.dumps(secret, ensure_ascii=False)[1:-1], repr(secret)[1:-1])
        )
        variants.update(line for line in secret.splitlines() if len(line) >= 8)
    for secret in sorted(variants, key=len, reverse=True):
        text = text.replace(secret, MASK)
    for pattern in _patterns:
        text = pattern.sub(MASK, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


class RedactingStream(io.TextIOBase):
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.pending = ""

    @property
    def encoding(self):
        return self.wrapped.encoding

    def write(self, text):
        self.pending += text
        if "\n" in self.pending:
            complete, self.pending = self.pending.rsplit("\n", 1)
            self.wrapped.write(redact(complete) + "\n")
        return len(text)

    def flush(self):
        if self.pending:
            self.wrapped.write(redact(self.pending))
            self.pending = ""
        self.wrapped.flush()

    def fileno(self):
        return self.wrapped.fileno()

    def isatty(self):
        return self.wrapped.isatty()


def install_redaction():
    if isinstance(sys.stdout, RedactingStream):
        return
    factory = logging.getLogRecordFactory()

    def safe_record(*args, **kwargs):
        record = factory(*args, **kwargs)
        record.msg, record.args = redact(record.getMessage()), ()
        if record.exc_info:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return record

    logging.setLogRecordFactory(safe_record)
    sys.stdout = RedactingStream(sys.stdout)
    sys.stderr = RedactingStream(sys.stderr)
