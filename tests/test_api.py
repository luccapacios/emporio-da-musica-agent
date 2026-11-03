import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from emporio.agent import AgentReply, ToolTrace
from emporio.api import create_app
from emporio.config import at
from emporio.memory import ConversationStore
from emporio.tools import ToolBox

ALICE = {"X-Client-Id": "alice-0123456789abcdef"}
BOB = {"X-Client-Id": "bob-0123456789abcdef00"}


class FakeAgent:
    def __init__(self, memory, toolbox):
        self.memory = memory
        self.toolbox = toolbox

    def reply(self, session_id, text, on_event=None):
        if on_event:
            on_event({"type": "tool", "name": "get_product", "arguments": {"name_or_id": "95"}})
        history = self.memory.load(session_id)
        self.memory.save(session_id, history + [
            {"role": "user", "content": text, "ts": "2026-11-03T10:00:00-04:00"},
            {"role": "assistant", "content": "Olha este!\n[[cards: 95]]\n[[quick: Parcelamento]]", "ts": "2026-11-03T10:00:05-04:00"},
        ])
        traces = [ToolTrace("get_product", {"name_or_id": "95"}, {})]
        if "reclamação" in text:
            traces.append(ToolTrace("escalate_to_human", {"reason": "complaint"}, {"protocol": "EMP-20261103-0001", "sla": "24h"}))
        return AgentReply(session_id, "Olha este!", traces, product_ids=[95], quick_replies=["Parcelamento"],
                          ts="2026-11-03T10:00:05-04:00")


@pytest.fixture
def client(store, policies, tmp_path):
    toolbox = ToolBox(store, policies, lambda: at(date(2026, 11, 3), 10), tmp_path / "h.jsonl")
    agent = FakeAgent(ConversationStore(tmp_path / "c.db"), toolbox)
    return TestClient(create_app(agent))


def test_chat_returns_reply_cards_and_quick_replies(client):
    body = client.post("/api/chat", json={"message": "  quanto custa o takamine?  "}, headers=ALICE).json()
    assert body["reply"] == "Olha este!"
    assert body["session_id"].startswith("web-")
    assert body["title"] == "quanto custa o takamine?"
    assert body["ts"] == "2026-11-03T10:00:05-04:00"
    assert body["tool_calls"] == [{"name": "get_product", "arguments": {"name_or_id": "95"}}]
    assert body["quick_replies"] == ["Parcelamento"]
    assert body["handoff"] is None
    card = body["cards"][0]
    assert card["name"] == "Takamine GD20 Dreadnought Natural"
    assert card["max_installments"] == 12 and card["installment_value"] == 183.25


def test_handoff_result_is_returned_for_the_protocol_card(client):
    body = client.post("/api/chat", json={"message": "quero fazer uma reclamação"}, headers=ALICE).json()
    assert body["handoff"]["protocol"] == "EMP-20261103-0001"


def test_conversations_are_listed_per_browser_only(client):
    first = client.post("/api/chat", json={"message": "oi, tudo bem?"}, headers=ALICE).json()
    client.post("/api/chat", json={"message": "segunda conversa"}, headers=ALICE)
    client.post("/api/chat", json={"message": "conversa do bob"}, headers=BOB)

    mine = client.get("/api/conversations", headers=ALICE).json()
    assert [c["title"] for c in mine] == ["segunda conversa", "oi, tudo bem?"]
    assert mine[1]["preview"] == "Olha este!"
    assert [c["title"] for c in client.get("/api/conversations", headers=BOB).json()] == ["conversa do bob"]

    # another browser can neither read, continue, rename nor delete it
    sid = first["session_id"]
    assert client.get(f"/api/chat/{sid}", headers=BOB).status_code == 404
    assert client.post("/api/chat", json={"message": "oi", "session_id": sid}, headers=BOB).status_code == 404
    assert client.patch(f"/api/conversations/{sid}", json={"title": "x"}, headers=BOB).status_code == 404
    assert client.delete(f"/api/chat/{sid}", headers=BOB).status_code == 404


def test_history_has_timestamps_cards_and_quick_replies(client):
    client.post("/api/chat", json={"message": "oi", "session_id": "abc"}, headers=ALICE)
    history = client.get("/api/chat/abc", headers=ALICE).json()
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["ts"] == "2026-11-03T10:00:00-04:00"
    assert history[1]["content"] == "Olha este!"
    assert history[1]["cards"][0]["id"] == 95
    assert history[1]["quick_replies"] == ["Parcelamento"]


def test_rename_and_delete(client):
    client.post("/api/chat", json={"message": "oi", "session_id": "abc"}, headers=ALICE)
    assert client.patch("/api/conversations/abc", json={"title": "  Violão   pro meu filho "}, headers=ALICE).status_code == 204
    assert client.get("/api/conversations", headers=ALICE).json()[0]["title"] == "Violão pro meu filho"
    assert client.delete("/api/chat/abc", headers=ALICE).status_code == 204
    assert client.get("/api/conversations", headers=ALICE).json() == []
    assert client.get("/api/chat/abc", headers=ALICE).status_code == 404


def test_feedback_is_logged(client, tmp_path):
    client.post("/api/chat", json={"message": "oi", "session_id": "abc"}, headers=ALICE)
    response = client.post("/api/feedback", json={"session_id": "abc", "message_ts": "2026-11-03T10:00:05-04:00",
                                                  "rating": "down", "comment": "não era isso"}, headers=ALICE)
    assert response.status_code == 204
    logged = json.loads((tmp_path / "feedback.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert logged["rating"] == "down" and logged["session_id"] == "abc"
    assert client.post("/api/feedback", json={"session_id": "abc", "rating": "up"}, headers=BOB).status_code == 404


def test_stream_sends_tool_status_then_reply(client):
    with client.stream("POST", "/api/chat/stream", json={"message": "takamine"}, headers=ALICE) as response:
        body = "".join(response.iter_text())
    events = [block.split("\n") for block in body.strip().split("\n\n")]
    assert events[0][0] == "event: status"
    assert json.loads(events[0][1][len("data: "):])["label"] == "Conferindo o produto"
    assert events[-1][0] == "event: reply"
    assert json.loads(events[-1][1][len("data: "):])["reply"] == "Olha este!"


def test_requires_a_client_id(client):
    assert client.post("/api/chat", json={"message": "oi"}).status_code == 400
    assert client.get("/api/conversations", headers={"X-Client-Id": "short"}).status_code == 400


def test_store_status_comes_from_the_rules(client):
    status = client.get("/api/store-status").json()
    assert status["open_now"] is True
    assert status["closes_at"] == "18:00"


def test_rejects_bad_input(client):
    assert client.post("/api/chat", json={"message": ""}, headers=ALICE).status_code == 422
    assert client.post("/api/chat", json={"message": "oi", "session_id": "../etc"}, headers=ALICE).status_code == 422


def test_serves_the_web_chat(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Empório da Música" in page.text
