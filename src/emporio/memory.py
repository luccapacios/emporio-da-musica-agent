"""Conversation persistence: one SQLite row per session with the full message list.

Tool results are kept in the history, so an order verified earlier in the chat does not
have to be verified again, and the model can refer back to what it looked up.

The web chat also keeps a small index per session (owner, title, preview, dates) so a
browser can list and reopen its own conversations. The owner is an unguessable id the
browser generates; there are no accounts, so that id is what keeps one browser from
listing or reading another's conversations.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def trim_history(messages: list[dict], max_user_turns: int) -> list[dict]:
    """Keep the last N user turns. Cutting at a user message never splits a tool call pair."""
    user_positions = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(user_positions) <= max_user_turns:
        return messages
    return messages[user_positions[-max_user_turns]:]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ConversationStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS conversations ("
                "session_id TEXT PRIMARY KEY, messages TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS conversation_index ("
                "session_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, title TEXT NOT NULL, "
                "preview TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_index_client ON conversation_index (client_id, updated_at)")

    def _connect(self) -> sqlite3.Connection:
        # A connection per operation keeps this safe under FastAPI's thread pool.
        return sqlite3.connect(self.path)

    # ---- messages -------------------------------------------------------------------

    def load(self, session_id: str) -> list[dict]:
        with self._connect() as db:
            row = db.execute("SELECT messages FROM conversations WHERE session_id = ?", (session_id,)).fetchone()
        return json.loads(row[0]) if row else []

    def exists(self, session_id: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM conversations WHERE session_id = ?", (session_id,)).fetchone() is not None

    def save(self, session_id: str, messages: list[dict]) -> None:
        payload = json.dumps(messages, ensure_ascii=False)
        with self._connect() as db:
            db.execute(
                "INSERT INTO conversations (session_id, messages, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET messages = excluded.messages, updated_at = excluded.updated_at",
                (session_id, payload, _now()),
            )

    def clear(self, session_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))
            db.execute("DELETE FROM conversation_index WHERE session_id = ?", (session_id,))

    # ---- index for the web chat -----------------------------------------------------

    def owner(self, session_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT client_id FROM conversation_index WHERE session_id = ?", (session_id,)).fetchone()
        return row[0] if row else None

    def touch(self, session_id: str, client_id: str, title: str, preview: str) -> None:
        """Record a turn. The title is set once (from the first message) unless renamed."""
        stamp = _now()
        with self._connect() as db:
            db.execute(
                "INSERT INTO conversation_index (session_id, client_id, title, preview, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "preview = excluded.preview, updated_at = excluded.updated_at",
                (session_id, client_id, title, preview, stamp, stamp),
            )

    def rename(self, session_id: str, title: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE conversation_index SET title = ? WHERE session_id = ?", (title, session_id))

    def list_for(self, client_id: str, limit: int = 50) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT session_id, title, preview, created_at, updated_at FROM conversation_index "
                "WHERE client_id = ? ORDER BY updated_at DESC LIMIT ?",
                (client_id, limit),
            ).fetchall()
        keys = ("session_id", "title", "preview", "created_at", "updated_at")
        return [dict(zip(keys, row)) for row in rows]
