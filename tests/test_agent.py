import copy
import json
from datetime import date
from types import SimpleNamespace

import openai
import pytest
from openai.types.chat import ChatCompletionMessage

from emporio.agent import FALLBACK_TEXT, MAX_TOOL_ROUNDS, PROVIDER_ERROR_TEXT, Agent, split_markers
from emporio.config import Settings, at
from emporio.memory import ConversationStore, trim_history
from emporio.tools import ToolBox


def tool_call(name, arguments, call_id="call_1"):
    return ChatCompletionMessage.model_validate({
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
            "extra_content": {"google": {"thought_signature": "sig-123"}},
        }],
    })


def text(content):
    return ChatCompletionMessage.model_validate({"role": "assistant", "content": content})


class FakeClient:
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        item = self.scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(choices=[SimpleNamespace(message=item)])


@pytest.fixture
def settings(tmp_path):
    return Settings("http://fake", "key", "fake-model", 0.0, None, tmp_path, tmp_path)


@pytest.fixture
def toolbox(store, policies, tmp_path):
    return ToolBox(store, policies, lambda: at(date(2026, 11, 3), 10), tmp_path / "handoffs.jsonl")


@pytest.fixture
def memory(tmp_path):
    return ConversationStore(tmp_path / "conversations.db")


def test_runs_tools_then_answers_and_saves_history(settings, toolbox, memory):
    client = FakeClient([tool_call("get_product", {"name_or_id": "takamine gd20"}), text("Custa R$ 2.199,00.")])
    agent = Agent(settings, toolbox, client=client, memory=memory)

    reply = agent.reply("s1", "Quanto custa o Takamine GD20?")

    assert reply.text == "Custa R$ 2.199,00."
    assert [t.name for t in reply.tool_calls] == ["get_product"]
    assert reply.tool_calls[0].result["product"]["id"] == 95

    second_request = client.requests[1]["messages"]
    assert second_request[0]["role"] == "system"
    assert "terça-feira, 03/11/2026" in second_request[0]["content"]
    assistant_turn = second_request[2]
    assert assistant_turn["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "sig-123"
    assert second_request[3]["role"] == "tool" and second_request[3]["tool_call_id"] == "call_1"

    saved = memory.load("s1")
    assert [m["role"] for m in saved] == ["user", "assistant", "tool", "assistant"]


def test_next_turn_sees_previous_history(settings, toolbox, memory):
    client = FakeClient([text("Oi! Sou a Duda."), text("Claro.")])
    agent = Agent(settings, toolbox, client=client, memory=memory)
    agent.reply("s2", "oi")
    agent.reply("s2", "me ajuda?")
    roles = [m["role"] for m in client.requests[1]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


def test_provider_errors_are_friendly_and_not_saved(settings, toolbox, memory):
    error = openai.APIConnectionError(request=SimpleNamespace(method="POST", url="http://fake"))
    agent = Agent(settings, toolbox, client=FakeClient([error]), memory=memory)
    reply = agent.reply("s3", "oi")
    assert reply.text == PROVIDER_ERROR_TEXT
    assert reply.error
    assert memory.load("s3") == []


def test_tool_loop_is_capped(settings, toolbox, memory):
    calls = [tool_call("store_hours_now", {}, f"c{i}") for i in range(MAX_TOOL_ROUNDS)]
    agent = Agent(settings, toolbox, client=FakeClient(calls), memory=memory)
    reply = agent.reply("s4", "vocês abrem?")
    assert reply.text == FALLBACK_TEXT
    assert len(reply.tool_calls) == MAX_TOOL_ROUNDS


def test_display_markers_are_stripped_and_parsed():
    raw = "Separei estes:\n- Tagima, R$ 429,90\n\n[[cards: 83, 85, 85, 9999]]\n[[quick: Só nylon | Só aço]]"
    text, ids, quick = split_markers(raw)
    assert text == "Separei estes:\n- Tagima, R$ 429,90"
    assert ids == [83, 85, 9999]
    assert quick == ["Só nylon", "Só aço"]


def test_reply_exposes_known_product_ids_only(settings, toolbox, memory):
    client = FakeClient([text("Olha só: Tagima Memphis.\n[[cards: 83, 9999]]\n[[quick: Ver mais]]")])
    reply = Agent(settings, toolbox, client=client, memory=memory).reply("s5", "violão barato?")
    assert reply.text == "Olha só: Tagima Memphis."
    assert reply.product_ids == [83]
    assert reply.quick_replies == ["Ver mais"]
    assert "[[cards" in memory.load("s5")[-1]["content"]  # kept in history for the model and the UI


def test_trim_history_cuts_at_user_turns():
    messages = []
    for i in range(5):
        messages += [{"role": "user", "content": str(i)},
                     {"role": "assistant", "tool_calls": [{"id": str(i)}]},
                     {"role": "tool", "tool_call_id": str(i), "content": "{}"},
                     {"role": "assistant", "content": "ok"}]
    trimmed = trim_history(messages, 2)
    assert trimmed[0] == {"role": "user", "content": "3"}
    assert len(trimmed) == 8
