import hashlib
import json
import threading
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from emporio.agent import PROVIDER_ERROR_TEXT, AgentReply, ToolTrace
from emporio.api import CARD_MASK, STREAM_ERROR_TEXT, create_app, mask_cards
from emporio.config import at
from emporio.memory import ConversationStore
from emporio.tools import ToolBox

ALICE = {"X-Client-Id": "alice-0123456789abcdef"}
BOB = {"X-Client-Id": "bob-0123456789abcdef00"}


class FakeAgent:
    """Stands in for the model: picks tool calls from keywords and saves the turn as Agent does."""

    def __init__(self, memory, toolbox):
        self.memory = memory
        self.toolbox = toolbox
        self.seen = []  # texts as the agent received them
        self.turns = 0
        self.active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    def _traces(self, text):
        def real(name, arguments):
            return ToolTrace(name, arguments, self.toolbox.call(name, arguments))

        traces = [ToolTrace("get_product", {"name_or_id": "95"}, {})]
        if "produto 95" in text:
            traces.append(real("get_product", {"name_or_id": "95"}))
        if "até 1500" in text:
            traces.append(real("search_products", {"category": "violão", "max_price": 1500}))
        if "pedido" in text:
            traces.append(ToolTrace("get_order", {"order_id": 8}, {"verified": True, "order": {"order_id": 8, "status": "shipped"},
                                                                "delivery": {"overdue": True, "days_late": 3}}))
        if "estranho" in text:
            traces.append(ToolTrace("get_order", {"order_id": 8}, {"verified": False, "reason": "mismatch"}))
        if "reclamação" in text:
            traces.append(ToolTrace("escalate_to_human", {"reason": "complaint"}, {"protocol": "EMP-20261103-0001", "sla": "24h"}))
        if "troca" in text:
            traces.append(real("check_return", {"order_id": 1, "phone": "(67) 98432-1098"}))
        if "horário" in text:
            traces.append(real("store_hours_now", {}))
        return traces

    def reply(self, session_id, text, on_event=None):
        self.seen.append(text)
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if "devagar" in text:
                time.sleep(0.3)
            if "explode" in text:
                raise RuntimeError("boom")
            traces = self._traces(text)
            if on_event:
                for t in traces:
                    on_event({"type": "tool", "name": t.name, "arguments": t.arguments})
            if "falha" in text:
                return AgentReply(session_id, PROVIDER_ERROR_TEXT, traces, error="APIConnectionError: down")
            n, self.turns = self.turns, self.turns + 1
            user_ts, reply_ts = f"2026-11-03T10:{n:02d}:00-04:00", f"2026-11-03T10:{n:02d}:05-04:00"
            calls = [{"id": f"c{n}-{i}", "type": "function",
                      "function": {"name": t.name, "arguments": json.dumps(t.arguments, ensure_ascii=False)}}
                     for i, t in enumerate(traces)]
            turn = [
                {"role": "user", "content": text, "ts": user_ts},
                {"role": "assistant", "tool_calls": calls},
                *({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(t.result, ensure_ascii=False)}
                  for c, t in zip(calls, traces)),
                {"role": "assistant", "content": "Olha este!\n[[cards: 95]]\n[[quick: Parcelamento]]", "ts": reply_ts},
            ]
            self.memory.save(session_id, self.memory.load(session_id) + turn)
            return AgentReply(session_id, "Olha este!", traces, product_ids=[95], quick_replies=["Parcelamento"],
                              ts=reply_ts)
        finally:
            with self._guard:
                self.active -= 1


@pytest.fixture
def agent(store, policies, tmp_path):
    toolbox = ToolBox(store, policies, lambda: at(date(2026, 11, 3), 10), tmp_path / "h.jsonl")
    return FakeAgent(ConversationStore(tmp_path / "c.db"), toolbox)


@pytest.fixture
def client(agent):
    return TestClient(create_app(agent))


def chat(client, message, session_id=None, headers=ALICE, **extra):
    body = {"message": message, **({"session_id": session_id} if session_id else {}), **extra}
    return client.post("/api/chat", json=body, headers=headers)


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


def test_order_card_only_for_a_verified_lookup(client):
    ok = client.post("/api/chat", json={"message": "meu pedido 8"}, headers=ALICE).json()
    assert ok["order"]["order"]["order_id"] == 8 and ok["order"]["delivery"]["overdue"] is True
    nope = client.post("/api/chat", json={"message": "pedido estranho"}, headers=ALICE).json()
    assert nope["order"]["order"]["order_id"] == 8  # the verified call in that same turn still counts
    other = client.post("/api/chat", json={"message": "algo estranho"}, headers=ALICE).json()
    assert other["order"] is None


def test_product_details_endpoint(client):
    body = client.get("/api/products/95").json()
    assert body["product"]["specs"]["top"] == "Cedro Sólido"
    assert client.get("/api/products/1").status_code == 404


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


def test_feedback_is_stored_and_a_new_vote_replaces_the_old_one(client, agent):
    chat(client, "oi", "abc")
    vote = {"session_id": "abc", "message_ts": "2026-11-03T10:00:05-04:00", "rating": "down",
            "reason": "price_stock", "comment": "  o preço   mudou "}
    assert client.post("/api/feedback", json=vote, headers=ALICE).status_code == 204
    assert client.get("/api/chat/abc", headers=ALICE).json()[1]["feedback"] == "down"
    assert client.post("/api/feedback", json={**vote, "rating": "up", "reason": None}, headers=ALICE).status_code == 204
    assert agent.memory.feedback_for("abc") == {"2026-11-03T10:00:05-04:00": "up"}
    assert client.get("/api/chat/abc", headers=ALICE).json()[1]["feedback"] == "up"


def test_feedback_must_point_at_an_answer_of_that_conversation(client):
    chat(client, "oi", "abc")
    vote = {"session_id": "abc", "message_ts": "2026-11-03T10:00:05-04:00", "rating": "up"}
    assert client.post("/api/feedback", json={**vote, "message_ts": "2026-11-03T10:00:00-04:00"}, headers=ALICE).status_code == 422
    assert client.post("/api/feedback", json={**vote, "reason": "other"}, headers=ALICE).status_code == 422
    assert client.post("/api/feedback", json={**vote, "comment": "x" * 501}, headers=ALICE).status_code == 422
    assert client.post("/api/feedback", json=vote, headers=BOB).status_code == 404


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


def test_pwa_files_are_served_from_the_root(client):
    sw = client.get("/sw.js")
    assert sw.status_code == 200
    assert sw.headers["service-worker-allowed"] == "/"
    assert "/api/" in sw.text  # api calls are never cached
    manifest = client.get("/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    data = manifest.json()
    assert data["start_url"] == "/" and data["display"] == "standalone"
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200


# ---- ownership and privacy ------------------------------------------------------------

def test_only_the_hash_of_the_client_id_is_stored(client, agent):
    chat(client, "oi", "abc")
    raw = ALICE["X-Client-Id"]
    assert agent.memory.owner("abc") == hashlib.sha256(raw.encode()).hexdigest()
    assert raw.encode() not in agent.memory.path.read_bytes()


def test_card_numbers_are_masked_before_the_agent_and_memory(client, agent):
    body = chat(client, "meu cartão é 4111 1111 1111 1111, telefone (67) 98432-1098", "abc").json()
    assert agent.seen[-1] == f"meu cartão é {CARD_MASK}, telefone (67) 98432-1098"
    assert "4111" not in json.dumps(agent.memory.load("abc"))
    assert "4111" not in body["title"]


def test_mask_cards_only_touches_luhn_valid_numbers():
    assert mask_cards("4111-1111-1111-1111") == CARD_MASK
    assert mask_cards("5555555555554444 ok") == f"{CARD_MASK} ok"
    assert mask_cards("cartão 4111 1111 1111 1111 10x") == f"cartão {CARD_MASK} 10x"  # the 18 digits fail Luhn
    assert mask_cards("4111 1111 1111 1112") == "4111 1111 1111 1112"  # fails Luhn
    assert mask_cards("pedido 8, fone (67) 98432-1098") == "pedido 8, fone (67) 98432-1098"
    assert mask_cards("+55 67 98432 1098 5") == "+55 67 98432 1098 5"


def test_delete_all_conversations_of_one_browser(client):
    chat(client, "oi", "a1")
    chat(client, "oi de novo", "a2")
    chat(client, "oi", "b1", headers=BOB)
    client.post("/api/feedback", json={"session_id": "a1", "message_ts": "2026-11-03T10:00:05-04:00", "rating": "up"},
                headers=ALICE)
    assert client.delete("/api/conversations", headers=ALICE).status_code == 204
    assert client.get("/api/conversations", headers=ALICE).json() == []
    assert client.get("/api/chat/a1", headers=ALICE).status_code == 404
    assert [c["session_id"] for c in client.get("/api/conversations", headers=BOB).json()] == ["b1"]


def test_policy_endpoint_serves_only_whitelisted_topics(client):
    body = client.get("/api/policy/privacy").json()
    assert body["topic"] == "privacy" and "LGPD" in body["text"]
    for topic in ("returns", "shipping", "warranty", "payment", "hours"):
        assert client.get(f"/api/policy/{topic}").status_code == 200
    for topic in ("service", "final", "about", "nope"):
        assert client.get(f"/api/policy/{topic}").status_code == 404


def test_product_details_merge_card_fields_and_alternatives_are_cards(client):
    body = client.get("/api/products/95").json()
    product = body["product"]
    assert body["found"] is True
    assert product["max_installments"] == 12 and product["installment_value"] == 183.25
    assert product["category_id"] == 5 and product["specs"]["top"] == "Cedro Sólido"
    assert "alternatives" not in body
    out = client.get("/api/products/96").json()
    assert out["product"]["availability"] == "out_of_stock"
    assert out["availability_note"].startswith("Temporariamente")
    assert out["alternatives"] and all("installment_value" in card for card in out["alternatives"])


# ---- conversation list ----------------------------------------------------------------

def test_list_has_status_protocol_rating_and_search(client):
    chat(client, "quero um violão até 1500", "a1")
    chat(client, "quero fazer uma reclamação", "a2")
    rows = {c["session_id"]: c for c in client.get("/api/conversations", headers=ALICE).json()}
    assert rows["a1"]["status"] == "open" and rows["a1"]["protocol"] is None
    assert rows["a2"]["status"] == "handoff" and rows["a2"]["protocol"] == "EMP-20261103-0001"
    assert rows["a2"]["rating"] is None and rows["a2"]["closed_at"] is None

    def found(q):
        return [c["session_id"] for c in client.get("/api/conversations", params={"q": q}, headers=ALICE).json()]

    assert found("VIOLAO") == ["a1"]  # title and turn text, ignoring case and accents
    assert found("EMP-20261103") == ["a2"]
    assert found("olha este") == ["a2", "a1"]  # preview
    assert found("100%") == []
    assert client.get("/api/conversations", params={"q": "violao"}, headers=BOB).json() == []


def test_close_with_rating_then_a_new_message_reopens(client):
    chat(client, "quero fazer uma reclamação", "abc")
    assert client.post("/api/conversations/abc/close", json={"rating": 6}, headers=ALICE).status_code == 422
    assert client.post("/api/conversations/abc/close", json={"rating": 4, "comment": "ok"}, headers=BOB).status_code == 404
    assert client.post("/api/conversations/abc/close", json={"rating": 4, "comment": "ok"}, headers=ALICE).status_code == 204
    row = client.get("/api/conversations", headers=ALICE).json()[0]
    assert row["status"] == "closed" and row["rating"] == 4 and row["closed_at"]
    body = chat(client, "mais uma coisa", "abc").json()
    assert body["status"] == "handoff" and body["protocol"] == "EMP-20261103-0001"
    row = client.get("/api/conversations", headers=ALICE).json()[0]
    assert row["status"] == "handoff" and row["closed_at"] is None and row["rating"] == 4

    chat(client, "oi", "plain")
    assert client.post("/api/conversations/plain/close", headers=ALICE).status_code == 204
    assert chat(client, "voltei", "plain").json()["status"] == "open"


# ---- titles -----------------------------------------------------------------------------

@pytest.mark.parametrize("message, title", [
    ("me fala do produto 95", "Takamine GD20 Dreadnought Natural"),
    ("quero um violão até 1500", "Violões até R$ 1.500"),
    ("onde está meu pedido?", "Pedido #8"),
    ("quero fazer uma troca", "Troca ou devolução, pedido #1"),
    ("quero fazer uma reclamação", "Atendimento com a equipe"),
    ("qual o horário de vocês?", "Horário da loja"),
])
def test_smart_titles_come_from_the_tools(client, message, title):
    assert chat(client, message, "abc").json()["title"] == title
    assert client.get("/api/conversations", headers=ALICE).json()[0]["title"] == title


def test_a_greeting_title_is_replaced_by_the_next_smart_title_only(client):
    assert chat(client, "oi", "g").json()["title"] == "oi"
    assert chat(client, "me fala do produto 95", "g").json()["title"] == "Takamine GD20 Dreadnought Natural"
    assert chat(client, "onde está meu pedido?", "g").json()["title"] == "Takamine GD20 Dreadnought Natural"

    chat(client, "boa tarde", "r")
    client.patch("/api/conversations/r", json={"title": "Meu"}, headers=ALICE)
    assert chat(client, "me fala do produto 95", "r").json()["title"] == "Meu"  # renamed titles stay


def test_titles_never_carry_phones_or_emails(client):
    title = chat(client, "sobre a entrega, meu e-mail é ana@coldmail.com e fone (67) 98432-1098", "abc").json()["title"]
    assert "@" not in title and "98432" not in title
    assert title.startswith("sobre a entrega")
    assert chat(client, "67 98432-1098", "only").json()["title"] == "Nova conversa"


# ---- history rebuild ----------------------------------------------------------------------

def test_history_rebuilds_the_extras_of_each_answer(client):
    live = [chat(client, m, "abc").json() for m in ("quero fazer uma reclamação", "quero fazer uma troca",
                                                     "qual o horário?", "oi")]
    history = client.get("/api/chat/abc", headers=ALICE).json()
    answers = [m for m in history if m["role"] == "assistant"]
    assert [m["role"] for m in history] == ["user", "assistant"] * 4
    for answer, reply in zip(answers, live):
        for key in ("handoff", "order", "returns", "store"):
            assert answer[key] == reply[key]
        assert answer["tools"] == reply["tool_calls"]
    assert answers[0]["handoff"]["protocol"] == "EMP-20261103-0001"
    assert answers[1]["order"]["order"]["order_id"] == 1
    assert answers[1]["returns"]["applicable"] is True and "order" not in answers[1]["returns"]
    assert answers[2]["store"]["open_now"] is True
    assert answers[3]["handoff"] is None and answers[3]["order"] is None and answers[3]["store"] is None
    assert answers[3]["feedback"] is None


def test_returns_card_only_for_a_verified_check(client):
    body = chat(client, "quero fazer uma troca", "abc").json()
    assert body["returns"]["windows"]["legal_warranty"]
    assert body["order"]["order"]["order_id"] == 1
    assert chat(client, "oi", "abc").json()["returns"] is None


# ---- errors, edits and concurrency ------------------------------------------------------

def test_provider_error_is_flagged_and_nothing_is_saved(client):
    body = chat(client, "isso vai dar falha", "abc").json()
    assert body["error"] is True and body["reply"] == PROVIDER_ERROR_TEXT
    assert body["cards"] == [] and body["handoff"] is None
    assert client.get("/api/conversations", headers=ALICE).json() == []
    assert chat(client, "oi", "abc").json()["error"] is False


def test_stream_error_is_portuguese_and_retryable(client):
    with client.stream("POST", "/api/chat/stream", json={"message": "explode", "session_id": "abc"},
                       headers=ALICE) as response:
        body = "".join(response.iter_text())
    kind, data = body.strip().split("\n")
    assert kind == "event: error"
    assert json.loads(data[len("data: "):]) == {"message": STREAM_ERROR_TEXT, "retryable": True}
    # the session lock was released: the next turn on that conversation goes through
    assert chat(client, "oi", "abc").status_code == 200


def test_stream_status_never_echoes_arguments(client):
    with client.stream("POST", "/api/chat/stream", json={"message": "quero fazer uma troca"}, headers=ALICE) as response:
        body = "".join(response.iter_text())
    events = [block.split("\n") for block in body.strip().split("\n\n")]
    statuses = [json.loads(e[1][len("data: "):]) for e in events if e[0] == "event: status"]
    assert statuses[-1] == {"tool": "check_return", "label": "Conferindo os prazos de troca e devolução"}
    assert "98432" not in json.dumps(statuses)
    reply = json.loads(events[-1][1][len("data: "):])
    assert reply["error"] is False and reply["returns"]["applicable"] is True


def test_edit_replaces_the_turn_and_drops_its_votes(client, agent):
    chat(client, "oi", "abc")
    chat(client, "qual o horário?", "abc")
    second_user, second_answer = "2026-11-03T10:01:00-04:00", "2026-11-03T10:01:05-04:00"
    client.post("/api/feedback", json={"session_id": "abc", "message_ts": second_answer, "rating": "down"}, headers=ALICE)
    body = chat(client, "qual o horário de sábado?", "abc", replace_from_ts=second_user).json()
    history = client.get("/api/chat/abc", headers=ALICE).json()
    assert [m["content"] for m in history if m["role"] == "user"] == ["oi", "qual o horário de sábado?"]
    assert body["ts"] == history[-1]["ts"] == "2026-11-03T10:02:05-04:00"
    assert agent.memory.feedback_for("abc") == {}


def test_edit_refuses_to_remove_a_handoff(client):
    chat(client, "quero fazer uma reclamação", "abc")
    response = chat(client, "outra coisa", "abc", replace_from_ts="2026-11-03T10:00:00-04:00")
    assert response.status_code == 409 and response.json() == {"detail": "protocol"}
    with client.stream("POST", "/api/chat/stream", json={"message": "x", "session_id": "abc",
                                                          "replace_from_ts": "2026-11-03T10:00:00-04:00"},
                       headers=ALICE) as streamed:
        assert streamed.status_code == 409
    assert len([m for m in client.get("/api/chat/abc", headers=ALICE).json() if m["role"] == "user"]) == 1
    assert chat(client, "x", "abc", replace_from_ts="2030-01-01T00:00:00-04:00").status_code == 422


def test_turns_on_the_same_conversation_are_serialized(client, agent):
    chat(client, "oi", "abc")
    threads = [threading.Thread(target=chat, args=(client, f"devagar {i}", "abc")) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert agent.max_active == 1
    assert len([m for m in agent.memory.load("abc") if m["role"] == "user"]) == 3


def test_mask_cards_accepts_pasted_separators():
    for pasted in ("4111 1111 1111 1111", "4111  1111  1111  1111", "4111.1111.1111.1111",
                   "4111 1111 1111 1111", "4111 1111 - 1111 1111"):
        assert mask_cards(f"cartão {pasted} ok") == f"cartão {CARD_MASK} ok"
    assert mask_cards("R$ 1.500,00 em 10x") == "R$ 1.500,00 em 10x"


def test_mask_cards_stays_fast_on_a_long_run_of_digit_groups():
    text = ("1 " * 1000).strip()
    started = time.perf_counter()
    assert mask_cards(text) == text
    assert time.perf_counter() - started < 0.1
    assert mask_cards(f"{text} 4111 1111 1111 1111").startswith("1 1 ")


def test_a_failed_regenerate_leaves_the_conversation_as_it_was(client, agent):
    chat(client, "oi primeira", "abc")
    chat(client, "segunda pergunta", "abc")
    second_user, second_answer = "2026-11-03T10:01:00-04:00", "2026-11-03T10:01:05-04:00"
    client.post("/api/feedback", json={"session_id": "abc", "message_ts": second_answer, "rating": "up"}, headers=ALICE)
    before = client.get("/api/chat/abc", headers=ALICE).json()

    assert chat(client, "falha agora", "abc", replace_from_ts=second_user).json()["error"] is True
    assert client.get("/api/chat/abc", headers=ALICE).json() == before
    with client.stream("POST", "/api/chat/stream", json={"message": "explode", "session_id": "abc",
                                                          "replace_from_ts": second_user}, headers=ALICE) as response:
        assert "event: error" in "".join(response.iter_text())
    assert client.get("/api/chat/abc", headers=ALICE).json() == before
    assert agent.memory.feedback_for("abc") == {second_answer: "up"}

    # the restored turn can still be edited, and a successful edit drops its votes
    body = chat(client, "segunda, de novo", "abc", replace_from_ts=second_user).json()
    assert body["error"] is False and agent.memory.feedback_for("abc") == {}


def test_delete_waits_for_a_turn_in_flight(client, agent):
    chat(client, "meu pedido 8, telefone 67 98432-1098", "abc")
    worker = threading.Thread(target=chat, args=(client, "devagar, e o prazo?", "abc"))
    worker.start()
    time.sleep(0.1)
    assert client.delete("/api/chat/abc", headers=ALICE).status_code == 204
    worker.join()
    assert client.get("/api/conversations", headers=ALICE).json() == []
    assert agent.memory.load("abc") == []


def test_delete_all_waits_for_a_first_turn_in_flight(client, agent):
    chat(client, "oi", "a1")
    worker = threading.Thread(target=chat, args=(client, "devagar, primeira mensagem", "fresh"))
    worker.start()
    time.sleep(0.1)
    assert client.delete("/api/conversations", headers=ALICE).status_code == 204
    worker.join()
    assert client.get("/api/conversations", headers=ALICE).json() == []
    assert agent.memory.load("fresh") == [] and agent.memory.load("a1") == []


def test_close_waits_for_a_turn_in_flight(client):
    chat(client, "oi", "abc")
    worker = threading.Thread(target=chat, args=(client, "devagar", "abc"))
    worker.start()
    time.sleep(0.1)
    assert client.post("/api/conversations/abc/close", json={"rating": 5}, headers=ALICE).status_code == 204
    worker.join()
    assert client.get("/api/conversations", headers=ALICE).json()[0]["status"] == "closed"


def test_a_deleted_conversation_id_is_not_reused(client, agent):
    chat(client, "oi", "abc")
    client.delete("/api/chat/abc", headers=ALICE)
    assert chat(client, "e o segundo?", "abc").status_code == 404
    chat(client, "oi", "a2")
    client.delete("/api/conversations", headers=ALICE)
    assert chat(client, "e o segundo?", "a2").status_code == 404
    with client.stream("POST", "/api/chat/stream", json={"message": "x", "session_id": "a2"}, headers=ALICE) as response:
        assert response.status_code == 404
    assert client.get("/api/conversations", headers=ALICE).json() == []


def test_unknown_or_negative_product_ids_are_404(client):
    assert client.get("/api/products/-1").status_code == 404
    assert client.get("/api/products/99999").status_code == 404


def test_replay_clock_keeps_running():
    import time as _time
    from emporio.config import Settings, make_clock
    fixed = at(date(2025, 12, 26), 10, 30)
    clock = make_clock(Settings("u", "k", "m", 0.0, fixed, None, None))
    first = clock()
    assert first == fixed
    _time.sleep(1.1)
    assert clock() > first
