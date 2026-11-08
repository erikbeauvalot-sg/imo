"""Runner de réunion et serveur web local, contre le faux Teams (pas de vraie réunion)."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
from fastapi.testclient import TestClient

import imo.runner
from imo.agent import WELCOME
from imo.connectors.teams import TeamsSession
from imo.models import ChatMessage
from imo.runner import MeetingConfig, MeetingRunner
from imo.server import create_app
from imo.storage import Store
from test_teams_join import FIXTURE, MEETING_URL


class FakeTeamsSession(TeamsSession):
    """Session servie par le faux Teams, sans requête vers le vrai."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.resolve_launcher = False

    async def start(self) -> None:
        await super().start()

        async def serve_fixture(route):
            await route.fulfill(status=200, content_type="text/html", body=FIXTURE)

        await self._context.route("https://teams.microsoft.com/**", serve_fixture)


@pytest.fixture
def fake_teams(monkeypatch):
    monkeypatch.setattr(imo.runner, "TeamsSession", FakeTeamsSession)
    monkeypatch.delenv("IMV_IM", raising=False)


def _config(query: str = "", **kwargs) -> MeetingConfig:
    return MeetingConfig(url=MEETING_URL + query, name="IMV - Test", debug_dir=None, **kwargs)


def test_runner_joins_reads_and_stops(fake_teams, caplog):
    caplog.set_level(logging.INFO, logger="imo")

    async def scenario():
        store = Store(":memory:")
        runner = MeetingRunner(_config(managers=["Bob Durand"]), store)
        task = asyncio.create_task(runner.run())
        deadline = time.monotonic() + 15
        while store.count_messages(runner.session_id) < 1:
            assert not task.done() and time.monotonic() < deadline, runner.events
            await asyncio.sleep(0.1)
        assert runner.status == "in_meeting"
        sent = await runner.session.page.evaluate("window.sent")
        assert sent[0].startswith(WELCOME)

        runner.stop()
        assert await asyncio.wait_for(task, 10) == 0
        assert runner.status == "ended"
        assert any("Messages enregistrés" in e for e in runner.events)
        assert store.messages(runner.session_id)[0]["text"] == "Le site de paiement renvoie des erreurs 500"

    asyncio.run(scenario())


def test_runner_leaves_after_imv_clore(fake_teams):
    async def scenario():
        store = Store(":memory:")
        runner = MeetingRunner(_config(managers=["Bob Durand"], welcome=False), store)
        task = asyncio.create_task(runner.run())
        deadline = time.monotonic() + 15
        while runner.agent is None or store.count_messages(runner.session_id) < 1:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.1)
        await runner.session.page.evaluate("addMessage('Bob Durand', 'imv clore', 'm9')")
        assert await asyncio.wait_for(task, 15) == 0
        return runner, store

    runner, store = asyncio.run(scenario())
    assert runner.status == "ended"
    kinds = [i["type"] for i in store.interventions(runner.session_id)]
    assert kinds[-2:] == ["report", "command"]


def test_runner_stopped_in_lobby(fake_teams):
    async def scenario():
        runner = MeetingRunner(_config("?lobby=60000"), Store(":memory:"))
        task = asyncio.create_task(runner.run())
        await asyncio.sleep(2)
        runner.stop()
        assert await asyncio.wait_for(task, 10) == 0
        assert runner.status == "ended"

    asyncio.run(scenario())


def test_runner_rejected(fake_teams):
    async def scenario():
        runner = MeetingRunner(_config("?reject=1&lobby=100"), Store(":memory:"))
        assert await asyncio.wait_for(runner.run(), 30) == 2
        assert runner.status == "failed" and runner.error

    asyncio.run(scenario())


def test_runner_validates_before_opening_the_browser():
    with pytest.raises(ValueError):
        MeetingRunner(MeetingConfig(url="https://example.com/meeting"), Store(":memory:"))


def _client(db) -> TestClient:
    return TestClient(create_app(db, MeetingConfig(url="", debug_dir=None), allowed_hosts=["testserver"]))


def _wait(client: TestClient, session_id: int, predicate, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        data = client.get(f"/api/meetings/{session_id}/feed").json()
        if predicate(data):
            return data
        assert time.monotonic() < deadline, data
        time.sleep(0.2)


def test_server_runs_several_meetings(fake_teams, tmp_path):
    db = tmp_path / "imv.db"
    with _client(db) as client:
        assert "IMV" in client.get("/").text
        ids = []
        for name in ("IMV - Atlas", "IMV - Vega"):
            r = client.post("/api/meetings", json={"url": MEETING_URL, "name": name, "im": ["Bob Durand"]})
            assert r.status_code == 201, r.text
            ids.append(r.json()["id"])
        for session_id in ids:
            data = _wait(client, session_id, lambda d: any("paiement" in f["text"] for f in d["feed"]))
            assert data["meeting"]["status"] == "in_meeting"
            assert data["meeting"]["managers"] == ["Bob Durand"]
        listed = client.get("/api/meetings").json()
        assert [m["id"] for m in listed] == sorted(ids, reverse=True)

        assert client.delete(f"/api/meetings/{ids[0]}").status_code == 202
        _wait(client, ids[0], lambda d: d["meeting"]["status"] == "ended" and not d["meeting"]["stopping"])
        assert client.get(f"/api/meetings/{ids[1]}/feed").json()["meeting"]["status"] == "in_meeting"
    # Arrêt du serveur : l'autre agent quitte aussi sa réunion
    assert [s["status"] for s in Store(db).list_sessions()] == ["ended", "ended"]


def test_server_rejects_bad_requests():
    with _client(":memory:") as client:
        r = client.post("/api/meetings", json={"url": "https://example.com/x"})
        assert r.status_code == 400 and "Teams" in r.json()["detail"]
        r = client.post("/api/meetings", content='{"url": "x"}', headers={"Content-Type": "text/plain"})
        assert r.status_code == 422
        assert client.get("/api/meetings/42/feed").status_code == 404
        assert client.delete("/api/meetings/42").status_code == 404
        assert client.get("/api/meetings").json() == []


def test_server_only_answers_to_localhost():
    with TestClient(create_app(":memory:", MeetingConfig(url=""))) as client:
        assert client.get("/api/meetings").status_code == 400  # Host: testserver
        assert client.get("/api/meetings", headers={"Host": "127.0.0.1:8765"}).status_code == 200


def test_server_sends_text_to_the_meeting_chat(fake_teams, tmp_path):
    with _client(tmp_path / "imv.db") as client:
        session_id = client.post("/api/meetings", json={"url": MEETING_URL, "welcome": False}).json()["id"]
        _wait(client, session_id, lambda d: d["meeting"]["status"] == "in_meeting" and d["feed"])
        assert client.post(f"/api/meetings/{session_id}/chat", json={"text": "  "}).status_code == 400
        r = client.post(f"/api/meetings/{session_id}/chat", json={"text": "Point dans 10 min\nMerci"})
        assert r.status_code == 200, r.text
        data = client.get(f"/api/meetings/{session_id}/feed").json()
        assert data["feed"][-1] == {**data["feed"][-1], "kind": "manual", "text": "Point dans 10 min\nMerci"}
        # Le message de l'agent n'est pas relu comme un message humain
        time.sleep(1.5)
        texts = [f["text"] for f in client.get(f"/api/meetings/{session_id}/feed").json()["feed"]]
        assert texts.count("Point dans 10 min\nMerci") == 1
        client.delete(f"/api/meetings/{session_id}")
        _wait(client, session_id, lambda d: not d["meeting"]["live"])
        r = client.post(f"/api/meetings/{session_id}/chat", json={"text": "trop tard"})
        assert r.status_code == 409


def test_server_keeps_history_across_restarts(tmp_path):
    db = tmp_path / "imv.db"
    store = Store(db)
    done = store.create_session(MEETING_URL, "IMV - Atlas")
    store.add_message(done, ChatMessage(sender="Alice", text="Erreurs 500", message_id="m1"))
    store.set_session_status(done, "ended")
    crashed = store.create_session(MEETING_URL, "IMV - Vega")  # serveur arrêté brutalement
    store.set_session_status(crashed, "in_meeting")
    store.close()

    with _client(db) as client:
        listed = client.get("/api/meetings").json()
        assert [(m["id"], m["status"], m["live"]) for m in listed] == [
            (crashed, "interrupted", False),
            (done, "ended", False),
        ]
        data = client.get(f"/api/meetings/{done}/feed").json()
        assert [f["text"] for f in data["feed"]] == ["Erreurs 500"]
        assert data["meeting"]["messages"] == 1 and data["events"] == []
        assert client.delete(f"/api/meetings/{done}").status_code == 409
        assert client.post(f"/api/meetings/{done}/chat", json={"text": "x"}).status_code == 409
