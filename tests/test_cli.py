"""Valeurs par défaut de la ligne de commande."""

from __future__ import annotations

from pathlib import Path

from imo.cli import AGENT_NAMES, build_parser, random_agent_name, resolve_context_path
from imo.connectors.teams.join import validate_display_name


def test_random_agent_name_is_a_valid_display_name():
    for _ in range(20):
        name = random_agent_name()
        assert name.startswith("IMV - ") and name[6:] in AGENT_NAMES
        assert validate_display_name(name) == name


def test_context_file_is_loaded_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IMV_CONTEXT", raising=False)
    assert resolve_context_path(None) is None
    (tmp_path / "contexte.md").write_text("DBA : dba-oncall", encoding="utf-8")
    assert resolve_context_path(None) == Path("contexte.md")
    # Un fichier désigné explicitement reste prioritaire
    assert resolve_context_path(Path("autre.md")) == Path("autre.md")


def test_name_and_context_options(monkeypatch):
    monkeypatch.delenv("IMV_NAME", raising=False)
    monkeypatch.delenv("IMV_CONTEXT", raising=False)
    args = build_parser().parse_args(["join", "https://teams.microsoft.com/meet/1"])
    assert args.name is None and args.context is None
    args = build_parser().parse_args(["join", "https://teams.microsoft.com/meet/1", "--name", "IMV - Test"])
    assert args.name == "IMV - Test"
