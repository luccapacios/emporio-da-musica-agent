"""Conversation persistence: one SQLite row per session with the full message list.

Tool results are kept in the history, so an order verified earlier in the chat does not
have to be verified again, and the model can refer back to what it looked up.
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


class ConversationStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS conversations ("
                "session_id TEXT PRIMARY KEY, messages TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        # A connection per operation keeps this safe under FastAPI's thread pool.
        return sqlite3.connect(self.path)

    def load(self, session_id: str) -> list[dict]:
        with self._connect() as db:
            row = db.execute("SELECT messages FROM conversations WHERE session_id = ?", (session_id,)).fetchone()
        return json.loads(row[0]) if row else []

    def save(self, session_id: str, messages: list[dict]) -> None:
        payload = json.dumps(messages, ensure_ascii=False)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as db:
            db.execute(
                "INSERT INTO conversations (session_id, messages, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET messages = excluded.messages, updated_at = excluded.updated_at",
                (session_id, payload, stamp),
            )

    def clear(self, session_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))
