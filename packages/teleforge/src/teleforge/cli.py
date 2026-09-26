"""Inspect the real application definition without running its lifespan."""

import argparse
import importlib
import json
from collections.abc import Sequence
from typing import cast

from .app import App


def load_app(reference: str) -> App:
    module_name, separator, name = reference.partition(":")
    if not separator or not module_name or not name or "." in name:
        raise ValueError("Use an importable module:factory reference")
    candidate = getattr(importlib.import_module(module_name), name)
    value = candidate() if callable(candidate) else candidate
    if not isinstance(value, App):
        raise TypeError("The application factory must synchronously return App without opening resources")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="teleforge")
    parser.add_argument("command", choices=("inspect", "check"))
    parser.add_argument("application", help="module:factory; importing it must not start the application")
    parser.add_argument("--json", action="store_true", help="Print the versioned structural manifest")
    args = parser.parse_args(argv)
    try:
        app = load_app(args.application)
    except (ImportError, AttributeError, TypeError, ValueError) as error:
        parser.exit(2, f"Cannot load application ({type(error).__name__}); check module:factory and its return type.\n")
    manifest = app.inspect()
    diagnostics = cast(list[dict[str, object]], manifest["diagnostics"])
    if args.json:
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
    elif args.command == "inspect":
        for handler in cast(list[dict[str, object]], manifest["handlers"]):
            print(
                f"{handler['key']}: {handler['kind']} {handler['event'] or ''} {' '.join(cast(list[str], handler['names']))}".rstrip()
            )
    for diagnostic in diagnostics:
        if not args.json:
            source = cast(dict[str, object], diagnostic["source"])
            location = f"{source['file']}:{source['line']}" if source["file"] else diagnostic["feature"]
            print(f"{location}: {diagnostic['code']}: {diagnostic['message']}")
    if args.command == "check" and not diagnostics and not args.json:
        print("Application declarations are valid.")
    return 1 if diagnostics else 0


if __name__ == "__main__":
    raise SystemExit(main())
