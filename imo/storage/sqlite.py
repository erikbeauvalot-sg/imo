"""Persistance SQLite : sessions, incidents, messages et interventions de l'agent ; utilisateurs,
groupes et connexions du serveur web."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from imo.auth import User
from imo.models import ChatMessage, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY,
    ref         TEXT,
    status      TEXT NOT NULL DEFAULT 'open',
    started_at  TEXT NOT NULL,
    closed_at   TEXT,
    meeting_url TEXT,
    im_name     TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY,
    incident_id  INTEGER REFERENCES incidents(id),
    meeting_url  TEXT NOT NULL,
    display_name TEXT NOT NULL,
    status       TEXT NOT NULL,          -- joining | in_meeting | ended | failed
    error_code   TEXT,
    created_at   TEXT NOT NULL,
    joined_at    TEXT,
    ended_at     TEXT,
    options      TEXT,                   -- JSON : options de lancement, pour relancer la session
    group_id     INTEGER REFERENCES groups(id) ON DELETE SET NULL,  -- NULL : visible des admins seuls
    created_by   TEXT                    -- utilisateur du serveur web qui a lancé l'agent
);
CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY,
    session_id       INTEGER NOT NULL REFERENCES sessions(id),
    incident_id      INTEGER REFERENCES incidents(id),
    source           TEXT NOT NULL,      -- chat | system | transcript (V2)
    teams_message_id TEXT,
    author_name      TEXT NOT NULL,
    text             TEXT NOT NULL,
    sent_at          TEXT,
    captured_at      TEXT NOT NULL,
    UNIQUE (session_id, teams_message_id)
);
CREATE TABLE IF NOT EXISTS interventions (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    incident_id INTEGER REFERENCES incidents(id),
    type        TEXT NOT NULL,           -- welcome | command | proactive | manual (interface web) | report
    rule_id     TEXT,
    content     TEXT NOT NULL,
    delivered   INTEGER NOT NULL,        -- 0 si le chat est en lecture seule
    timestamp   TEXT NOT NULL
);
-- Utilisateurs du serveur web (voir imo/auth.py)
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,         -- admin | operator | viewer
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS groups (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_groups (
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, group_id)
);
CREATE TABLE IF NOT EXISTS logins (
    token_hash TEXT PRIMARY KEY,         -- empreinte SHA-256 du jeton du cookie
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,          -- usage, choisi par l'utilisateur (« supervision », « script astreinte »)
    token_hash   TEXT NOT NULL UNIQUE,   -- empreinte SHA-256 du jeton, montré une seule fois
    prefix       TEXT NOT NULL,          -- début du jeton, pour le reconnaître dans la liste
    created_at   TEXT NOT NULL,
    expires_at   TEXT,                   -- NULL : sans expiration
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS telegram_chats (
    chat_id INTEGER PRIMARY KEY,         -- groupe Telegram où le bot est présent (lien telegram:<id>)
    title   TEXT NOT NULL,
    seen_at TEXT NOT NULL
);
"""

# Colonnes ajoutées après coup à des bases existantes
_ADDED_COLUMNS = {
    "options": "TEXT",
    "group_id": "INTEGER REFERENCES groups(id) ON DELETE SET NULL",
    "created_by": "TEXT",
}


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(sessions)")}
        for name, definition in _ADDED_COLUMNS.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")

    def close(self) -> None:
        self.db.close()

    # -- sessions ---------------------------------------------------------

    def create_session(
        self,
        meeting_url: str,
        display_name: str,
        options: dict | None = None,
        group_id: int | None = None,
        created_by: str | None = None,
    ) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO sessions (meeting_url, display_name, status, created_at, options, group_id, created_by) "
                "VALUES (?, ?, 'joining', ?, ?, ?, ?)",
                (
                    meeting_url,
                    display_name,
                    utc_now(),
                    json.dumps(options) if options is not None else None,
                    group_id,
                    created_by,
                ),
            )
        return cur.lastrowid

    def session_options(self, session_id: int) -> dict | None:
        """Options de lancement enregistrées ; None pour une session antérieure à leur enregistrement."""
        row = self.db.execute("SELECT options FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return json.loads(row["options"]) if row and row["options"] else None

    def set_session_status(self, session_id: int, status: str, error_code: str | None = None) -> None:
        now = utc_now()
        with self.db:
            self.db.execute(
                """UPDATE sessions SET status = ?, error_code = ?,
                       joined_at = CASE WHEN ? = 'in_meeting' THEN ? ELSE joined_at END,
                       ended_at  = CASE WHEN ? IN ('ended', 'failed') THEN ? ELSE ended_at END
                   WHERE id = ?""",
                (status, error_code, status, now, status, now, session_id),
            )

    def list_sessions(self, group_ids: list[int] | None = None) -> list[sqlite3.Row]:
        """Toutes les sessions, ou seulement celles des groupes donnés."""
        where, params = "", ()
        if group_ids is not None:
            where = f"WHERE s.group_id IN ({','.join('?' * len(group_ids))})"
            params = tuple(group_ids)
        return self.db.execute(
            f"""SELECT s.id, s.created_at, s.joined_at, s.ended_at, s.status, s.error_code, s.meeting_url,
                      s.display_name, s.group_id, s.created_by,
                      i.ref AS incident_ref, g.name AS group_name,
                      (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count
               FROM sessions s LEFT JOIN incidents i ON i.id = s.incident_id
                               LEFT JOIN groups g ON g.id = s.group_id
               {where}
               ORDER BY s.id""",
            params,
        ).fetchall()

    def session(self, session_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            """SELECT s.*, i.ref AS incident_ref, g.name AS group_name FROM sessions s
               LEFT JOIN incidents i ON i.id = s.incident_id
               LEFT JOIN groups g ON g.id = s.group_id WHERE s.id = ?""",
            (session_id,),
        ).fetchone()

    # -- incidents --------------------------------------------------------

    def start_incident(self, session_id: int, ref: str | None, im_name: str | None) -> int:
        """Crée l'incident, y rattache la session et les messages déjà captés."""
        with self.db:
            cur = self.db.execute(
                "INSERT INTO incidents (ref, started_at, meeting_url, im_name) "
                "SELECT ?, ?, meeting_url, ? FROM sessions WHERE id = ?",
                (ref, utc_now(), im_name, session_id),
            )
            incident_id = cur.lastrowid
            self.db.execute("UPDATE sessions SET incident_id = ? WHERE id = ?", (incident_id, session_id))
            self.db.execute(
                "UPDATE messages SET incident_id = ? WHERE session_id = ? AND incident_id IS NULL",
                (incident_id, session_id),
            )
        return incident_id

    def close_incident(self, session_id: int) -> None:
        """Marque clos l'incident suivi par la session (sans effet si aucun n'a été démarré)."""
        with self.db:
            self.db.execute(
                """UPDATE incidents SET status = 'closed', closed_at = ?
                   WHERE id = (SELECT incident_id FROM sessions WHERE id = ?)""",
                (utc_now(), session_id),
            )

    def session_incident(self, session_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT i.* FROM incidents i JOIN sessions s ON s.incident_id = i.id WHERE s.id = ?",
            (session_id,),
        ).fetchone()

    # -- messages ---------------------------------------------------------

    def add_message(self, session_id: int, msg: ChatMessage) -> bool:
        """Enregistre le message ; False s'il l'était déjà (même identifiant Teams)."""
        with self.db:
            cur = self.db.execute(
                """INSERT OR IGNORE INTO messages
                       (session_id, incident_id, source, teams_message_id, author_name, text, sent_at, captured_at)
                   SELECT id, incident_id, ?, ?, ?, ?, ?, ? FROM sessions WHERE id = ?""",
                (msg.source, msg.message_id, msg.sender, msg.text, msg.sent_at, msg.captured_at, session_id),
            )
        return cur.rowcount == 1

    def messages(self, session_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()

    def count_messages(self, session_id: int) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()[0]

    # -- interventions ----------------------------------------------------

    def add_intervention(
        self, session_id: int, kind: str, content: str, delivered: bool, rule_id: str | None = None
    ) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO interventions (session_id, incident_id, type, rule_id, content, delivered, timestamp)
                   SELECT id, incident_id, ?, ?, ?, ?, ? FROM sessions WHERE id = ?""",
                (kind, rule_id, content, int(delivered), utc_now(), session_id),
            )

    def interventions(self, session_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM interventions WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()

    # -- utilisateurs et groupes du serveur web ----------------------------

    def _groups_of(self, user_id: int) -> dict[int, str]:
        return {
            row["id"]: row["name"]
            for row in self.db.execute(
                "SELECT g.id, g.name FROM groups g JOIN user_groups ug ON ug.group_id = g.id WHERE ug.user_id = ?",
                (user_id,),
            )
        }

    def _user(self, row: sqlite3.Row | None) -> User | None:
        return User(row["id"], row["username"], row["role"], self._groups_of(row["id"])) if row else None

    def create_user(self, username: str, password_hash: str, role: str, group_ids: list[int] = ()) -> int:
        try:
            with self.db:
                cur = self.db.execute(
                    "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                    (username, password_hash, role, utc_now()),
                )
                self._set_groups(cur.lastrowid, group_ids)
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"l'utilisateur {username} existe déjà, ou groupe inconnu") from exc
        return cur.lastrowid

    def _set_groups(self, user_id: int, group_ids: list[int]) -> None:
        self.db.execute("DELETE FROM user_groups WHERE user_id = ?", (user_id,))
        self.db.executemany(
            "INSERT INTO user_groups (user_id, group_id) VALUES (?, ?)", [(user_id, g) for g in set(group_ids)]
        )

    def update_user(
        self,
        user_id: int,
        *,
        role: str | None = None,
        password_hash: str | None = None,
        group_ids: list[int] | None = None,
    ) -> None:
        """Un nouveau mot de passe déconnecte toutes les sessions de l'utilisateur."""
        try:
            with self.db:
                if role is not None:
                    self.db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
                if password_hash is not None:
                    self.db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
                    self.db.execute("DELETE FROM logins WHERE user_id = ?", (user_id,))
                if group_ids is not None:
                    self._set_groups(user_id, group_ids)
        except sqlite3.IntegrityError as exc:
            raise ValueError("groupe inconnu") from exc

    def delete_user(self, user_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def user(self, user_id: int) -> User | None:
        return self._user(self.db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())

    def user_by_name(self, username: str) -> User | None:
        return self._user(self.db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())

    def password_hash(self, user_id: int) -> str | None:
        row = self.db.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        return row["password_hash"] if row else None

    def users(self) -> list[User]:
        return [self._user(row) for row in self.db.execute("SELECT * FROM users ORDER BY username")]

    def count_admins(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]

    def create_group(self, name: str) -> int:
        try:
            with self.db:
                cur = self.db.execute("INSERT INTO groups (name, created_at) VALUES (?, ?)", (name, utc_now()))
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"le groupe {name} existe déjà") from exc
        return cur.lastrowid

    def groups(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT g.id, g.name,
                      (SELECT COUNT(*) FROM user_groups ug WHERE ug.group_id = g.id) AS members,
                      (SELECT COUNT(*) FROM sessions s WHERE s.group_id = g.id) AS sessions
               FROM groups g ORDER BY g.name"""
        ).fetchall()

    def group(self, group_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()

    def group_by_name(self, name: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM groups WHERE name = ?", (name,)).fetchone()

    def delete_group(self, group_id: int) -> None:
        """Ses réunions restent, sans groupe : visibles des admins seuls."""
        with self.db:
            self.db.execute("DELETE FROM groups WHERE id = ?", (group_id,))

    # -- connexions au serveur web ------------------------------------------

    def add_login(self, token_hash: str, user_id: int, days: int) -> None:
        now = datetime.now(timezone.utc)
        with self.db:
            self.db.execute("DELETE FROM logins WHERE expires_at < ?", (now.isoformat(timespec="seconds"),))
            self.db.execute(
                "INSERT INTO logins (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token_hash, user_id, utc_now(), (now + timedelta(days=days)).isoformat(timespec="seconds")),
            )

    def login_user(self, token_hash: str) -> User | None:
        """Utilisateur d'une connexion en cours ; None si le jeton est inconnu ou expiré."""
        row = self.db.execute(
            "SELECT u.* FROM logins l JOIN users u ON u.id = l.user_id WHERE l.token_hash = ? AND l.expires_at >= ?",
            (token_hash, utc_now()),
        ).fetchone()
        return self._user(row)

    def delete_login(self, token_hash: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM logins WHERE token_hash = ?", (token_hash,))

    # -- jetons d'API ---------------------------------------------------------

    def add_api_token(self, user_id: int, name: str, token_hash: str, prefix: str, days: int | None) -> int:
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(days=days)).isoformat(timespec="seconds") if days else None
        with self.db:
            cur = self.db.execute(
                "INSERT INTO api_tokens (user_id, name, token_hash, prefix, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, name, token_hash, prefix, utc_now(), expires),
            )
        return cur.lastrowid

    def api_tokens(self, user_id: int | None = None) -> list[sqlite3.Row]:
        """Jetons d'un utilisateur, ou de tous ; sans leur empreinte."""
        where, params = ("WHERE t.user_id = ?", (user_id,)) if user_id is not None else ("", ())
        return self.db.execute(
            f"""SELECT t.id, t.user_id, u.username, t.name, t.prefix, t.created_at, t.expires_at, t.last_used_at
                FROM api_tokens t JOIN users u ON u.id = t.user_id {where} ORDER BY t.id""",
            params,
        ).fetchall()

    def api_token(self, token_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT id, user_id, name FROM api_tokens WHERE id = ?", (token_id,)).fetchone()

    def api_token_user(self, token_hash: str) -> tuple[User, int] | None:
        """(utilisateur, id du jeton) pour un jeton valide ; None s'il est inconnu ou expiré."""
        now = utc_now()
        row = self.db.execute(
            """SELECT u.*, t.id AS token_id, t.last_used_at FROM api_tokens t JOIN users u ON u.id = t.user_id
               WHERE t.token_hash = ? AND (t.expires_at IS NULL OR t.expires_at >= ?)""",
            (token_hash, now),
        ).fetchone()
        if row is None:
            return None
        if (row["last_used_at"] or "")[:16] != now[:16]:  # au plus une écriture par minute
            with self.db:
                self.db.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (now, row["token_id"]))
        return self._user(row), row["token_id"]

    def delete_api_token(self, token_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))

    # -- groupes Telegram ------------------------------------------------------

    def save_telegram_chat(self, chat_id: int, title: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO telegram_chats (chat_id, title, seen_at) VALUES (?, ?, ?) "
                "ON CONFLICT (chat_id) DO UPDATE SET title = excluded.title, seen_at = excluded.seen_at",
                (chat_id, title, utc_now()),
            )

    def forget_telegram_chat(self, chat_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM telegram_chats WHERE chat_id = ?", (chat_id,))

    def telegram_chats(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT chat_id, title FROM telegram_chats ORDER BY title").fetchall()
