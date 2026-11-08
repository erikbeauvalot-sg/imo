"""Utilisateurs, groupes et droits du serveur web (serve --auth), sans navigateur."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import imo.cli
import imo.server
from imo.auth import COOKIE, LoginThrottle, hash_password, token_hash, verify_password
from imo.cli import main
from imo.models import ChatMessage
from imo.runner import MeetingConfig
from imo.server import create_app
from imo.storage import Store
from test_teams_join import MEETING_URL

PASSWORD = "correct horse battery"


def test_password_hashing():
    stored = hash_password(PASSWORD)
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert verify_password(PASSWORD, stored)
    assert not verify_password("autre mot de passe", stored)
    assert not verify_password(PASSWORD, "n'importe quoi")
    assert hash_password(PASSWORD) != stored  # sel aléatoire


def test_login_throttle(monkeypatch):
    throttle = LoginThrottle()
    for _ in range(LoginThrottle.MAX_FAILURES):
        assert not throttle.blocked("Alice")
        throttle.failed("alice")
    assert throttle.blocked("ALICE")
    throttle.succeeded("Alice")
    assert not throttle.blocked("alice")


def test_users_and_groups_in_the_store():
    store = Store(":memory:")
    sre, dba = store.create_group("SRE"), store.create_group("DBA")
    with pytest.raises(ValueError):
        store.create_group("sre")  # nom insensible à la casse
    alice = store.create_user("Alice", "h", "operator", [sre])
    with pytest.raises(ValueError):
        store.create_user("alice", "h", "viewer")
    assert store.user_by_name("ALICE").groups == {sre: "SRE"}
    store.update_user(alice, role="viewer", group_ids=[sre, dba])
    assert store.user(alice).role == "viewer" and set(store.user(alice).groups) == {sre, dba}

    # Un nouveau mot de passe ferme les connexions en cours
    store.add_login("jeton", alice, days=1)
    assert store.login_user("jeton").username == "Alice"
    store.update_user(alice, password_hash="h2")
    assert store.login_user("jeton") is None

    mine = store.create_session(MEETING_URL, "IMV - A", group_id=sre, created_by="Alice")
    store.create_session(MEETING_URL, "IMV - B", group_id=dba)
    store.create_session(MEETING_URL, "IMV - C")
    assert [s["id"] for s in store.list_sessions([sre])] == [mine]
    assert len(store.list_sessions()) == 3 and store.list_sessions([]) == []
    store.delete_group(sre)
    assert store.session(mine)["group_id"] is None and set(store.user(alice).groups) == {dba}


def test_old_database_gets_the_new_columns(tmp_path):
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute(
        "CREATE TABLE sessions (id INTEGER PRIMARY KEY, incident_id INTEGER, meeting_url TEXT NOT NULL, "
        "display_name TEXT NOT NULL, status TEXT NOT NULL, error_code TEXT, created_at TEXT NOT NULL, "
        "joined_at TEXT, ended_at TEXT)"
    )
    old.execute("INSERT INTO sessions VALUES (1, NULL, 'u', 'n', 'ended', NULL, 't', NULL, NULL)")
    old.commit()
    old.close()
    store = Store(db)
    assert store.session(1)["group_id"] is None and store.session(1)["options"] is None


@pytest.fixture
def online(tmp_path):
    """Serveur avec --auth : un admin, une opératrice et un lecteur du groupe SRE, un groupe DBA."""
    db = tmp_path / "imv.db"
    store = Store(db)
    ids = {"SRE": store.create_group("SRE"), "DBA": store.create_group("DBA")}
    for name, role, groups in (("admin", "admin", []), ("alice", "operator", ["SRE"]), ("victor", "viewer", ["SRE"])):
        ids[name] = store.create_user(name, hash_password(PASSWORD), role, [ids[g] for g in groups])
    ids["sre_meeting"] = store.create_session(MEETING_URL, "IMV - SRE", group_id=ids["SRE"])
    ids["dba_meeting"] = store.create_session(MEETING_URL, "IMV - DBA", group_id=ids["DBA"])
    ids["no_group"] = store.create_session(MEETING_URL, "IMV - Sans groupe")
    store.close()
    app = create_app(db, MeetingConfig(url="", debug_dir=None), allowed_hosts=["testserver"], auth=True)
    with TestClient(app) as client:
        yield client, ids


def _login(client: TestClient, username: str, password: str = PASSWORD):
    return client.post("/api/login", json={"username": username, "password": password})


def _token(client: TestClient, username: str, password: str = PASSWORD) -> str:
    """Connexion distincte (cookie), pour simuler un autre navigateur avec le même client."""
    client.cookies.clear()
    _login(client, username, password)
    return client.cookies[COOKIE]


def _use(client: TestClient, token: str) -> TestClient:
    client.cookies.clear()
    client.cookies.set(COOKIE, token)
    return client


def test_login_is_required(online):
    client, _ = online
    assert client.get("/api/meetings").status_code == 401
    assert client.get("/api/me").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "Mot de passe" in client.get("/login").text

    assert _login(client, "alice", "mauvais mot de passe").status_code == 401
    assert _login(client, "inconnu").status_code == 401
    r = _login(client, "Alice")  # identifiant insensible à la casse
    assert r.status_code == 200 and r.json()["role"] == "operator"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/me").json()["username"] == "alice"
    assert client.get("/login", follow_redirects=False).status_code == 303

    client.post("/api/logout")
    assert client.get("/api/me").status_code == 401


def test_too_many_failures_block_the_login(online):
    client, _ = online
    for _ in range(LoginThrottle.MAX_FAILURES):
        _login(client, "alice", "mauvais mot de passe")
    assert _login(client, "alice").status_code == 429


def test_members_only_see_the_meetings_of_their_groups(online):
    client, ids = online
    _login(client, "victor")
    assert [m["name"] for m in client.get("/api/meetings").json()] == ["IMV - SRE"]
    assert client.get(f"/api/meetings/{ids['sre_meeting']}/feed").status_code == 200
    assert client.get(f"/api/meetings/{ids['dba_meeting']}/feed").status_code == 404
    assert client.get(f"/api/meetings/{ids['no_group']}/feed").status_code == 404

    _login(client, "admin")
    assert len(client.get("/api/meetings").json()) == 3


def test_viewers_cannot_act(online):
    client, ids = online
    _login(client, "victor")
    assert client.post("/api/meetings", json={"url": MEETING_URL}).status_code == 403
    assert client.post(f"/api/meetings/{ids['sre_meeting']}/restart").status_code == 403
    assert client.delete(f"/api/meetings/{ids['sre_meeting']}").status_code == 403
    assert client.post(f"/api/meetings/{ids['sre_meeting']}/chat", json={"text": "x"}).status_code == 403
    assert client.get("/api/users").status_code == 403


def test_operators_launch_in_their_groups_only(online):
    client, ids = online
    _login(client, "alice")
    assert [g["name"] for g in client.get("/api/options").json()["groups"]] == ["SRE"]
    # Droits vérifiés avant la validation du lien : 400 « Teams » = demande acceptée
    r = client.post("/api/meetings", json={"url": "https://example.com/x"})
    assert r.status_code == 400 and "Teams" in r.json()["detail"]
    r = client.post("/api/meetings", json={"url": "https://example.com/x", "group_id": ids["DBA"]})
    assert r.status_code == 403
    assert client.post(f"/api/meetings/{ids['dba_meeting']}/restart").status_code == 404
    assert client.delete(f"/api/meetings/{ids['sre_meeting']}").status_code == 409  # visible, mais terminée
    assert client.get("/api/users").status_code == 403


def test_operator_without_group_cannot_launch(online):
    client, ids = online
    _login(client, "admin")
    client.patch(f"/api/users/{ids['alice']}", json={"group_ids": []})
    _login(client, "alice")
    r = client.post("/api/meetings", json={"url": MEETING_URL})
    assert r.status_code == 400 and "aucun groupe" in r.json()["detail"]


def test_admin_manages_users_and_groups(online):
    client, ids = online
    _login(client, "admin")
    r = client.post("/api/users", json={"username": "bob", "password": "court", "role": "operator"})
    assert r.status_code == 400 and "trop court" in r.json()["detail"]
    r = client.post("/api/users", json={"username": "bob", "password": PASSWORD, "role": "chef"})
    assert r.status_code == 400
    r = client.post("/api/users", json={"username": "bob", "password": PASSWORD, "role": "operator",
                                        "group_ids": [ids["DBA"]]})
    assert r.status_code == 201 and r.json()["groups"] == [{"id": ids["DBA"], "name": "DBA"}]
    assert client.post("/api/users", json={"username": "Bob", "password": PASSWORD}).status_code == 400
    bob = r.json()["id"]

    group = client.post("/api/groups", json={"name": "Réseau"}).json()
    assert client.patch(f"/api/users/{bob}", json={"group_ids": [group["id"]], "role": "viewer"}).status_code == 200
    assert {g["name"]: g["members"] for g in client.get("/api/groups").json()}["Réseau"] == 1
    assert client.delete(f"/api/groups/{group['id']}").status_code == 200
    assert client.patch(f"/api/users/{bob}", json={"group_ids": [999]}).status_code == 400

    # Le dernier administrateur ne peut ni se supprimer, ni être rétrogradé
    assert client.delete(f"/api/users/{ids['admin']}").status_code == 409
    assert client.patch(f"/api/users/{ids['admin']}", json={"role": "operator"}).status_code == 409
    assert client.delete(f"/api/users/{bob}").status_code == 200
    assert client.delete(f"/api/users/{bob}").status_code == 404

    # Mot de passe changé par l'admin : l'utilisateur est déconnecté
    admin, alice = client.cookies[COOKIE], _token(client, "alice")
    _use(client, admin).patch(f"/api/users/{ids['alice']}", json={"password": "nouveau mot de passe"})
    assert _use(client, alice).get("/api/me").status_code == 401
    assert _login(client, "alice", "nouveau mot de passe").status_code == 200


def test_user_changes_own_password(online):
    client, _ = online
    other, mine = _token(client, "alice"), _token(client, "alice")
    r = client.post("/api/me/password", json={"current": "faux", "new": "nouveau mot de passe"})
    assert r.status_code == 400
    r = client.post("/api/me/password", json={"current": PASSWORD, "new": "nouveau mot de passe"})
    assert r.status_code == 200
    assert client.cookies[COOKIE] != mine  # nouveau jeton pour cette connexion, qui reste ouverte…
    assert client.get("/api/me").status_code == 200
    assert _use(client, other).get("/api/me").status_code == 401  # … les autres sont fermées


def test_requests_from_another_site_are_refused(online):
    client, _ = online
    r = client.post("/api/login", json={"username": "alice", "password": PASSWORD},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/login", json={"username": "alice", "password": PASSWORD},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_local_server_without_auth_keeps_full_access():
    app = create_app(":memory:", MeetingConfig(url="", debug_dir=None), allowed_hosts=["testserver"])
    with TestClient(app) as client:
        me = client.get("/api/me").json()
        assert me["role"] == "admin" and me["auth"] is False
        assert client.post("/api/login", json={"username": "x", "password": "y"}).status_code == 404


def test_user_and_group_commands(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "imv.db")
    monkeypatch.setattr(imo.cli.getpass, "getpass", lambda prompt="": PASSWORD)
    assert main(["user", "add", "erik", "--role", "admin", "--group", "SRE", "--db", db]) == 0
    assert "Groupe SRE créé" in capsys.readouterr().out
    assert main(["user", "add", "alice", "--group", "SRE", "--db", db]) == 0
    assert main(["user", "add", "alice", "--db", db]) == 1
    assert main(["user", "set", "alice", "--role", "viewer", "--group", "DBA", "--db", db]) == 0
    assert main(["user", "del", "erik", "--db", db]) == 1  # dernier administrateur
    assert main(["group", "add", "Réseau", "--db", db]) == 0
    capsys.readouterr()
    assert main(["user", "list", "--db", db]) == 0
    listing = capsys.readouterr().out
    assert "erik" in listing and "administrateur" in listing and "alice" in listing and "DBA" in listing
    assert main(["group", "list", "--db", db]) == 0
    assert "Réseau" in capsys.readouterr().out
    assert main(["group", "del", "Réseau", "--db", db]) == 0

    store = Store(db)
    user = store.user_by_name("alice")
    assert user.role == "viewer" and list(user.groups.values()) == ["DBA"]
    assert verify_password(PASSWORD, store.password_hash(user.id))
    store.close()

    monkeypatch.setattr(imo.cli.getpass, "getpass", lambda prompt="": "court")
    assert main(["user", "passwd", "alice", "--db", db]) == 1


def test_serve_refuses_unsafe_setups(tmp_path, capsys):
    db = str(tmp_path / "imv.db")
    assert main(["serve", "--host", "0.0.0.0", "--db", db]) == 1
    assert "--auth" in capsys.readouterr().err
    assert main(["serve", "--auth", "--db", db]) == 1
    assert "aucun administrateur" in capsys.readouterr().err


def test_serve_requires_login_as_soon_as_a_user_exists(tmp_path, monkeypatch, capsys):
    import uvicorn

    started = []
    monkeypatch.setattr(imo.server, "create_app", lambda db, defaults, allowed_hosts, auth: started.append(auth))

    async def serve(self):
        return None

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    db = str(tmp_path / "imv.db")
    assert main(["serve", "--db", db]) == 0 and started[-1] is False  # base sans compte : poste local
    store = Store(db)
    viewer = store.create_user("paul", hash_password(PASSWORD), "viewer")
    store.close()
    assert main(["serve", "--db", db]) == 1  # connexion activée d'office, mais personne pour administrer
    assert "aucun administrateur" in capsys.readouterr().err
    store = Store(db)
    store.update_user(viewer, role="admin")
    store.close()
    assert main(["serve", "--db", db]) == 0 and started[-1] is True
    assert main(["serve", "--no-auth", "--db", db]) == 0 and started[-1] is False
    assert main(["serve", "--no-auth", "--host", "0.0.0.0", "--db", db]) == 1


def test_api_tokens(online):
    client, ids = online
    _login(client, "alice")
    r = client.post("/api/tokens", json={"name": "script astreinte", "days": 30})
    assert r.status_code == 201
    token = r.json()["token"]
    assert token.startswith("imv_") and r.json()["prefix"] == token[:8]
    assert "token" not in client.get("/api/tokens").json()[0]  # jamais réaffiché

    api = {"Authorization": f"Bearer {token}"}
    client.cookies.clear()
    assert client.get("/api/me", headers=api).json()["username"] == "alice"
    assert [m["name"] for m in client.get("/api/meetings", headers=api).json()] == ["IMV - SRE"]
    assert client.get(f"/api/meetings/{ids['sre_meeting']}", headers=api).json()["group"] == "SRE"
    assert client.get(f"/api/meetings/{ids['dba_meeting']}", headers=api).status_code == 404
    r = client.post("/api/meetings", json={"url": "https://example.com/x"}, headers=api)
    assert r.status_code == 400 and "Teams" in r.json()["detail"]  # droits d'opératrice
    # Un jeton ne crée pas d'autres jetons ; un faux jeton est refusé (sans repli sur le cookie)
    assert client.post("/api/tokens", json={"name": "x"}, headers=api).status_code == 403
    assert client.get("/api/me", headers={"Authorization": "Bearer imv_faux"}).status_code == 401

    token_id = client.get("/api/tokens", headers=api).json()[0]["id"]
    assert client.delete(f"/api/tokens/{token_id}", headers=api).status_code == 200
    assert client.get("/api/me", headers=api).status_code == 401


def test_viewer_and_expired_tokens(online, tmp_path):
    client, ids = online
    _login(client, "admin")
    # Un administrateur crée le jeton d'un autre utilisateur (compte de service)
    r = client.post("/api/tokens", json={"name": "tableau de bord", "user_id": ids["victor"]})
    assert r.status_code == 201 and r.json()["username"] == "victor" and r.json()["expires_at"] is None
    viewer = {"Authorization": f"Bearer {r.json()['token']}"}
    assert len(client.get("/api/tokens?all=true").json()) == 1
    client.cookies.clear()
    assert client.get("/api/meetings", headers=viewer).status_code == 200
    assert client.post("/api/meetings", json={"url": MEETING_URL}, headers=viewer).status_code == 403
    assert client.get("/api/tokens?all=true", headers=viewer).status_code == 403

    store = Store(tmp_path / "imv.db")
    store.db.execute("UPDATE api_tokens SET expires_at = '2000-01-01T00:00:00+00:00'")
    store.db.commit()
    store.close()
    assert client.get("/api/meetings", headers=viewer).status_code == 401


def test_api_documentation_requires_login(online):
    client, _ = online
    assert client.get("/api/openapi.json").status_code == 401
    assert client.get("/api/docs", follow_redirects=False).status_code == 303
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404
    _login(client, "victor")
    assert "swagger" in client.get("/api/docs").text.lower()
    schema = client.get("/api/openapi.json").json()
    for path in ("/api/meetings", "/api/meetings/{session_id}/command", "/api/meetings/{session_id}/analyze",
                 "/api/tokens", "/api/users"):
        assert path in schema["paths"], path
    assert "/login" not in schema["paths"]
    assert any(s.get("scheme") == "bearer" for s in schema["components"]["securitySchemes"].values())


class FakeLLM:
    name = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return "Chronologie : 10:02 erreurs 500"


def test_analyze_a_finished_meeting(online, tmp_path, monkeypatch):
    client, ids = online
    llm = FakeLLM()
    monkeypatch.setattr(imo.server, "make_llm", lambda name, model=None: llm)
    store = Store(tmp_path / "imv.db")
    store.add_message(ids["sre_meeting"], ChatMessage(sender="Bob", text="Erreurs 500 sur le paiement", message_id="m1"))
    store.close()

    _login(client, "victor")
    assert client.post(f"/api/meetings/{ids['sre_meeting']}/analyze", json={}).status_code == 403
    _login(client, "alice")
    url = f"/api/meetings/{ids['sre_meeting']}/analyze"
    r = client.post(url, json={"command": "timeline"})
    assert r.status_code == 400 and "LLM" in r.json()["detail"]  # ni la session ni le serveur n'en ont

    store = Store(tmp_path / "imv.db")
    store.db.execute("UPDATE sessions SET options = ? WHERE id = ?", ('{"llm": "fake"}', ids["sre_meeting"]))
    store.db.commit()
    store.close()
    r = client.post(url, json={"command": "timeline", "language": "en"})
    assert r.status_code == 200, r.text
    assert r.json() == {"command": "timeline", "arg": "", "text": "Chronologie : 10:02 erreurs 500"}
    assert "Bob : Erreurs 500 sur le paiement" in llm.prompts[-1] and "Demande faite par alice" in llm.prompts[-1]
    r = client.post(url, json={"command": "com marketing"})
    assert r.status_code == 400 and "Audience" in r.json()["detail"]
    assert client.post(url, json={"command": "pause"}).status_code == 400  # pas une analyse
    assert client.post(f"/api/meetings/{ids['dba_meeting']}/analyze", json={}).status_code == 404
    # Commande à l'agent : seulement pendant la réunion
    r = client.post(f"/api/meetings/{ids['sre_meeting']}/command", json={"command": "résumé"})
    assert r.status_code == 409


def test_token_commands(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "imv.db")
    monkeypatch.setattr(imo.cli.getpass, "getpass", lambda prompt="": PASSWORD)
    main(["user", "add", "supervision", "--group", "SRE", "--db", db])
    capsys.readouterr()
    assert main(["token", "add", "supervision", "--name", "astreinte", "--days", "30", "--db", db]) == 0
    token = capsys.readouterr().out.splitlines()[-1]
    assert token.startswith("imv_")
    assert main(["token", "add", "inconnu", "--name", "x", "--db", db]) == 1
    assert main(["token", "list", "--db", db]) == 0
    listing = capsys.readouterr().out
    assert "astreinte" in listing and token[:8] in listing and token not in listing

    store = Store(db)
    user, token_id = store.api_token_user(token_hash(token))
    assert user.username == "supervision" and user.groups
    store.close()
    assert main(["token", "del", str(token_id), "--db", db]) == 0
    assert main(["token", "del", str(token_id), "--db", db]) == 1
    assert Store(db).api_token_user(token_hash(token)) is None


def test_prometheus_metrics(online, tmp_path):
    from imo import metrics

    client, ids = online
    metrics.reset()
    metrics.record_llm("claude-cli:opus", 2.5, ok=True)
    metrics.record_llm("claude-cli:opus", 0.5, ok=False)
    store = Store(tmp_path / "imv.db")
    store.add_message(ids["sre_meeting"], ChatMessage(sender="Bob", text="Erreurs 500", message_id="m1"))
    store.set_session_status(ids["dba_meeting"], "failed", "rejected")
    store.close()

    assert client.get("/api/metrics").status_code == 401
    _login(client, "victor")  # un lecteur suffit
    token = client.post("/api/tokens", json={"name": "prometheus"}).json()["token"]
    client.cookies.clear()
    r = client.get("/api/metrics", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    text = r.text
    for line in (
        'imv_agents{status="in_meeting"} 0',
        'imv_sessions{status="joining"} 2',
        'imv_sessions{status="failed"} 1',
        'imv_session_failures{error_code="rejected"} 1',
        'imv_messages{source="chat"} 1',
        'imv_users{role="viewer"} 1',
        'imv_llm_requests_total{llm="claude-cli:opus",outcome="ok"} 1',
        'imv_llm_requests_total{llm="claude-cli:opus",outcome="error"} 1',
        'imv_llm_request_seconds_total{llm="claude-cli:opus"} 3',
    ):
        assert line in text, line
    assert "# TYPE imv_llm_requests_total counter" in text
    metrics.reset()


def test_llm_calls_are_counted(monkeypatch):
    from imo import metrics
    from imo.llm.providers import LLMError, Timed

    class Failing:
        name = "faux"

        async def complete(self, system, prompt):
            raise LLMError("quota")

    class Working:
        name = "faux"

        async def complete(self, system, prompt):
            return "OK"

    metrics.reset()
    import asyncio

    assert asyncio.run(Timed(Working()).complete("s", "p")) == "OK"
    with pytest.raises(LLMError):
        asyncio.run(Timed(Failing()).complete("s", "p"))
    text = metrics.render(Store(":memory:"), [])
    assert 'imv_llm_requests_total{llm="faux",outcome="ok"} 1' in text
    assert 'imv_llm_requests_total{llm="faux",outcome="error"} 1' in text
    metrics.reset()
