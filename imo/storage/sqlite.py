"""Persistance SQLite : sessions, incidents, messages et interventions de l'agent."""

from __future__ import annotations

import sqlite3
from pathlib import Path

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
    ended_at     TEXT
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
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- sessions ---------------------------------------------------------

    def create_session(self, meeting_url: str, display_name: str) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO sessions (meeting_url, display_name, status, created_at) "
                "VALUES (?, ?, 'joining', ?)",
                (meeting_url, display_name, utc_now()),
            )
        return cur.lastrowid

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

    def list_sessions(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT s.id, s.created_at, s.joined_at, s.ended_at, s.status, s.error_code, s.meeting_url,
                      s.display_name,
                      i.ref AS incident_ref,
                      (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count
               FROM sessions s LEFT JOIN incidents i ON i.id = s.incident_id
               ORDER BY s.id"""
        ).fetchall()

    def session(self, session_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            """SELECT s.*, i.ref AS incident_ref FROM sessions s
               LEFT JOIN incidents i ON i.id = s.incident_id WHERE s.id = ?""",
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
