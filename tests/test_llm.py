"""Fournisseurs LLM (sans appel réel) et préparation des analyses."""

from __future__ import annotations

import asyncio
import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from datetime import datetime

from imo.analysis import (
    SYSTEM,
    analyze,
    build_system,
    contains_secret,
    format_transcript,
    load_context,
    mask_secrets,
    resolve_audience,
)
from imo.llm import LLMError, make_llm
from imo.llm.providers import ClaudeCLI, Ollama


def _row(author, text, sent_at="2026-11-07T14:20:45+00:00"):
    return {"author_name": author, "text": text, "sent_at": sent_at, "captured_at": sent_at}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("le mot de passe : hunter2", "le mot de passe [MASQUÉ]"),
        ("password=abc123 ok", "password [MASQUÉ] ok"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.x", "Authorization: [MASQUÉ]"),
        ("rien de sensible", "rien de sensible"),
    ],
)
def test_mask_secrets(text, expected):
    assert mask_secrets(text) == expected


def test_format_transcript_skips_commands_and_masks():
    transcript = format_transcript(
        [
            _row("Alice", "Erreurs 500\nsur le paiement"),
            _row("Bob", "imv résumé"),
            _row("Bob", "token: abcdef"),
        ]
    )
    lines = transcript.splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("Alice : Erreurs 500 sur le paiement")
    assert lines[1].endswith("Bob : token [MASQUÉ]")


class FakeLLM:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        return "Symptômes : erreurs 500"


def test_analyze_builds_prompt():
    llm = FakeLLM()
    result = asyncio.run(analyze(llm, "resume", [_row("Alice", "Erreurs 500")], "INC42"))
    assert result == "Symptômes : erreurs 500"
    system, prompt = llm.calls[0]
    assert system == SYSTEM
    assert "Incident : INC42" in prompt and "<chat>" in prompt and "Alice : Erreurs 500" in prompt


def test_context_goes_into_system_prompt(tmp_path):
    (tmp_path / "contexte.md").write_text("Équipe DBA : astreinte dba-oncall", encoding="utf-8")
    context = load_context(tmp_path / "contexte.md")
    llm = FakeLLM()
    asyncio.run(analyze(llm, "pistes", [_row("Alice", "Erreurs 500")], context=context))
    system, prompt = llm.calls[0]
    assert system.startswith(SYSTEM) and "<contexte>\nÉquipe DBA : astreinte dba-oncall\n</contexte>" in system
    # Contexte dans le prompt système (mis en cache), pas dans le message
    assert "dba-oncall" not in prompt and "Hypothèses" in prompt
    assert build_system(None) == build_system("  \n") == SYSTEM


def test_unreadable_context_file(tmp_path):
    assert load_context(None) is None
    with pytest.raises(ValueError, match="fichier de contexte illisible"):
        load_context(tmp_path / "absent.md")


def test_catchup_since_hour():
    llm = FakeLLM()
    rows = [
        _row("Alice", "ancien message", "2026-11-07T08:00:00+00:00"),
        _row("Bob", "message récent", "2026-11-07T09:30:00+00:00"),
    ]
    since = datetime.fromisoformat("2026-11-07T09:00:00+00:00").astimezone().strftime("%H:%M")
    asyncio.run(analyze(llm, "catchup", rows, arg=since))
    prompt = llm.calls[0][1]
    assert "message récent" in prompt and "ancien message" not in prompt
    assert f"depuis {since}" in prompt


@pytest.mark.parametrize(
    "arg, expected", [("", "direction"), ("mgmt", "direction"), ("users", "utilisateurs"), ("Tech", "tech")]
)
def test_resolve_audience(arg, expected):
    assert resolve_audience(arg) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("mot de passe : Xk9!pq2", True),
        ("api_key=abcd1234", True),
        ("voici ghp_abcdefghijklmnop", True),
        ("le token a expiré", False),
        ("quel est le mot de passe du compte ?", False),
    ],
)
def test_contains_secret(text, expected):
    assert contains_secret(text) is expected


def test_analyze_without_messages_does_not_call_llm():
    llm = FakeLLM()
    assert "Aucun message" in asyncio.run(analyze(llm, "resume", [_row("Bob", "imv résumé")]))
    assert llm.calls == []


def _fake_claude(tmp_path, payload: dict):
    """Remplace la commande `claude` : enregistre ses arguments et son entrée, renvoie `payload`."""
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{tmp_path}/args"\n'
        f'cat > "{tmp_path}/stdin"\n'
        f"echo '{json.dumps(payload)}'\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_claude_cli(tmp_path):
    script = _fake_claude(tmp_path, {"type": "result", "subtype": "success", "is_error": False, "result": " OK "})
    llm = ClaudeCLI("sonnet", executable=str(script))
    assert asyncio.run(llm.complete("système", "question")) == "OK"
    args = (tmp_path / "args").read_text().splitlines()
    assert args[:4] == ["-p", "--output-format", "json", "--model"]
    assert "--tools" in args and args[args.index("--tools") + 1] == ""
    assert args[args.index("--setting-sources") + 1] == ""
    assert args[args.index("--system-prompt") + 1] == "système"
    assert (tmp_path / "stdin").read_text() == "question"


def test_claude_cli_error(tmp_path):
    script = _fake_claude(tmp_path, {"type": "result", "subtype": "success", "is_error": True, "result": "quota"})
    with pytest.raises(LLMError, match="quota"):
        asyncio.run(ClaudeCLI("sonnet", executable=str(script)).complete("s", "p"))


def test_claude_cli_missing_executable():
    with pytest.raises(LLMError, match="introuvable"):
        asyncio.run(ClaudeCLI("sonnet", executable="/nonexistent/claude").complete("s", "p"))


def test_ollama():
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received["path"] = self.path
            received["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"message": {"content": "<think>hmm</think>\nRéponse"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        llm = Ollama("qwen3:14b", host=f"127.0.0.1:{server.server_port}")
        assert asyncio.run(llm.complete("système", "question")) == "Réponse"
    finally:
        server.shutdown()
    assert received["path"] == "/api/chat"
    assert received["body"]["model"] == "qwen3:14b" and received["body"]["stream"] is False
    assert received["body"]["messages"][0] == {"role": "system", "content": "système"}


def test_ollama_unreachable():
    with pytest.raises(LLMError, match="injoignable"):
        asyncio.run(Ollama("x", host="http://127.0.0.1:9", timeout=2).complete("s", "p"))


_AZURE_VARS = (
    "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_FOUNDRY_MODEL",
    "AZURE_FOUNDRY_ENDPOINT", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_API_VERSION",
    "AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET", "AZURE_TOKEN_SCOPE",
)


@pytest.fixture
def azure_env(monkeypatch):
    """Environnement Azure vierge (les variables du poste ne doivent pas fausser les tests)."""
    for var in _AZURE_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _service_principal(env):
    env.setenv("AZURE_TENANT_ID", "00000000-0000-0000-0000-000000000000")
    env.setenv("AZURE_CLIENT_ID", "11111111-1111-1111-1111-111111111111")
    env.setenv("AZURE_CLIENT_SECRET", "secret")


def test_foundry_without_configuration(azure_env):
    with pytest.raises(LLMError, match="Foundry"):
        make_llm("foundry")


def test_foundry_with_entra_id_and_common_endpoint(azure_env):
    _service_principal(azure_env)
    azure_env.setenv("AZURE_FOUNDRY_ENDPOINT", "https://ma-ressource.services.ai.azure.com/")
    azure_env.setenv("ANTHROPIC_FOUNDRY_MODEL", "claude-sonnet-5-5")
    llm = make_llm("foundry").llm
    assert llm.name == "foundry:claude-sonnet-5-5"
    assert str(llm.client.base_url) == "https://ma-ressource.services.ai.azure.com/anthropic/"


def test_azure_openai_configuration_errors(azure_env):
    with pytest.raises(LLMError, match="adresse"):
        make_llm("azure-openai")
    azure_env.setenv("AZURE_FOUNDRY_ENDPOINT", "https://ma-ressource.services.ai.azure.com")
    with pytest.raises(LLMError, match="OPENAI_MODEL"):
        make_llm("azure-openai")
    _service_principal(azure_env)
    azure_env.setenv("OPENAI_MODEL", "gpt-5-mini")
    assert make_llm("azure-openai").name == "azure-openai:gpt-5-mini"


def test_azure_openai_chat(azure_env):
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received["path"] = self.path
            received["api-key"] = self.headers.get("api-key")
            received["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps(
                {
                    "id": "x", "object": "chat.completion", "created": 0, "model": "gpt-5-mini",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": " Résumé "}}],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    azure_env.setenv("AZURE_OPENAI_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
    azure_env.setenv("AZURE_OPENAI_API_KEY", "cle")
    azure_env.setenv("OPENAI_MODEL", "gpt-5-mini")
    try:
        assert asyncio.run(make_llm("azure-openai").complete("système", "question")) == "Résumé"
    finally:
        server.shutdown()
    assert received["path"].startswith("/openai/deployments/gpt-5-mini/chat/completions?api-version=")
    assert received["api-key"] == "cle"
    assert received["body"]["messages"][0] == {"role": "system", "content": "système"}


def test_unknown_provider():
    with pytest.raises(ValueError):
        make_llm("gpt")


def test_transcript_lines_are_marked_oral():
    rows = [_row("Alice", "Erreurs 500"), {**_row("Bob", "je redémarre le pod"), "source": "transcript"}]
    lines = format_transcript(rows).splitlines()
    assert lines[0].endswith("Alice : Erreurs 500")
    assert lines[1].endswith("Bob (oral) : je redémarre le pod")
    assert "(oral)" in SYSTEM
