from msu_hub_bot.settings import MissingIntegration

import asyncio
import random
from functools import cached_property
from itertools import cycle

import aiohttp
from pydantic import BaseModel, ConfigDict

from common.mixins import LoggerMixin
from msu_hub_bot.telemetry import Boundary, Outcome, Provider, Telemetry

LANGUAGES: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "ada": ("Ada", [("GNATMAKE 6.1.1", "0"), ("GNATMAKE 7.2.0", "1"), ("GNATMAKE 8.1.0", "2"), ("GNATMAKE 9.1.0", "3")]),
    "bash": ("Bash Shell", [("4.3.42", "0"), ("4.4.12", "1"), ("4.4.19", "2"), ("5.0.011", "3")]),
    "bc": ("BC", [("1.06.95", "0"), ("1.07.1", "1")]),
    "brainfuck": ("Brainfuck", [("bfc-0.1", "0")]),
    "c": ("C", [("GCC 5.3.0", "0"), ("Zapcc 5.0.0", "1"), ("GCC 7.2.0", "2"), ("GCC 8.1.0", "3"), ("GCC 9.1.0", "4")]),
    "c99": ("C-99", [("GCC 5.3.0", "0"), ("GCC 7.2.0", "1"), ("GCC 8.1.0", "2"), ("GCC 9.1.0", "3")]),
    "clisp": ("CLISP", [("GNU C 5.2.0", "0"), ("GNU C 6.2.1", "1"), ("GNU 8.1.0", "2"), ("GNU 9.1.0", "3")]),
    "clojure": ("Clojure", [("1.8.0", "0"), ("1.9.0", "1"), ("1.10.1", "2")]),
    "cobol": ("COBOL", [("GNU COBOL 2.0.0", "0"), ("GNU COBOL 2.2.0", "1"), ("GNU COBOL 3.0", "2")]),
    "coffeescript": ("CoffeeScript", [("1.11.1", "0"), ("2.0.0", "1"), ("2.3.0", "2"), ("2.4.1", "3")]),
    "cpp": ("C++", [("GCC 5.3.0", "0"), ("Zapcc 5.0.0", "1"), ("GCC 7.2.0", "2"), ("GCC 8.1.0", "3"), ("GCC 9.1.0", "4")]),
    "cpp14": ("C++ 14", [("g++ 14 GCC 5.3.0", "0"), ("g++ 14 GCC 7.2.0", "1"), ("g++ 14 GCC 8.1.0", "2"), ("g++ 14 GCC 9.1.0", "3")]),
    "cpp17": ("C++ 17", [("g++ 17 GCC 9.10", "0")]),
    "csharp": ("C#", [("mono 4.2.2", "0"), ("mono 5.0.0", "1"), ("mono 5.10.1", "2"), ("mono 6.0.0", "3")]),
    "d": ("D", [("DMD64 D Compiler v2.071.1", "0"), ("DMD64 D Compiler  v2.088", "1")]),
    "dart": ("Dart", [("1.18.0", "0"), ("1.24.2", "1"), ("1.24.3", "2"), ("2.5.1", "3")]),
    "elixir": ("Elixir", [("1.3.4", "0"), ("1.5.2", "1"), ("1.6.4", "2"), ("1.9.1", "3")]),
    "erlang": ("Erlang", [("22.1", "0")]),
    "factor": ("Factor", [("8.25", "0"), ("8.28", "1"), ("8.29", "2"), ("8.31", "3")]),
    "falcon": ("Falcon", [("0.9.6 (Chimera)", "0")]),
    "fantom": ("Fantom", [("1.0.69", "0")]),
    "forth": ("Forth", [("gforth 0.7.3", "0")]),
    "fortran": ("Fortran", [("GNU 5.3.0", "0"), ("GNU 7.2.0", "1"), ("GNU 8.1.0", "2"), ("GNU 9.1.0", "3")]),
    "freebasic": ("FREE BASIC", [("1.05.0", "0"), ("1.07.1", "1")]),
    "fsharp": ("F#", [("4.1", "0"), ("4.5.0", "1")]),
    "gccasm": ("Assembler - GCC", [("GCC 6.2.1", "0"), ("GCC 8.1.0", "1"), ("GCC 9.1.0", "2")]),
    "go": ("GO Lang", [("1.5.2", "0"), ("1.9.2", "1"), ("1.10.2", "2"), ("1.13.1", "3")]),
    "groovy": ("Groovy", [("2.4.6 JVM: 1.7.0_99", "0"), ("2.4 JVM: 9.0.1", "1"), ("2.4 JVM: 10.0.1", "2"), ("2.5.8 JVM: 11.0.4", "3")]),
    "hack": ("Hack", [("HipHop VM 3.13.0", "0")]),
    "haskell": ("Haskell", [("ghc 7.10.3", "0"), ("ghc 8.2.1", "1"), ("ghc 8.2.2", "2"), ("ghc 8.6.5", "3")]),
    "icon": ("Icon", [("9.4.3", "0"), ("9.5.1", "1")]),
    "intercal": ("Intercal", [("0.30", "0")]),
    "java": ("Java", [("JDK 1.8.0_66", "0"), ("JDK 9.0.1", "1"), ("JDK 10.0.1", "2"), ("JDK 11.0.4", "3")]),
    "jlang": ("J", [("9.01.10", "0")]),
    "kotlin": ("Kotlin", [("1.1.51 (JRE 9.0.1+11)", "0"), ("1.2.40 (JRE 10.0.1)", "1"), ("1.3.50 (JRE 11.0.4)", "2")]),
    "lolcode": ("LOLCODE", [("0.10.5", "0")]),
    "lua": ("Lua", [("5.3.2", "0"), ("5.3.4", "1"), ("5.3.5", "2")]),
    "mozart": ("OZ Mozart", [("2.0.0 (OZ 3)", "0")]),
    "nasm": ("Assembler - NASM", [("2.11.08", "0"), ("2.13.01", "1"), ("2.13.03", "2"), ("2.14.02", "3")]),
    "nemerle": ("Nemerle", [("1.2.0.507", "0")]),
    "nim": ("Nim", [("0.15.0", "0"), ("0.17.2", "1"), ("0.18.0", "2")]),
    "nodejs": ("NodeJS", [("6.3.1", "0"), ("9.2.0", "1"), ("10.1.0", "2"), ("12.11.1", "3")]),
    "objc": ("Objective C", [("GCC 5.3.0", "0"), ("GCC 7.2.0", "1"), ("GCC 8.1.0", "2"), ("GCC 9.1.0", "3")]),
    "ocaml": ("Ocaml", [("4.03.0", "0"), ("4.08.1", "1")]),
    "octave": ("Octave", [("GNU 4.0.0", "0"), ("GNU 4.2.1", "1"), ("GNU 4.4.0", "2"), ("GNU 5.1.0", "3")]),
    "pascal": ("Pascal", [("fpc 3.0.0", "0"), ("fpc-3.0.2", "1"), ("fpc-3.0.4", "2")]),
    "perl": ("Perl", [("5.22.0", "0"), ("5.26.1", "1"), ("5.26.2", "2"), ("5.30.0", "3")]),
    "php": ("PHP", [("5.6.16", "0"), ("7.1.11", "1"), ("7.2.5", "2"), ("7.3.10", "3")]),
    "picolisp": ("Picolisp", [("3.1.11.1", "0"), ("17.11.14", "1"), ("18.5.11", "2"), ("18.9.5", "3")]),
    "pike": ("Pike", [("v8.0", "0"), ("v8.0.702", "1")]),
    "prolog": ("Prolog", [("GNU Prolog 1.4.4", "0"), ("GNU Prolog 1.4.5", "1")]),
    "python2": ("Python 2", [("2.7.11", "0"), ("2.7.15", "1"), ("2.7.16", "2")]),
    "python3": ("Python 3", [("3.5.1", "0"), ("3.6.3", "1"), ("3.6.5", "2"), ("3.7.4", "3")]),
    "r": ("R Language", [("3.3.1", "0"), ("3.4.2", "1"), ("3.5.0", "2"), ("3.6.1", "3")]),
    "racket": ("Racket", [("6.11", "0"), ("6.12", "1"), ("7.4", "2")]),
    "rhino": ("Rhino JS", [("1.7.7.1", "0"), ("1.7.7.2", "1")]),
    "ruby": ("Ruby", [("2.2.4", "0"), ("2.4.2p198", "1"), ("2.5.1p57", "2"), ("2.6.5", "3")]),
    "rust": ("RUST", [("1.10.0", "0"), ("1.21.0", "1"), ("1.25.0", "2"), ("1.38.0", "3")]),
    "scala": ("Scala", [("2.12.0", "0"), ("2.12.4", "1"), ("2.12.5", "2"), ("2.13.0", "3")]),
    "scheme": ("Scheme", [("Gauche 0.9.4", "0"), ("Gauche 0.9.5", "1"), ("Gauche 0.9.8", "2")]),
    "smalltalk": ("SmallTalk", [("GNU SmallTalk 3.2.92", "0")]),
    "spidermonkey": ("SpiderMonkey", [("38", "0"), ("45.0.2", "1")]),
    "sql": ("SQL", [("SQLite 3.9.2", "0"), ("SQLite 3.21.0", "1"), ("SQLite 3.23.1", "2"), ("SQLite 3.29.0", "3")]),
    "swift": ("Swift", [("2.2", "0"), ("3.1.1", "1"), ("4.1", "2"), ("5.1", "3")]),
    "tcl": ("TCL", [("8.6", "0"), ("8.6.7", "1"), ("8.6.8", "2"), ("8.6.9", "3")]),
    "unlambda": ("Unlambda", [("0.1.3", "0")]),
    "vbn": ("VB.Net", [("mono 4.0.1", "0"), ("mono 4.6", "1"), ("mono 5.10.1", "2"), ("mono 6.0.0", "3")]),
    "verilog": ("VERILOG", [("10.1", "0"), ("10.2", "1"), ("10.3", "2")]),
    "whitespace": ("Whitespace", [("0.3", "0")]),
    "yabasic": ("YaBasic", [("2.769", "0"), ("2.84.1", "1")]),
}


class JDoodleResponse(BaseModel):
    output: str | None = None
    statusCode: int
    memory: str | None = None
    cpuTime: str | None = None
    model_config = ConfigDict(str_strip_whitespace=True, coerce_numbers_to_str=True)


class JDoodleCreditResponse(BaseModel):
    used: int


class JDoodleError(Exception):
    def __init__(self, code: int, reason: str | bytes) -> None:
        self.code = code
        self.reason = reason

    def __repr__(self) -> str:
        return f"[{self.code}] {str(self.reason)}"


class JDoodle(LoggerMixin):
    api_base = "https://api.jdoodle.com/v1/"

    def __init__(self, client_id: str, client_secret: str, telemetry: Telemetry | None = None) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.telemetry = telemetry or Telemetry()

    @cached_property
    def session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession()

    async def close(self) -> None:
        if "session" in self.__dict__:
            await self.session.close()

    async def _request(self, endpoint: str, json: dict[str, object] | None = None) -> bytes:
        json = json or {}
        json = dict(clientId=self.client_id, clientSecret=self.client_secret, **json)
        key = "jdoodle.execute" if endpoint == "execute" else "http.request"
        with self.telemetry.operation(Boundary.PROVIDER, key, provider=Provider.JDOODLE) as operation:
            async with self.session.post(self.api_base + endpoint, json=json) as response:
                operation.http_status(response.status)
                if response.status != 200:
                    operation.set_outcome(Outcome.UNAVAILABLE)
                    raise JDoodleError(response.status, await response.read())
                return await response.read()

    async def credit_spent(self) -> JDoodleCreditResponse:
        result = await self._request("credit-spent")
        return JDoodleCreditResponse.model_validate_json(result)

    async def request(self, source_code: str, stdin: str = "", lang: str = "python3", version: int | None = None) -> JDoodleResponse:
        selected_version = LANGUAGES[lang][1][-1][1] if version is None else str(version)
        json: dict[str, object] = dict(script=source_code, stdin=stdin, language=lang, versionIndex=selected_version)
        result = await self._request("execute", json=json)
        return JDoodleResponse.model_validate_json(result)

    async def request_and_parse(self, source_code: str, stdin: str = "", lang: str = "python3", version: int | None = None) -> str:
        r = await self.request(source_code, stdin, lang, version)

        if r.output is not None:
            output = "\n".join(r.output.split("\n")[:80])[:2048]

            if r.output.startswith("JDoodle - Timeout"):
                text = "🤷🏻‍♂️ Timeout\n\n"
            else:
                text = f"{output}\n\n"

                if r.output.endswith("JDoodle - output Limit reached."):
                    text += "🤷🏻‍♂️ Output limit reached\n\n"
        else:
            text = "🤷🏻‍♂️ No any output\n\n"

        if r.cpuTime is not None:
            text += f"CPU Time: {r.cpuTime[:4]} s\n"

        if r.memory is not None:
            text += f"Memory: {r.memory} KB\n"

        return text


class ManyJDoodle:
    def __init__(self, tokens: list[tuple[str, str]], telemetry: Telemetry | None = None) -> None:
        tokens = list(tokens)
        random.shuffle(tokens)
        self.instances = [JDoodle(*t, telemetry=telemetry) for t in tokens]
        self.it = cycle(self.instances)

    async def close(self) -> None:
        await asyncio.gather(*[i.close() for i in self.instances])

    @property
    def instance(self) -> JDoodle:
        if not self.instances:
            raise MissingIntegration("jdoodle_tokens")
        return next(self.it)
