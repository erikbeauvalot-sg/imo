"""Connecteur Telegram, contre un faux serveur de l'API Bot (aucune requête vers Telegram)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from imo.connectors.telegram import is_telegram_url, parse_chat_id, reset_bots
from imo.runner import MeetingConfig, MeetingRunner
from imo.server import create_app
from imo.storage import Store

BOT = {"id": 999, "is_bot": True, "first_name": "Oscar", "username": "oscar_test_bot",
       "can_read_all_group_messages": True}
GROUP = {"id": -100123, "type": "supergroup", "title": "Incident paiement"}
BOB = {"id": 11, "first_name": "Bob", "last_name": "Durand", "username": "bobd"}


class FakeTelegram:
    """Faux serveur de l'API Bot : updates poussées par le test, messages envoyés enregistrés."""

    def __init__(self, chats=(GROUP,)) -> None:
        self.chats = {c["id"]: c for c in chats}
        self.updates: list[dict] = []
        self.sent: list[dict] = []
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                method = self.path.rsplit("/", 1)[1]
                params = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                status, body = fake.handle(method, params)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self._next_id = 1

    def handle(self, method: str, params: dict) -> tuple[int, dict]:
        ok = lambda result: (200, {"ok": True, "result": result})  # noqa: E731
        if method == "getMe":
            return ok(BOT)
        if method == "setMyCommands":
            return ok(True)
        if method == "getChat":
            chat = self.chats.get(params["chat_id"])
            return ok(chat) if chat else (400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})
        if method == "getChatMember":
            return ok({"status": "member"})
        if method == "sendMessage":
            with self.lock:
                self.sent.append(params)
            return ok({"message_id": len(self.sent)})
        if method == "getUpdates":
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline:
                with self.lock:
                    pending = [u for u in self.updates if u["update_id"] >= params.get("offset", 0)]
                if pending:
                    return ok(pending)
                time.sleep(0.02)
            return ok([])
        return 404, {"ok": False, "error_code": 404, "description": "Not Found"}

    def push_message(self, user: dict, text: str, chat: dict = GROUP, date: float | None = None) -> None:
        with self.lock:
            self.updates.append({"update_id": self._next_id, "message": {
                "message_id": self._next_id, "from": user, "chat": chat, "text": text,
                "date": int(date if date is not None else time.time())}})
            self._next_id += 1

    def push_removed(self, chat: dict = GROUP) -> None:
        with self.lock:
            self.updates.append({"update_id": self._next_id, "my_chat_member": {
                "chat": chat, "new_chat_member": {"user": BOT, "status": "kicked"}}})
            self._next_id += 1

    def texts(self) -> list[str]:
        with self.lock:
            return [m["text"] for m in self.sent]

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def telegram(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test")
    monkeypatch.setenv("TELEGRAM_API_URL", fake.url)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    reset_bots()
    yield fake
    reset_bots()
    fake.close()


async def _until(predicate, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "délai dépassé"
        await asyncio.sleep(0.05)


def test_telegram_links():
    assert is_telegram_url("telegram:-100123") and not is_telegram_url("https://teams.microsoft.com/x")
    assert parse_chat_id(" telegram:-100123 ") == -100123
    with pytest.raises(ValueError):
        parse_chat_id("telegram:groupe")


def test_runner_follows_a_telegram_group(telegram):
    async def scenario():
        from imo.connectors.telegram import get_bot

        store = Store(":memory:")
        runner = MeetingRunner(MeetingConfig(url="telegram:-100123", name="IMV - Oscar", managers=["Bob Durand (#11)"],
                                             debug_dir=None), store)
        task = asyncio.create_task(runner.run())
        await _until(lambda: runner.status == "in_meeting" and telegram.sent)
        assert "Oscar" in telegram.texts()[0]  # accueil

        # Message écrit avant l'arrivée d'Oscar et livré en retard par Telegram (ex. serveur
        # redémarré) : enregistré, mais sa commande n'est pas exécutée
        telegram.push_message(BOB, "imv statut", date=time.time() - 60)
        await _until(lambda: store.count_messages(runner.session_id) >= 1)
        telegram.push_message(BOB, "Oscar, statut")
        await _until(lambda: len(telegram.sent) >= 2)
        telegram.push_message(BOB, "/aide@oscar_test_bot")
        await _until(lambda: len(telegram.sent) >= 3)
        telegram.push_message(BOB, "/start@autre_bot")  # commande d'un autre bot : ignorée
        telegram.push_message(BOB, "Le site de paiement renvoie des erreurs 500")
        # Même nom affiché que le responsable, autre compte : commande réservée refusée
        telegram.push_message({"id": 66, "first_name": "Bob", "last_name": "Durand"}, "imv autoriser tous")
        await _until(lambda: len(telegram.sent) >= 4)
        assert not runner.agent.open_to_all

        telegram.push_removed()  # bot retiré du groupe : fin de session
        assert await asyncio.wait_for(task, 10) == 0
        assert get_bot().sessions == {}
        await get_bot().stop()
        return runner, store

    runner, store = asyncio.run(scenario())
    texts = telegram.texts()
    assert len(texts) == 4, texts  # accueil, statut, aide, refus : rien pour le message d'avant l'arrivée
    assert "enregistrement : actif" in texts[1]  # réponse à « Oscar, statut »
    assert "imv hermes" in texts[2]  # aide complète
    assert "Bob Durand (#66)" in texts[3] and "réservé aux responsables" in texts[3]
    messages = [(m["author_name"], m["text"]) for m in store.messages(runner.session_id)]
    assert messages[0] == ("Bob Durand (#11)", "imv statut")
    assert ("Bob Durand (#11)", "imv aide") in messages
    assert not any("autre_bot" in t for _, t in messages)
    assert runner.status == "ended" and runner.session.url == "telegram:-100123"


def test_unknown_telegram_group_fails_cleanly(telegram):
    async def scenario():
        from imo.connectors.telegram import get_bot

        store = Store(":memory:")
        runner = MeetingRunner(MeetingConfig(url="telegram:-555", debug_dir=None), store)
        code = await asyncio.wait_for(runner.run(), 10)
        await get_bot().stop()
        return code, runner, store

    code, runner, store = asyncio.run(scenario())
    assert code == 2 and runner.status == "failed"
    assert store.session(runner.session_id)["error_code"] == "chat_not_found"


def test_oscar_command_starts_a_session_for_allowed_users(telegram, monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "@BobD, 42")
    other_group = {"id": -100456, "type": "group", "title": "Autre groupe"}
    telegram.chats[other_group["id"]] = other_group
    app = create_app(tmp_path / "imv.db", MeetingConfig(url="", debug_dir=None), allowed_hosts=["testserver"])
    with TestClient(app) as client:
        telegram.push_message({"id": 7, "first_name": "Mallory"}, "/oscar", chat=other_group)
        telegram.push_message(BOB, "/oscar@oscar_test_bot")
        deadline = time.monotonic() + 15
        while True:
            meetings = client.get("/api/meetings").json()
            if meetings and meetings[0]["status"] == "in_meeting" and len(telegram.sent) >= 2:
                break
            assert time.monotonic() < deadline, (meetings, telegram.texts())
            time.sleep(0.1)
        assert [(m["url"], m["managers"]) for m in meetings] == [("telegram:-100123", ["Bob Durand (#11)"])]
        assert meetings[0]["created_by"] == "telegram:@bobd"
        texts = telegram.texts()
        assert any("Mallory (#7), vous n'êtes pas autorisé" in t for t in texts)
        options = client.get("/api/options").json()["telegram"]
        assert options["bot"] == "@oscar_test_bot"
        assert {c["title"] for c in options["chats"]} == {"Incident paiement", "Autre groupe"}

        # « Quitter la réunion » : au revoir, le bot reste dans le groupe
        client.delete(f"/api/meetings/{meetings[0]['id']}")
        while client.get(f"/api/meetings/{meetings[0]['id']}").json()["live"]:
            assert time.monotonic() < deadline + 15
            time.sleep(0.1)
        assert "IMV follow-up stops here" in telegram.texts()[-1] or "suivi par IMV" in telegram.texts()[-1]
