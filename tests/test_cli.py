"""Valeurs par défaut de la ligne de commande."""

from __future__ import annotations

from pathlib import Path

from imo.cli import build_parser
from imo.connectors.teams.join import validate_display_name
from imo.runner import DEFAULT_AGENT_NAME, resolve_context_path


def test_default_agent_name_is_a_valid_display_name():
    assert DEFAULT_AGENT_NAME == "IMV - Oscar"
    assert validate_display_name(DEFAULT_AGENT_NAME) == DEFAULT_AGENT_NAME


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


def test_browser_hidden_by_default():
    args = build_parser().parse_args(["join", "https://teams.microsoft.com/meet/1"])
    assert not args.show
    assert build_parser().parse_args(["join", "https://teams.microsoft.com/meet/1", "--show"]).show
