"""Conversation persistence: one SQLite row per session with the full message list.

Tool results are kept in the history, so an order verified earlier in the chat does not
have to be verified again, and the model can refer back to what it looked up.

The web chat also keeps a small index per session (owner, title, preview, dates, status,
protocol, rating) so a browser can list, search and reopen its own conversations. The owner
is an unguessable id the browser generates; there are no accounts, so that id is what keeps
one browser from listing or reading another's conversations. Only its sha256 is stored, so a
copy of the database cannot be used to impersonate a browser.

Per-answer votes live in a feedback table keyed by (session, message timestamp), so a new
vote replaces the old one. Conversations not updated for `retention_days` are purged.

Deleted and purged session ids are remembered (until the retention period runs out), so a
browser that still holds one is told the conversation is gone instead of silently starting
a new one under the same id.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .data import normalize

SEARCH_TEXT_LIMIT = 4000
PURGE_EVERY_SECONDS = 24 * 3600

# Columns added to conversation_index after the first version, with their SQL definitions.
# title_source: "auto" (taken from the customer's words), "smart" (from a tool) or "user" (renamed).
INDEX_COLUMNS = {
    "status": "TEXT NOT NULL DEFAULT 'open'",
    "protocol": "TEXT",
    "closed_at": "TEXT",
    "rating": "INTEGER",
    "rating_comment": "TEXT",
    "title_source": "TEXT NOT NULL DEFAULT 'auto'",
    "search_text": "TEXT NOT NULL DEFAULT ''",
}
LIST_KEYS = ("session_id", "title", "preview", "created_at", "updated_at", "status", "protocol", "rating", "closed_at")


def trim_history(messages: list[dict], max_user_turns: int) -> list[dict]:
    """Keep the last N user turns. Cutting at a user message never splits a tool call pair."""
    user_positions = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(user_positions) <= max_user_turns:
        return messages
    return messages[user_positions[-max_user_turns]:]


def owner_key(client_id: str) -> str:
    """What is stored for a browser's client id: its sha256, never the id itself."""
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_greeting(title: str) -> bool:
    return len(title.split()) <= 3


def _like(text: str) -> str:
    escaped = normalize(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class ConversationStore:
    def __init__(self, path: Path, retention_days: int | None = None):
        self.path = path
        self.retention_days = retention_days
        self._last_purge: float | None = None
        self._purge_lock = threading.Lock()
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
            db.execute(
                "CREATE TABLE IF NOT EXISTS feedback ("
                "session_id TEXT NOT NULL, message_ts TEXT NOT NULL, rating TEXT NOT NULL, reason TEXT, "
                "comment TEXT, created_at TEXT NOT NULL, PRIMARY KEY (session_id, message_ts))"
            )
            db.execute("CREATE TABLE IF NOT EXISTS deleted_sessions (session_id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL)")
            self._migrate(db)
        self._purge_if_due()

    def _connect(self) -> sqlite3.Connection:
        # A connection per operation keeps this safe under FastAPI's thread pool.
        db = sqlite3.connect(self.path)
        db.create_function("norm", 1, lambda text: normalize(text or ""), deterministic=True)
        return db

    def _migrate(self, db: sqlite3.Connection) -> None:
        present = {row[1] for row in db.execute("PRAGMA table_info(conversation_index)")}
        missing = [name for name in INDEX_COLUMNS if name not in present]
        for name in missing:
            db.execute(f"ALTER TABLE conversation_index ADD COLUMN {name} {INDEX_COLUMNS[name]}")
        if missing:
            # The first version stored raw client ids: hash them so those browsers keep their list.
            rows = db.execute("SELECT session_id, client_id FROM conversation_index").fetchall()
            for session_id, client_id in rows:
                if not re.fullmatch(r"[0-9a-f]{64}", client_id):
                    db.execute("UPDATE conversation_index SET client_id = ? WHERE session_id = ?",
                               (owner_key(client_id), session_id))

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

    def truncate_from_ts(self, session_id: str, ts: str, drop_votes: bool = True) -> list[dict] | None:
        """Drop the history from the last user message stamped `ts` on, for edit and regenerate.

        Returns the removed messages, or None when no user message has that timestamp. Votes
        on removed answers are deleted with them, unless `drop_votes` is False (the caller
        then drops them with drop_feedback once the new answer is saved).
        """
        messages = self.load(session_id)
        cut = next((i for i in range(len(messages) - 1, -1, -1)
                    if messages[i].get("role") == "user" and messages[i].get("ts") == ts), None)
        if cut is None:
            return None
        removed = messages[cut:]
        self.save(session_id, messages[:cut])
        if drop_votes:
            self.drop_feedback(session_id, removed)
        return removed

    def drop_feedback(self, session_id: str, messages: list[dict]) -> None:
        """Delete the votes on the answers among `messages`."""
        stamps = [m["ts"] for m in messages if m.get("role") == "assistant" and m.get("ts")]
        with self._connect() as db:
            db.executemany("DELETE FROM feedback WHERE session_id = ? AND message_ts = ?",
                           [(session_id, stamp) for stamp in stamps])

    def _forget(self, db: sqlite3.Connection, sessions: list[tuple[str]]) -> None:
        """Delete sessions with their index rows and votes, and remember that they were deleted."""
        for table in ("conversations", "conversation_index", "feedback"):
            db.executemany(f"DELETE FROM {table} WHERE session_id = ?", sessions)
        db.executemany("INSERT OR REPLACE INTO deleted_sessions (session_id, deleted_at) VALUES (?, ?)",
                       [(s, _now()) for (s,) in sessions])

    def clear(self, session_id: str) -> None:
        with self._connect() as db:
            self._forget(db, [(session_id,)])

    def clear_client(self, owner: str) -> None:
        """Delete every conversation of one browser, with its index rows and votes."""
        with self._connect() as db:
            self._forget(db, [(s,) for s in self._sessions_of(db, owner)])

    def sessions_of(self, owner: str) -> list[str]:
        with self._connect() as db:
            return self._sessions_of(db, owner)

    @staticmethod
    def _sessions_of(db: sqlite3.Connection, owner: str) -> list[str]:
        return [row[0] for row in db.execute("SELECT session_id FROM conversation_index WHERE client_id = ?", (owner,))]

    def was_deleted(self, session_id: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM deleted_sessions WHERE session_id = ?",
                              (session_id,)).fetchone() is not None

    def purge(self, older_than_days: int, now: datetime | None = None) -> int:
        """Delete conversations not updated for `older_than_days`. Returns how many went."""
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=older_than_days)
        stamp = cutoff.astimezone(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as db:
            old = {row[0] for row in db.execute("SELECT session_id FROM conversations WHERE updated_at < ?", (stamp,))}
            old |= {row[0] for row in db.execute(
                "SELECT session_id FROM conversation_index WHERE updated_at < ?", (stamp,))}
            db.execute("DELETE FROM deleted_sessions WHERE deleted_at < ?", (stamp,))
            self._forget(db, [(s,) for s in old])
        return len(old)

    def _purge_if_due(self) -> None:
        """Apply the retention period at startup and then at most once a day."""
        if not self.retention_days:
            return
        with self._purge_lock:
            if self._last_purge is not None and time.monotonic() - self._last_purge < PURGE_EVERY_SECONDS:
                return
            self._last_purge = time.monotonic()
        self.purge(self.retention_days)

    # ---- index for the web chat -----------------------------------------------------

    def owner(self, session_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT client_id FROM conversation_index WHERE session_id = ?", (session_id,)).fetchone()
        return row[0] if row else None

    def info(self, session_id: str) -> dict | None:
        """The index row of one conversation, with the same keys as list_for()."""
        with self._connect() as db:
            row = db.execute(f"SELECT {', '.join(LIST_KEYS)} FROM conversation_index WHERE session_id = ?",
                             (session_id,)).fetchone()
        return dict(zip(LIST_KEYS, row)) if row else None

    def touch(self, session_id: str, owner: str, title: str, preview: str, text: str = "", smart: bool = False) -> str:
        """Record a turn and return the conversation's title.

        The first turn sets the title. A later turn replaces it only while it is an automatic
        title from a short greeting ("oi", "boa tarde"): a title from a tool or from longer text
        is kept, and a renamed one is never touched. `text` (the turn's user and reply text) is
        appended to the capped search text. A closed conversation reopens.
        """
        stamp = _now()
        source = "smart" if smart else "auto"
        with self._connect() as db:
            row = db.execute("SELECT title, title_source FROM conversation_index WHERE session_id = ?",
                             (session_id,)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO conversation_index (session_id, client_id, title, preview, created_at, updated_at, "
                    "title_source, search_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (session_id, owner, title, preview, stamp, stamp, source, text[-SEARCH_TEXT_LIMIT:]),
                )
                return title
            current, current_source = row
            if current_source == "auto" and _is_greeting(current) and (smart or not _is_greeting(title)):
                current, current_source = title, source
            db.execute(
                "UPDATE conversation_index SET title = ?, title_source = ?, preview = ?, updated_at = ?, "
                "search_text = substr(search_text || char(10) || ?, ?), "
                "status = CASE WHEN status = 'closed' THEN (CASE WHEN protocol IS NULL THEN 'open' ELSE 'handoff' END) "
                "ELSE status END, "
                "closed_at = CASE WHEN status = 'closed' THEN NULL ELSE closed_at END "
                "WHERE session_id = ?",
                (current, current_source, preview, stamp, text, -SEARCH_TEXT_LIMIT, session_id),
            )
        self._purge_if_due()
        return current

    def set_protocol(self, session_id: str, protocol: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE conversation_index SET protocol = ?, status = 'handoff' WHERE session_id = ?",
                       (protocol, session_id))

    def close(self, session_id: str, rating: int | None = None, comment: str | None = None) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE conversation_index SET status = 'closed', closed_at = ?, rating = COALESCE(?, rating), "
                "rating_comment = COALESCE(?, rating_comment) WHERE session_id = ?",
                (_now(), rating, comment, session_id),
            )

    def rename(self, session_id: str, title: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE conversation_index SET title = ?, title_source = 'user' WHERE session_id = ?",
                       (title, session_id))

    def list_for(self, owner: str, q: str | None = None, limit: int = 100) -> list[dict]:
        """Newest first. `q` matches title, preview, protocol and the turn text, ignoring case and accents."""
        self._purge_if_due()
        sql = f"SELECT {', '.join(LIST_KEYS)} FROM conversation_index WHERE client_id = ?"
        params: list = [owner]
        if q and q.strip():
            sql += (" AND norm(title || ' ' || preview || ' ' || COALESCE(protocol, '') || ' ' || search_text) "
                    "LIKE ? ESCAPE '\\'")
            params.append(_like(q))
        sql += " ORDER BY updated_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(zip(LIST_KEYS, row)) for row in rows]

    # ---- feedback -------------------------------------------------------------------

    def set_feedback(self, session_id: str, message_ts: str, rating: str, reason: str | None = None,
                     comment: str | None = None) -> None:
        """Store a vote on one answer. A new vote on the same answer replaces the old one."""
        with self._connect() as db:
            db.execute(
                "INSERT INTO feedback (session_id, message_ts, rating, reason, comment, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, message_ts) DO UPDATE SET "
                "rating = excluded.rating, reason = excluded.reason, comment = excluded.comment, "
                "created_at = excluded.created_at",
                (session_id, message_ts, rating, reason, comment, _now()),
            )

    def feedback_for(self, session_id: str) -> dict[str, str]:
        """Message timestamp -> "up" or "down" for one conversation."""
        with self._connect() as db:
            rows = db.execute("SELECT message_ts, rating FROM feedback WHERE session_id = ?", (session_id,)).fetchall()
        return dict(rows)
