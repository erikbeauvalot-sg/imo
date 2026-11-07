"""Moteur de l'agent et stockage SQLite, sans navigateur."""

from __future__ import annotations

import asyncio

import pytest

from imo.agent import HELP, WELCOME, Command, IncidentAgent, parse_command
from imo.llm import LLMError
from imo.models import ChatMessage
from imo.storage import Store


@pytest.mark.parametrize(
    "text, expected",
    [
        ("imv aide", Command("aide", "aide", "")),
        ("IMV", Command("aide", "aide", "")),
        ("imv: démarrer INC0012345", Command("demarrer", "démarrer", "INC0012345")),
        ("  Imv Demarrer  INC1 hors prod", Command("demarrer", "Demarrer", "INC1 hors prod")),
        ("imv reprendre", Command("reprise", "reprendre", "")),
        ("imv résumé", Command("resume", "résumé", "")),
        ("imv blabla", Command("blabla", "blabla", "")),
    ],
)
def test_parse_command(text, expected):
    assert parse_command(text) == expected


@pytest.mark.parametrize("text", ["bonjour", "imvx aide", "on relance imv aide", ""])
def test_not_a_command(text):
    assert parse_command(text) is None


class FakeChat:
    """Remplace TeamsSession.send_chat ; renvoie l'écho comme le ferait Teams."""

    def __init__(self, writable: bool = True) -> None:
        self.writable = writable
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        if self.writable:
            self.sent.append(text)
        return self.writable


class FakeLLM:
    name = "fake"

    def __init__(self, error: bool = False) -> None:
        self.error = error
        self.prompts: list[str] = []

    async def complete(self, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        await asyncio.sleep(0.05)
        if self.error:
            raise LLMError("quota atteint")
        return "Symptômes : erreurs 500"


def _agent(writable: bool = True, llm=None) -> tuple[IncidentAgent, FakeChat, Store, int]:
    store = Store(":memory:")
    session_id = store.create_session("https://teams.microsoft.com/meet/1", "IMV - Assistant IM")
    chat = FakeChat(writable)
    return IncidentAgent(store, session_id, "IMV - Assistant IM", chat.send, llm=llm), chat, store, session_id


def test_analysis_command_runs_in_background():
    async def scenario():
        llm = FakeLLM()
        agent, chat, store, sid = _agent(llm=llm)
        await agent.handle(_msg("Alice", "Erreurs 500 sur le paiement", "m1"))
        await agent.handle(_msg("Bob", "imv résumé", "m2"))
        # L'analyse tourne en fond : les messages suivants sont enregistrés sans attendre
        await agent.handle(_msg("Alice", "le LB est saturé", "m3"))
        assert chat.sent == []
        await agent.wait_analyses()
        return chat, llm, store, sid

    chat, llm, store, sid = asyncio.run(scenario())
    assert chat.sent == ["Symptômes : erreurs 500"]
    assert "Alice : Erreurs 500 sur le paiement" in llm.prompts[0]
    assert store.count_messages(sid) == 3


def test_ask_com_and_usage_errors():
    async def scenario():
        llm = FakeLLM()
        agent, chat, _, _ = _agent(llm=llm)
        await agent.handle(_msg("Alice", "Erreurs 500 sur le paiement", "m1"))
        await agent.handle(_msg("Bob", "imv ? quelle est la cause ?", "m2"))
        await agent.handle(_msg("Bob", "imv com utilisateurs", "m3"))
        await agent.handle(_msg("Bob", "imv ?", "m4"))
        await agent.handle(_msg("Bob", "imv com presse", "m5"))
        await agent.wait_analyses()
        return chat.sent, llm.prompts

    sent, prompts = asyncio.run(scenario())
    assert len(prompts) == 2
    assert "Question : quelle est la cause ?" in prompts[0]
    assert "audience « utilisateurs »" in prompts[1]
    assert any("Posez votre question" in s for s in sent)
    assert any("Audience inconnue" in s for s in sent)


def test_secret_posted_alert():
    async def scenario():
        agent, chat, store, sid = _agent()
        await agent.handle(_msg("Thomas", "mot de passe : Xk9!pq2", "m1"))
        await agent.handle(_msg("Alice", "le token a expiré", "m2"))  # pas un secret
        await agent.handle(_msg("Bob", "imv pause", "m3"))
        await agent.handle(_msg("Thomas", "password=hunter22", "m4"))  # en pause : pas d'alerte
        return chat.sent, store.interventions(sid)

    sent, interventions = asyncio.run(scenario())
    assert len(sent) == 2 and sent[0].startswith("Attention : le message de Thomas")
    assert (interventions[0]["type"], interventions[0]["rule_id"]) == ("proactive", "secret_posted")


def test_analysis_error_and_missing_llm():
    async def scenario():
        agent, chat, _, _ = _agent(llm=FakeLLM(error=True))
        await agent.handle(_msg("Alice", "Erreurs 500", "m1"))
        await agent.handle(_msg("Bob", "imv timeline", "m2"))
        await agent.wait_analyses()
        no_llm, chat2, _, _ = _agent()
        await no_llm.handle(_msg("Bob", "imv questions", "m1"))
        return chat.sent, chat2.sent

    with_error, without_llm = asyncio.run(scenario())
    assert with_error == ["Analyse « timeline » impossible pour le moment (quota atteint)."]
    assert "option --llm" in without_llm[0]


def _msg(sender: str, text: str, mid: str | None = None, **kw) -> ChatMessage:
    return ChatMessage(sender=sender, text=text, message_id=mid, **kw)


def test_commands_and_recording():
    async def scenario():
        agent, chat, store, sid = _agent()
        await agent.handle(_msg("Alice", "Erreurs 500 sur le paiement", "m1"))
        await agent.handle(_msg("Bob", "imv démarrer INC0012345", "m2"))
        await agent.handle(_msg("Bob", "imv statut", "m3"))
        await agent.handle(_msg("Bob", "imv aide", "m4"))
        await agent.handle(_msg("Bob", "imv clore", "m5"))
        await agent.handle(_msg("Bob", "imv xyz", "m6"))
        return chat, store, sid

    chat, store, sid = asyncio.run(scenario())
    assert chat.sent[0].startswith("Suivi de l'incident INC0012345 démarré, IM : Bob")
    assert "messages enregistrés : 3" in chat.sent[1]
    assert chat.sent[2] == HELP
    assert "pas encore disponible" in chat.sent[3]
    assert "Commande inconnue" in chat.sent[4] and "Vouliez-vous" not in chat.sent[4]

    incident = store.session_incident(sid)
    assert incident["ref"] == "INC0012345" and incident["im_name"] == "Bob"
    # Les messages antérieurs à « démarrer » sont rattachés à l'incident
    assert {m["incident_id"] for m in store.messages(sid)} == {incident["id"]}
    assert len(store.interventions(sid)) == 5


@pytest.mark.parametrize(
    "typo, suggestion",
    [("imv resmué", "imv résumé"), ("imv demarer INC1", "imv démarrer"), ("imv qestions", "imv questions")],
)
def test_typo_suggests_closest_command(typo, suggestion):
    async def scenario():
        agent, chat, _, _ = _agent()
        await agent.handle(_msg("Bob", typo, "m1"))
        return chat.sent[0]

    assert f"Vouliez-vous dire « {suggestion} » ?" in asyncio.run(scenario())


def test_start_twice_keeps_first_incident():
    async def scenario():
        agent, chat, store, sid = _agent()
        await agent.handle(_msg("Bob", "imv démarrer INC1", "m1"))
        await agent.handle(_msg("Bob", "imv démarrer INC2", "m2"))
        return chat, store, sid

    chat, store, sid = asyncio.run(scenario())
    assert chat.sent[1] == "Suivi déjà démarré : INC1."
    assert store.session_incident(sid)["ref"] == "INC1"


def test_pause_stops_recording():
    async def scenario():
        agent, chat, store, sid = _agent()
        await agent.handle(_msg("Bob", "imv pause", "m1"))
        await agent.handle(_msg("Alice", "mot de passe temporaire : hunter2", "m2"))
        await agent.handle(_msg("Bob", "imv reprise", "m3"))
        await agent.handle(_msg("Alice", "le service est revenu", "m4"))
        return store, sid

    store, sid = asyncio.run(scenario())
    assert [m["text"] for m in store.messages(sid)] == ["imv pause", "le service est revenu"]


def test_own_messages_are_ignored():
    async def scenario():
        agent, chat, store, sid = _agent()
        await agent.welcome()
        # Teams affiche le message de l'agent sans auteur : le lecteur l'attribue au dernier auteur vu
        await agent.handle(_msg("Alice", WELCOME.replace("\n", " "), "own1"))
        await agent.handle(_msg("IMV - Assistant IM (Guest)", "texte quelconque", "own2"))
        await agent.handle(_msg("Alice", "message humain", "m1"))
        return store, sid

    store, sid = asyncio.run(scenario())
    assert [m["text"] for m in store.messages(sid)] == ["message humain"]


def test_backlog_commands_are_not_executed():
    async def scenario():
        agent, chat, store, sid = _agent()
        await agent.handle(_msg("Bob", "imv pause", "m1", backlog=True))
        return agent, chat, store, sid

    agent, chat, store, sid = asyncio.run(scenario())
    assert chat.sent == [] and agent.paused is False
    assert store.count_messages(sid) == 1


def test_read_only_chat_still_records_intervention():
    async def scenario():
        agent, chat, store, sid = _agent(writable=False)
        assert await agent.welcome() is False
        await agent.handle(_msg("Bob", "imv aide", "m1"))
        return store, sid

    store, sid = asyncio.run(scenario())
    assert [(i["type"], i["delivered"]) for i in store.interventions(sid)] == [("welcome", 0), ("command", 0)]


def test_store_dedup_and_session_status(tmp_path):
    store = Store(tmp_path / "sub" / "imv.db")
    sid = store.create_session("https://teams.microsoft.com/meet/1", "IMV")
    assert store.add_message(sid, _msg("Alice", "a", "m1")) is True
    assert store.add_message(sid, _msg("Alice", "a", "m1")) is False
    # Sans identifiant Teams, pas de déduplication par la base (le lecteur s'en charge)
    assert store.add_message(sid, _msg("Alice", "b")) is True
    assert store.add_message(sid, _msg("Alice", "b")) is True

    store.set_session_status(sid, "in_meeting")
    store.set_session_status(sid, "failed", "chat_unavailable")
    (row,) = store.list_sessions()
    assert row["status"] == "failed" and row["error_code"] == "chat_unavailable"
    assert row["message_count"] == 3
    store.close()
