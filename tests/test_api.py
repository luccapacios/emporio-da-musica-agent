from datetime import date

import pytest
from fastapi.testclient import TestClient

from emporio.agent import AgentReply, ToolTrace
from emporio.api import create_app
from emporio.config import at
from emporio.memory import ConversationStore
from emporio.tools import ToolBox


class FakeAgent:
    def __init__(self, memory, toolbox):
        self.memory = memory
        self.toolbox = toolbox
        self.calls = []

    def reply(self, session_id, text):
        self.calls.append((session_id, text))
        self.memory.save(session_id, [{"role": "user", "content": text},
                                      {"role": "assistant", "content": "Olha este!\n[[cards: 95]]"}])
        return AgentReply(session_id, "Olha este!", [ToolTrace("get_product", {"name_or_id": "95"}, {})],
                          product_ids=[95], quick_replies=["Parcelamento"])


@pytest.fixture
def client(store, policies, tmp_path):
    toolbox = ToolBox(store, policies, lambda: at(date(2026, 11, 3), 10), tmp_path / "h.jsonl")
    agent = FakeAgent(ConversationStore(tmp_path / "c.db"), toolbox)
    return TestClient(create_app(agent))


def test_chat_returns_reply_cards_and_quick_replies(client):
    body = client.post("/api/chat", json={"message": "  quanto custa o takamine?  "}).json()
    assert body["reply"] == "Olha este!"
    assert body["session_id"].startswith("web-")
    assert body["tool_calls"] == [{"name": "get_product", "arguments": {"name_or_id": "95"}}]
    assert body["quick_replies"] == ["Parcelamento"]
    card = body["cards"][0]
    assert card["name"] == "Takamine GD20 Dreadnought Natural"
    assert card["max_installments"] == 12 and card["installment_value"] == 183.25
    assert card["category_id"] == 5 and card["color"] == "Natural"


def test_history_rebuilds_cards_and_reset_clears(client):
    client.post("/api/chat", json={"message": "oi", "session_id": "abc"})
    history = client.get("/api/chat/abc").json()
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[1]["content"] == "Olha este!"
    assert history[1]["cards"][0]["id"] == 95
    assert client.delete("/api/chat/abc").status_code == 204
    assert client.get("/api/chat/abc").json() == []


def test_store_status_comes_from_the_rules(client):
    status = client.get("/api/store-status").json()
    assert status["open_now"] is True
    assert status["closes_at"] == "18:00"


def test_rejects_bad_input(client):
    assert client.post("/api/chat", json={"message": ""}).status_code == 422
    assert client.post("/api/chat", json={"message": "oi", "session_id": "../etc"}).status_code == 422


def test_serves_the_web_chat(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Empório da Música" in page.text
