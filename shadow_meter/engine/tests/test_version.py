"""The version must be the same in the package, pyproject.toml and the app configuration."""

from __future__ import annotations

import re
from pathlib import Path

from shadow_meter import __version__

ENGINE = Path(__file__).resolve().parents[1]


def test_versions_match() -> None:
    pyproject = (ENGINE / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)[1] == __version__  # type: ignore[index]
    app_config = ENGINE.parent / "config.yaml"
    if app_config.exists():  # not present when the engine is built on its own
        found = re.search(r'^version: "([^"]+)"', app_config.read_text(encoding="utf-8"), re.MULTILINE)
        assert found is not None and found[1] == __version__
