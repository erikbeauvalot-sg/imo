"""Parcours complet contre un faux Teams servi localement par Playwright (pas de vraie réunion)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import pytest

from imo.agent import IncidentAgent
from imo.connectors.teams import TeamsJoinError, TeamsSession, validate_display_name
from imo.connectors.teams.avatar import avatar_video
from imo.connectors.teams.join import disable_app_launch, validate_meeting_url, web_join_url
from imo.models import ChatMessage
from imo.storage import Store

FIXTURE = (Path(__file__).parent / "fixtures" / "fake_teams.html").read_text(encoding="utf-8")
MEETING_URL = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_test%40thread.v2/0"


async def _session(query: str = "", tmp_path: Path | None = None, camera_video: Path | None = None) -> TeamsSession:
    session = TeamsSession(
        MEETING_URL + query,
        "IMV - Assistant IM",
        headless=True,
        debug_dir=tmp_path,
        resolve_launcher=False,
        camera_video=camera_video,
    )
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


# chatDelay : panneau de chat lent à s'afficher, l'accueil doit quand même partir
@pytest.mark.parametrize("query", ["?lobby=100", "?lobby=100&chatDelay=1500"])
def test_agent_welcome_and_commands_in_chat(query):
    async def scenario():
        session = await _session(query)
        store = Store(":memory:")
        try:
            await session.join(admission_timeout=10)
            await session.open_chat()
            sid = store.create_session(session.url, session.display_name)
            agent = IncidentAgent(store, sid, session.display_name, session.send_chat, managers=["Bob Durand"])
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
    assert sent[0].startswith("Bonjour, je suis Assistant IM, l'assistant de l'Incident Manager")
    assert sent[1].startswith("Suivi de l'incident INC0012345 démarré")
    # Ni l'accueil ni la réponse de l'agent ne sont enregistrés comme messages humains
    assert messages == [
        ("Alice Martin", "Le site de paiement renvoie des erreurs 500"),
        ("Bob Durand", "imv démarrer INC0012345"),
    ]
    assert incident["ref"] == "INC0012345"


@pytest.mark.parametrize("query", ["?lobby=100", "?lobby=100&camoff=1"])
def test_avatar_keeps_camera_on(query):
    async def scenario():
        session = await _session(query, camera_video=avatar_video())
        try:
            await session.join(admission_timeout=10)
            return await session.page.evaluate("window.joinedMedia")
        finally:
            await session.close()

    assert asyncio.run(scenario()) == {"camera": True, "mic": False, "noAudio": True}


def test_avatar_video_files(tmp_path):
    from PIL import Image

    robot = avatar_video()
    header, frame = robot.read_bytes().split(b"\n", 2)[0], robot.read_bytes().split(b"FRAME\n", 1)[1]
    assert header == b"YUV4MPEG2 W640 H360 F15:1 Ip A1:1 C420jpeg"
    assert len(frame) == 640 * 360 * 3 // 2
    # Image fournie, ratio quelconque : centrée sur un fond 16:9
    Image.new("RGB", (100, 300), (255, 0, 0)).save(tmp_path / "logo.png")
    custom = avatar_video(tmp_path / "logo.png")
    assert custom != robot and custom.stat().st_size == robot.stat().st_size


def test_missing_browser_is_reported():
    async def scenario():
        session = TeamsSession(MEETING_URL, "IMV - Assistant IM", headless=True, browser="navigateur-absent")
        try:
            await session.start()
        finally:
            await session.close()

    with pytest.raises(TeamsJoinError) as exc:
        asyncio.run(scenario())
    assert exc.value.code == "browser_missing" and "Microsoft Edge" in str(exc.value)


@pytest.mark.parametrize("channel", ["chrome", "msedge"])
def test_installed_browser_channel(channel):
    """Chrome / Edge installés (cas d'un poste où « playwright install » est bloqué)."""
    async def scenario():
        session = TeamsSession(MEETING_URL, "IMV - Assistant IM", headless=True, resolve_launcher=False, browser=channel)
        try:
            await session.start()
        except TeamsJoinError:
            pytest.skip(f"{channel} n'est pas installé sur cette machine")
        try:
            async def serve_fixture(route):
                await route.fulfill(status=200, content_type="text/html", body=FIXTURE)

            await session._context.route("https://teams.microsoft.com/**", serve_fixture)
            await session.join(admission_timeout=10)
            return await session.page.evaluate("window.joinedAs")
        finally:
            await session.close()

    assert asyncio.run(scenario()) == "IMV - Assistant IM"


def test_send_chat_when_read_only():
    async def scenario():
        session = await _session("?lobby=100&readonly=1")
        try:
            await session.join(admission_timeout=10)
            await session.open_chat(input_timeout=1)
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


def test_unrecognized_admission_screen_is_captured(tmp_path, caplog):
    async def scenario():
        session = await _session(tmp_path=tmp_path)
        try:
            await session.page.goto(MEETING_URL)
            await session.page.set_content("<p>Connecting to meeting...</p>")
            with pytest.raises(TeamsJoinError) as exc:
                await session._wait_admission(12)
            assert exc.value.code == "admission_timeout"
        finally:
            await session.close()

    asyncio.run(scenario())
    assert len(list(tmp_path.glob("*-admission-pending.png"))) == 1
    captures = list(tmp_path.glob("*-admission-pending.html"))
    assert len(captures) == 1
    assert "Connecting to meeting" in captures[0].read_text(encoding="utf-8")
    assert "ni la réunion ni la salle d'attente" in caplog.text


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


def test_disable_app_launch():
    # Redirection réelle d'un lien teams.microsoft.com/meet/... (novembre 2026)
    launcher = (
        "https://teams.microsoft.com/dl/launcher/launcher.html?url=%2F_%23%2Fmeet%2F344615604655081"
        "%3Fp%3Dsecret%26anon%3Dtrue&type=meet&deeplinkId=d1e5&directDl=true&msLaunch=true&enableMobilePage=true"
    )
    rewritten = disable_app_launch(launcher)
    assert rewritten == "https://teams.microsoft.com/_?webjoin=true#/meet/344615604655081?p=secret&anon=true"
    assert disable_app_launch(rewritten) == rewritten
    assert disable_app_launch(MEETING_URL) == MEETING_URL


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://teams.microsoft.com/meet/123?p=abc", "https://teams.microsoft.com/meet/123?p=abc&webjoin=true"),
        ("https://teams.microsoft.com/meet/123?webjoin=false", "https://teams.microsoft.com/meet/123?webjoin=true"),
        ("https://teams.microsoft.com/_#/meet/123?p=abc", "https://teams.microsoft.com/_?webjoin=true#/meet/123?p=abc"),
    ],
)
def test_web_join_url(url, expected):
    assert web_join_url(url) == expected
    assert web_join_url(expected) == expected


@pytest.mark.parametrize("destination", ["/_#/meet/123?p=abc&anon=true", MEETING_URL])
def test_launcher_opens_web_destination_without_loading_launcher(destination):
    async def scenario():
        session = await _session()
        session.resolve_launcher = True
        launcher_requests = []
        try:
            async def serve(route):
                launcher_requests.append(route.request.url)
                await route.fulfill(body="<p>Native app launcher must not load</p>")

            await session._context.unroute("**/dl/launcher/launcher.html*")
            await session._context.route("**/dl/launcher/launcher.html*", serve)
            launcher = "https://teams.microsoft.com/dl/launcher/launcher.html?msLaunch=true&url=" + quote(
                destination, safe=""
            )
            response = SimpleNamespace(headers={"location": launcher}, dispose=AsyncMock())
            with patch.object(session._context.request, "get", new=AsyncMock(return_value=response)):
                await session.join(prejoin_timeout=5, admission_timeout=5)
                session._context.request.get.assert_awaited_once_with(
                    web_join_url(MEETING_URL), max_redirects=0, timeout=15000
                )
            assert session.page.url == disable_app_launch(launcher)
            assert await session.page.evaluate("window.joinedAs") == "IMV - Assistant IM"
            assert launcher_requests == []
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("destination", ["https://example.com/meet/1", "msteams:/meet/1", "/dl/launcher/launcher.html"])
def test_invalid_launcher_destination(destination):
    launcher = "https://teams.microsoft.com/dl/launcher/launcher.html?url=" + quote(destination, safe="")
    with pytest.raises(TeamsJoinError) as exc:
        disable_app_launch(launcher)
    assert exc.value.code == "invalid_launcher"


def test_continue_in_browser_is_not_clicked_repeatedly():
    async def scenario():
        session = await _session()
        try:
            await session.page.goto(MEETING_URL)
            await session.page.evaluate(
                """() => {
                    window.continueClicks = 0;
                    document.getElementById('continue').onclick = () => { window.continueClicks++; };
                }"""
            )
            with pytest.raises(TeamsJoinError) as exc:
                await session._reach_prejoin(2)
            assert exc.value.code == "prejoin_timeout"
            assert await session.page.evaluate("window.continueClicks") == 1
        finally:
            await session.close()

    asyncio.run(scenario())


def test_launcher_navigation_is_rewritten():
    async def scenario():
        session = TeamsSession(MEETING_URL, "IMV - Assistant IM", headless=True, resolve_launcher=False)
        await session.start()
        try:
            async def launcher_page(route):
                await route.fulfill(status=200, content_type="text/html", body="<p>launcher</p>")

            # La dernière route enregistrée passe en premier : la réécriture est réinstallée
            # après la fausse page du lanceur, qui ne sert qu'en repli (route.fallback).
            await session._context.route("https://teams.microsoft.com/dl/launcher/**", launcher_page)
            await session._context.unroute("**/dl/launcher/launcher.html*")
            await session._context.route("**/dl/launcher/launcher.html*", session._block_app_launch)
            await session.page.goto("https://teams.microsoft.com/dl/launcher/launcher.html?msLaunch=true")
            return session.page.url
        finally:
            await session.close()

    url = asyncio.run(scenario())
    assert "msLaunch=false" in url


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


def test_captions_become_transcript_messages():
    async def scenario():
        session = await _session("?lobby=100")
        received: list[ChatMessage] = []
        try:
            await session.join(admission_timeout=10)
            assert await session.enable_captions(timeout=5) is True
            await session.page.evaluate("window.__imvCaptionsStableMs = 600")
            await session.read_captions(received.append)
            page = session.page
            # Phrase qui s'allonge pendant qu'Alice parle : publiée une fois stable
            await page.evaluate("caption(1, 'Alice Martin', 'Le site')")
            await asyncio.sleep(0.3)
            await page.evaluate("caption(1, 'Alice Martin', 'Le site renvoie des erreurs 500')")
            await _wait_for(lambda: len(received) == 1)
            # La même ligne s'allonge encore : seule la suite est publiée
            await page.evaluate("caption(1, 'Alice Martin', 'Le site renvoie des erreurs 500 depuis midi')")
            # Ligne retirée de l'écran avant d'être stable : publiée quand même
            await page.evaluate("caption(2, 'Bob Durand', 'Je regarde les logs')")
            await asyncio.sleep(0.1)
            await page.evaluate("removeCaption(2)")
            await _wait_for(lambda: len(received) == 3)
            await asyncio.sleep(1)  # aucune publication en double
            return received, await session.captions_state()
        finally:
            await session.close()

    received, state = asyncio.run(scenario())
    assert [(m.sender, m.text, m.source) for m in received] == [
        ("Alice Martin", "Le site renvoie des erreurs 500", "transcript"),
        ("Bob Durand", "Je regarde les logs", "transcript"),
        ("Alice Martin", "depuis midi", "transcript"),
    ]
    assert state["matchedContainer"] == '[data-tid="closed-captions-renderer"]'


@pytest.mark.parametrize("menu", ["", "&captionsmenu=speech"])
def test_captions_menu_variants(menu):
    """More → Captions (vrai Teams, invité) ou More → Language and speech → Turn on live captions."""

    async def scenario():
        session = await _session("?lobby=100" + menu)
        try:
            await session.join(admission_timeout=10)
            return await session.enable_captions(timeout=5)
        finally:
            await session.close()

    assert asyncio.run(scenario()) is True


def test_spoken_language():
    async def scenario():
        session = await _session("?lobby=100")
        try:
            await session.join(admission_timeout=10)
            assert await session.enable_captions(timeout=5) is True
            assert await session.set_spoken_language("fr") is True
            return await session.page.evaluate("window.spokenLanguage")
        finally:
            await session.close()

    assert asyncio.run(scenario()) == "French (France)"


def test_captions_unavailable(caplog):
    caplog.set_level(logging.INFO, logger="imo")

    async def scenario():
        session = await _session("?lobby=100&nocaptions=1")
        try:
            await session.join(admission_timeout=10)
            return await session.enable_captions(timeout=2)
        finally:
            await session.close()

    assert asyncio.run(scenario()) is False
    # Diagnostic : contenu du menu More journalisé pour ajuster les sélecteurs
    assert "Sous-titres, menu More : Meeting info | Settings" in caplog.text
    # Diagnostic : contenu du menu More journalisé pour ajuster les sélecteurs
    assert "Sous-titres, menu More : Meeting info | Settings" in caplog.text
