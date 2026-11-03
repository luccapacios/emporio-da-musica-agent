from fastapi.testclient import TestClient

from emporio.agent import AgentReply, ToolTrace
from emporio.api import create_app
from emporio.memory import ConversationStore


class FakeAgent:
    def __init__(self, memory):
        self.memory = memory
        self.calls = []

    def reply(self, session_id, text):
        self.calls.append((session_id, text))
        self.memory.save(session_id, [{"role": "user", "content": text},
                                      {"role": "assistant", "content": "Oi! Sou a Duda."}])
        return AgentReply(session_id, "Oi! Sou a Duda.", [ToolTrace("store_hours_now", {}, {"open_now": True})])


def make_client(tmp_path):
    agent = FakeAgent(ConversationStore(tmp_path / "c.db"))
    return TestClient(create_app(agent)), agent


def test_chat_returns_reply_session_and_tool_calls(tmp_path):
    client, agent = make_client(tmp_path)
    response = client.post("/api/chat", json={"message": "  oi  "})
    body = response.json()
    assert response.status_code == 200
    assert body["reply"] == "Oi! Sou a Duda."
    assert body["session_id"].startswith("web-")
    assert body["tool_calls"] == [{"name": "store_hours_now", "arguments": {}}]
    assert agent.calls == [(body["session_id"], "oi")]


def test_history_and_reset(tmp_path):
    client, _ = make_client(tmp_path)
    client.post("/api/chat", json={"message": "oi", "session_id": "abc"})
    assert [m["role"] for m in client.get("/api/chat/abc").json()] == ["user", "assistant"]
    assert client.delete("/api/chat/abc").status_code == 204
    assert client.get("/api/chat/abc").json() == []


def test_rejects_bad_input(tmp_path):
    client, _ = make_client(tmp_path)
    assert client.post("/api/chat", json={"message": ""}).status_code == 422
    assert client.post("/api/chat", json={"message": "oi", "session_id": "../etc"}).status_code == 422


def test_serves_the_web_chat(tmp_path):
    client, _ = make_client(tmp_path)
    page = client.get("/")
    assert page.status_code == 200
    assert "Empório da Música" in page.text
