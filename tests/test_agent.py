"""Moteur de l'agent et stockage SQLite, sans navigateur."""

from __future__ import annotations

import asyncio

import pytest

from imo.agent import HELP, WELCOME, Command, IncidentAgent, parse_command
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


def _agent(writable: bool = True) -> tuple[IncidentAgent, FakeChat, Store, int]:
    store = Store(":memory:")
    session_id = store.create_session("https://teams.microsoft.com/meet/1", "IMV - Assistant IM")
    chat = FakeChat(writable)
    return IncidentAgent(store, session_id, "IMV - Assistant IM", chat.send), chat, store, session_id


def _msg(sender: str, text: str, mid: str | None = None, **kw) -> ChatMessage:
    return ChatMessage(sender=sender, text=text, message_id=mid, **kw)


def test_commands_and_recording():
    async def scenario():
        agent, chat, store, sid = _agent()
        await agent.handle(_msg("Alice", "Erreurs 500 sur le paiement", "m1"))
        await agent.handle(_msg("Bob", "imv démarrer INC0012345", "m2"))
        await agent.handle(_msg("Bob", "imv statut", "m3"))
        await agent.handle(_msg("Bob", "imv aide", "m4"))
        await agent.handle(_msg("Bob", "imv timeline", "m5"))
        await agent.handle(_msg("Bob", "imv xyz", "m6"))
        return chat, store, sid

    chat, store, sid = asyncio.run(scenario())
    assert chat.sent[0].startswith("Suivi de l'incident INC0012345 démarré, IM : Bob")
    assert "messages enregistrés : 3" in chat.sent[1]
    assert chat.sent[2] == HELP
    assert "pas encore disponible" in chat.sent[3]
    assert "Commande inconnue" in chat.sent[4]

    incident = store.session_incident(sid)
    assert incident["ref"] == "INC0012345" and incident["im_name"] == "Bob"
    # Les messages antérieurs à « démarrer » sont rattachés à l'incident
    assert {m["incident_id"] for m in store.messages(sid)} == {incident["id"]}
    assert len(store.interventions(sid)) == 5


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
