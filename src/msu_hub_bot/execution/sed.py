"""Run user-supplied substitutions in a short-lived, killable process."""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import cast

SED_TIMEOUT = 2
MAX_TEXT_LENGTH = 4096
MAX_COMMANDS = 5
FLAGS = {"A": re.ASCII, "I": re.IGNORECASE, "L": re.LOCALE, "U": re.UNICODE, "M": re.MULTILINE, "S": re.DOTALL, "X": re.VERBOSE}
COMMAND = re.compile(r"^[sSыЫ]/(?P<pattern>(?:\\.|[^/\\\n])+)/(?P<sub>(?:\\.|[^/\\\n])*)(?:/(?P<mode>\w*))?$")


class SedTimeout(Exception):
    pass


def _calculate(text: str, commands: list[str]) -> str | None:
    substitutions = []
    for line in commands[:MAX_COMMANDS]:
        if len(line) > MAX_TEXT_LENGTH or not (match := COMMAND.fullmatch(line)):
            return None
        substitutions.append(match.groupdict())
    text = text[:MAX_TEXT_LENGTH]
    for substitution in substitutions:
        mode = substitution["mode"] or "mi"
        flags = 0
        for symbol in mode:
            flags |= FLAGS.get(symbol.upper(), 0)
        try:
            text = re.sub(
                cast(str, substitution["pattern"]).replace(r"\/", "/"),
                cast(str, substitution["sub"]).replace(r"\/", "/"),
                text,
                flags=flags,
            )[:MAX_TEXT_LENGTH]
        except re.error, ValueError:
            continue
    return text


def sed_calc(text: str, commands: list[str], limit: int = MAX_COMMANDS) -> str | None:
    payload = json.dumps([text[:MAX_TEXT_LENGTH], commands[: min(limit, MAX_COMMANDS)]], ensure_ascii=False).encode()
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve())],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=SED_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise SedTimeout from exc
    return cast(str | None, json.loads(result.stdout))


if __name__ == "__main__":
    print(json.dumps(_calculate(*json.load(sys.stdin)), ensure_ascii=False))
