import json
import sys
from types import ModuleType

import pytest

from teleforge.app import App
from teleforge.cli import main
from teleforge.declarations import command
from teleforge.feature import Feature


def test_cli_loads_factory_without_lifespan(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Commands(Feature):
        @command("roll")
        async def roll(self, count: int = 3) -> str:
            return str(count)

    module = ModuleType("offline_factory")
    module.create = lambda: App(Commands())
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert main(["inspect", "offline_factory:create", "--json"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["handlers"][0]["names"] == ["roll"]
    assert manifest["handlers"][0]["parameters"][0]["source"] == "argument"
    assert main(["check", "offline_factory:create"]) == 0
    assert "valid" in capsys.readouterr().out


def test_cli_reports_invalid_reference(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["check", "not-a-factory"])
    assert error.value.code == 2
    assert "module:factory" in capsys.readouterr().err
