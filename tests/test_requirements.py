"""requirements*.txt (installation pip seule) doivent refléter pyproject.toml (installation uv)."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _requirements(name: str) -> set[str]:
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith(("#", "-r"))}


def test_requirements_match_pyproject():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert _requirements("requirements.txt") == set(project["dependencies"])
    assert _requirements("requirements-dev.txt") == set(project["optional-dependencies"]["dev"])
