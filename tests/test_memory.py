import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from emporio.memory import SEARCH_TEXT_LIMIT, ConversationStore, owner_key

ALICE = owner_key("alice-0123456789abcdef")
BOB = owner_key("bob-0123456789abcdef00")


@pytest.fixture
def memory(tmp_path):
    return ConversationStore(tmp_path / "c.db")


def backdate(memory, session_id, days):
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with sqlite3.connect(memory.path) as db:
        db.execute("UPDATE conversations SET updated_at = ? WHERE session_id = ?", (stamp, session_id))
        db.execute("UPDATE conversation_index SET updated_at = ? WHERE session_id = ?", (stamp, session_id))


def turn(memory, session_id, owner=ALICE, title="oi", text="oi", smart=False):
    memory.save(session_id, memory.load(session_id) + [
        {"role": "user", "content": text, "ts": "t-user"},
        {"role": "assistant", "content": "Olá!", "ts": "t-answer"},
    ])
    return memory.touch(session_id, owner, title, "Olá!", text, smart=smart)


def test_owner_key_is_a_sha256():
    assert len(ALICE) == 64 and ALICE != BOB
    assert owner_key("alice-0123456789abcdef") == ALICE


def test_migrates_the_first_schema_and_hashes_raw_ids(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE conversation_index (session_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, "
                   "title TEXT NOT NULL, preview TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        db.execute("INSERT INTO conversation_index VALUES ('s1', 'alice-0123456789abcdef', 'oi', 'Olá', "
                   "'2026-11-01T10:00:00+00:00', '2026-11-01T10:00:00+00:00')")
    memory = ConversationStore(path)
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(conversation_index)")}
    assert {"status", "protocol", "closed_at", "rating", "rating_comment", "title_source", "search_text"} <= columns
    [row] = memory.list_for(ALICE)
    assert row["session_id"] == "s1" and row["status"] == "open" and row["protocol"] is None
    assert b"alice-0123456789abcdef" not in path.read_bytes()
    ConversationStore(path)  # opening again is a no-op
    assert memory.owner("s1") == ALICE


def test_greeting_titles_give_way_to_smart_titles(memory):
    assert turn(memory, "a", title="boa tarde") == "boa tarde"
    assert turn(memory, "a", title="Pedido #8", smart=True) == "Pedido #8"
    assert turn(memory, "a", title="Horário da loja", smart=True) == "Pedido #8"  # smart titles stay

    turn(memory, "b", title="oi")
    assert turn(memory, "b", title="ok", text="ok") == "oi"  # another greeting does not replace it
    assert turn(memory, "b", title="quero ver os violões da loja") == "quero ver os violões da loja"

    turn(memory, "c", title="quero ver os violões da loja")
    assert turn(memory, "c", title="Takamine GD20", smart=True) == "quero ver os violões da loja"

    turn(memory, "d", title="oi")
    memory.rename("d", "Meu")
    assert turn(memory, "d", title="Pedido #8", smart=True) == "Meu"


def test_search_text_is_capped_and_searchable(memory):
    turn(memory, "a", text="quero um violão Takamine")
    turn(memory, "a", text="x" * (SEARCH_TEXT_LIMIT + 500))
    with sqlite3.connect(memory.path) as db:
        [(text,)] = db.execute("SELECT search_text FROM conversation_index").fetchall()
    assert len(text) == SEARCH_TEXT_LIMIT
    turn(memory, "b", text="trompete para meu filho")
    assert [r["session_id"] for r in memory.list_for(ALICE, q="TROMPETE")] == ["b"]
    assert [r["session_id"] for r in memory.list_for(ALICE, q="a_b")] == []  # _ is not a wildcard
    assert len(memory.list_for(ALICE, q="  ")) == 2


def test_protocol_close_and_reopen(memory):
    turn(memory, "a")
    memory.set_protocol("a", "EMP-20261103-0001")
    assert memory.info("a")["status"] == "handoff"
    memory.close("a", rating=5, comment="ótimo")
    info = memory.info("a")
    assert info["status"] == "closed" and info["rating"] == 5 and info["closed_at"]
    turn(memory, "a")
    info = memory.info("a")
    assert info["status"] == "handoff" and info["closed_at"] is None and info["rating"] == 5


def test_clear_client_deletes_only_that_browser(memory):
    turn(memory, "a1")
    turn(memory, "b1", owner=BOB)
    memory.set_feedback("a1", "t-answer", "up")
    memory.clear_client(ALICE)
    assert memory.list_for(ALICE) == [] and memory.load("a1") == [] and memory.feedback_for("a1") == {}
    assert [r["session_id"] for r in memory.list_for(BOB)] == ["b1"]


def test_purge_removes_conversations_past_the_retention(memory, tmp_path):
    turn(memory, "old")
    turn(memory, "new")
    memory.set_feedback("old", "t-answer", "down")
    backdate(memory, "old", 91)
    assert memory.purge(90) == 1
    assert memory.load("old") == [] and memory.info("old") is None and memory.feedback_for("old") == {}
    assert memory.info("new") is not None

    backdate(memory, "new", 200)
    ConversationStore(memory.path, retention_days=90)  # startup applies the retention
    assert memory.info("new") is None


def test_feedback_vote_is_replaced_and_dropped_on_truncate(memory):
    turn(memory, "a")
    memory.set_feedback("a", "t-answer", "down", "unclear", "não entendi")
    memory.set_feedback("a", "t-answer", "up")
    assert memory.feedback_for("a") == {"t-answer": "up"}
    removed = memory.truncate_from_ts("a", "t-user")
    assert [m["role"] for m in removed] == ["user", "assistant"]
    assert memory.load("a") == [] and memory.feedback_for("a") == {}
    assert memory.truncate_from_ts("a", "missing") is None


def test_deleted_and_purged_sessions_are_remembered_until_the_retention_ends(memory):
    turn(memory, "a")
    turn(memory, "b")
    memory.clear("a")
    memory.clear_client(BOB)
    assert memory.was_deleted("a") and not memory.was_deleted("b")
    memory.clear_client(ALICE)
    assert memory.was_deleted("b")
    turn(memory, "old")
    backdate(memory, "old", 91)
    memory.purge(90)
    assert memory.was_deleted("old")
    with sqlite3.connect(memory.path) as db:
        db.execute("UPDATE deleted_sessions SET deleted_at = '2000-01-01T00:00:00+00:00' WHERE session_id = 'a'")
    memory.purge(90)
    assert not memory.was_deleted("a") and memory.was_deleted("b")


def test_truncate_can_keep_the_votes_for_later(memory):
    turn(memory, "a")
    memory.set_feedback("a", "t-answer", "up")
    removed = memory.truncate_from_ts("a", "t-user", drop_votes=False)
    assert memory.feedback_for("a") == {"t-answer": "up"}
    memory.drop_feedback("a", removed)
    assert memory.feedback_for("a") == {}
