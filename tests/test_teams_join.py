"""Parcours complet contre un faux Teams servi localement par Playwright (pas de vraie réunion)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from imo.agent import WELCOME, IncidentAgent
from imo.connectors.teams import TeamsJoinError, TeamsSession, validate_display_name
from imo.connectors.teams.join import validate_meeting_url
from imo.models import ChatMessage
from imo.storage import Store

FIXTURE = (Path(__file__).parent / "fixtures" / "fake_teams.html").read_text(encoding="utf-8")
MEETING_URL = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_test%40thread.v2/0"


async def _session(query: str = "", tmp_path: Path | None = None) -> TeamsSession:
    session = TeamsSession(MEETING_URL + query, "IMV - Assistant IM", headless=True, debug_dir=tmp_path)
    await session.start()

    async def serve_fixture(route):
        await route.fulfill(status=200, content_type="text/html", body=FIXTURE)

    async def serve_login(route):
        await route.fulfill(status=200, content_type="text/html", body="<p>Sign in</p>")

    await session._context.route("https://teams.microsoft.com/**", serve_fixture)
    await session._context.route("https://login.microsoftonline.com/**", serve_login)
    return session


async def _wait_for(predicate, timeout: float = 5) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition non atteinte")
        await asyncio.sleep(0.1)


def test_join_and_read_chat():
    async def scenario():
        session = await _session("?lobby=500")
        received: list[ChatMessage] = []
        try:
            await session.join(admission_timeout=10)
            assert await session.page.evaluate("window.joinedAs") == "IMV - Assistant IM"
            # Caméra et micro coupés, « Don't use audio » choisi
            assert await session.page.evaluate("window.joinedMedia") == {
                "camera": False,
                "mic": False,
                "noAudio": True,
            }

            await session.open_chat()
            await session.read_chat(received.append)
            await _wait_for(lambda: len(received) == 1)

            page = session.page
            await page.evaluate("addMessage('Bob Durand', 'Je regarde les logs du LB', 'm2')")
            # Suite de messages du même auteur : pas d'en-tête auteur dans le DOM
            await page.evaluate("addMessage('Bob Durand', 'Pic de CPU sur le nœud 2', 'm3', false)")
            # Événement Teams ; identifiant = horodatage en millisecondes, comme le vrai Teams
            await page.evaluate(
                "addMessage('', 'IMV - Assistant IM has temporarily joined the chat.', '1794061245353', false)"
            )
            await _wait_for(lambda: len(received) == 4)

            # Re-rendu d'un message existant (liste virtualisée) : pas de doublon
            await page.evaluate("rerenderFirst()")
            await asyncio.sleep(0.5)
            assert len(received) == 4

            await session.leave()
            assert await page.evaluate("window.left") is True
        finally:
            await session.close()
        return received

    received = asyncio.run(scenario())
    assert [(m.sender, m.text, m.source) for m in received] == [
        ("Alice Martin", "Le site de paiement renvoie des erreurs 500", "chat"),
        ("Bob Durand", "Je regarde les logs du LB", "chat"),
        ("Bob Durand", "Pic de CPU sur le nœud 2", "chat"),
        ("Teams", "IMV - Assistant IM has temporarily joined the chat.", "system"),
    ]
    assert received[0].message_id == "m1"
    assert received[0].sent_at == "2026-11-07T10:00:00Z"
    assert received[0].backlog is True and received[1].backlog is False
    assert received[3].sent_at == "2026-11-07T14:20:45+00:00"


def test_agent_welcome_and_commands_in_chat():
    async def scenario():
        session = await _session("?lobby=100")
        store = Store(":memory:")
        try:
            await session.join(admission_timeout=10)
            await session.open_chat()
            sid = store.create_session(session.url, session.display_name)
            agent = IncidentAgent(store, sid, session.display_name, session.send_chat)
            queue: asyncio.Queue[ChatMessage] = asyncio.Queue()
            await session.read_chat(queue.put_nowait)
            assert await agent.welcome() is True
            worker = asyncio.create_task(agent.consume(queue))

            await session.page.evaluate("addMessage('Bob Durand', 'imv démarrer INC0012345', 'm2')")
            await _wait_for(lambda: len(store.interventions(sid)) == 2)
            # Laisse au lecteur le temps de voir la réponse de l'agent dans le chat
            await asyncio.sleep(0.5)
            worker.cancel()
            sent = await session.page.evaluate("window.sent")
            return sent, [(m["author_name"], m["text"]) for m in store.messages(sid)], store.session_incident(sid)
        finally:
            await session.close()

    sent, messages, incident = asyncio.run(scenario())
    assert sent[0] == WELCOME
    assert sent[1].startswith("Suivi de l'incident INC0012345 démarré")
    # Ni l'accueil ni la réponse de l'agent ne sont enregistrés comme messages humains
    assert messages == [
        ("Alice Martin", "Le site de paiement renvoie des erreurs 500"),
        ("Bob Durand", "imv démarrer INC0012345"),
    ]
    assert incident["ref"] == "INC0012345"


def test_send_chat_when_read_only():
    async def scenario():
        session = await _session("?lobby=100&readonly=1")
        try:
            await session.join(admission_timeout=10)
            await session.open_chat()
            return await session.send_chat("bonjour")
        finally:
            await session.close()

    assert asyncio.run(scenario()) is False


def test_meeting_end_is_detected():
    async def scenario():
        session = await _session("?lobby=100&endAfter=2500")
        try:
            await session.join(admission_timeout=10)
            return await asyncio.wait_for(session.wait_until_ended(poll_seconds=0.2), timeout=5)
        finally:
            await session.close()

    assert asyncio.run(scenario()) == "ended_or_removed"


def test_rejected_admission(tmp_path):
    async def scenario():
        session = await _session("?lobby=100&reject=1", tmp_path)
        try:
            await session.join(admission_timeout=10)
        finally:
            await session.close()

    with pytest.raises(TeamsJoinError) as exc:
        asyncio.run(scenario())
    assert exc.value.code == "rejected"
    # Capture de diagnostic enregistrée
    assert list(tmp_path.glob("*-join-failed.png"))


def test_login_redirect_means_guests_not_allowed():
    async def scenario():
        session = await _session("?login=1")
        try:
            await session.join(prejoin_timeout=10)
        finally:
            await session.close()

    with pytest.raises(TeamsJoinError) as exc:
        asyncio.run(scenario())
    assert exc.value.code == "login_required"


@pytest.mark.parametrize("name", ["IMV - Assistant IM", "Bot_IM.1", "Agent@IM", "Équipe IM"])
def test_valid_display_names(name):
    assert validate_display_name(name) == name


@pytest.mark.parametrize("name", ["", "IMV (bot)", "IM: agent", "a/b"])
def test_invalid_display_names(name):
    with pytest.raises(ValueError):
        validate_display_name(name)


@pytest.mark.parametrize(
    "url",
    [
        "https://teams.microsoft.com/l/meetup-join/19%3ameeting_x%40thread.v2/0?context=%7b%7d",
        "https://teams.microsoft.com/meet/123456789?p=abc",
        "https://teams.live.com/meet/9876",
    ],
)
def test_valid_meeting_urls(url):
    assert validate_meeting_url(url) == url


@pytest.mark.parametrize("url", ["http://teams.microsoft.com/x", "https://evil.example.com/meet/1", "teams"])
def test_invalid_meeting_urls(url):
    with pytest.raises(ValueError):
        validate_meeting_url(url)
