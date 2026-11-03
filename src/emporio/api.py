"""HTTP API and web chat.

Run: uvicorn emporio.api:app --reload   then open http://localhost:8000

There are no accounts. The browser generates an unguessable client id and sends it as the
X-Client-Id header; every conversation is tied to the client id that started it (stored as
its sha256), and a request for a conversation owned by another id gets a 404, as if it did
not exist.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import Callable, Literal

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import FALLBACK_TEXT, Agent, AgentReply, ToolTrace, build_agent, split_markers
from .config import ROOT, get_settings
from .memory import owner_key
from .tools import ToolBox, brl

log = logging.getLogger(__name__)

WEB_DIR = ROOT / "web"
SESSION_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
CLIENT_PATTERN = r"^[A-Za-z0-9_-]{16,64}$"

# What the chat shows while a tool runs (streaming endpoint).
TOOL_STATUS = {
    "search_products": "Procurando no catálogo",
    "get_product": "Conferindo o produto",
    "list_active_promotions": "Vendo as promoções ativas",
    "payment_options": "Calculando pagamento e parcelas",
    "get_order": "Consultando seu pedido",
    "find_orders": "Procurando seus pedidos",
    "check_return": "Conferindo os prazos de troca e devolução",
    "store_hours_now": "Vendo o horário da loja",
    "get_policy": "Consultando o manual da loja",
    "escalate_to_human": "Chamando a equipe",
}

# Manual sections the chat may show directly (the rest is staff guidance, see manual section 10).
POLICY_TOPICS = ("privacy", "returns", "shipping", "warranty", "payment", "hours")

STREAM_ERROR_TEXT = "Não consegui responder agora. Tente de novo em instantes."
CARD_MASK = "[número de cartão removido]"

# A run of digit groups split by short runs of spaces (any kind, as pasted from banking apps
# and PDFs), dots or hyphens, not part of a "+55 ..." phone.
_DIGIT_RUN = re.compile(r"(?<![\d+])\d+(?:[\s.-]{1,3}\d+)*")
CARD_MAX_DIGITS = 19
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+?\(?\d[\d\s().-]{5,}\d")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, pattern=SESSION_PATTERN)
    replace_from_ts: str | None = Field(default=None, max_length=40)  # edit or regenerate from this user message


class ToolCallOut(BaseModel):
    name: str
    arguments: dict


class ChatResponse(BaseModel):
    session_id: str
    title: str
    status: Literal["open", "handoff", "closed"] = "open"
    protocol: str | None = None
    reply: str
    ts: str | None
    error: bool = False  # provider failure: the reply is the friendly error text and nothing was saved
    tool_calls: list[ToolCallOut]
    cards: list[dict]
    quick_replies: list[str]
    handoff: dict | None = None
    order: dict | None = None
    returns: dict | None = None
    store: dict | None = None


class HistoryMessage(BaseModel):
    role: str
    content: str
    ts: str | None = None
    cards: list[dict] = []
    quick_replies: list[str] = []
    tools: list[ToolCallOut] = []
    handoff: dict | None = None
    order: dict | None = None
    returns: dict | None = None
    store: dict | None = None
    feedback: Literal["up", "down"] | None = None


class ConversationOut(BaseModel):
    session_id: str
    title: str
    preview: str
    created_at: str
    updated_at: str
    status: str
    protocol: str | None
    rating: int | None
    closed_at: str | None


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=80)


class CloseRequest(BaseModel):
    rating: int | None = Field(default=None, ge=1, le=5)
    comment: str | None = Field(default=None, max_length=500)


class FeedbackRequest(BaseModel):
    session_id: str = Field(pattern=SESSION_PATTERN)
    message_ts: str = Field(min_length=1, max_length=40)
    rating: Literal["up", "down"]
    reason: Literal["wrong_info", "price_stock", "unclear", "want_human"] | None = None
    comment: str | None = Field(default=None, max_length=500)


def _title(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= 60 else flat[:57].rstrip() + "..."


def _preview(text: str) -> str:
    flat = " ".join(text.replace("*", "").split())
    return flat if len(flat) <= 90 else flat[:87].rstrip() + "..."


def _text_title(text: str) -> str:
    """The customer's words as a title, without phones or e-mails."""
    clean = _EMAIL.sub(" ", text)
    clean = _PHONE.sub(lambda m: " " if len(re.sub(r"\D", "", m.group(0))) >= 8 else m.group(0), clean)
    clean = re.sub(r"\s+([,.;:!?])", r"\1", " ".join(clean.split())).strip(" ,.;:-")
    return _title(clean) if clean else "Nova conversa"


def _money(value) -> str:
    text = brl(float(value))
    return text[:-3] if text.endswith(",00") else text


def _smart_title(text: str, traces: list[ToolTrace], toolbox: ToolBox) -> tuple[str, bool]:
    """(title, from a tool?) for a turn: what was looked up, else the customer's own words."""
    def last(name: str, ok: Callable[[dict], bool]) -> dict | None:
        return next((t.result for t in reversed(traces) if t.name == name and ok(t.result)), None)

    found = last("check_return", lambda r: r.get("verified"))
    if found:
        return f"Troca ou devolução, pedido #{found['order']['order_id']}", True
    found = last("get_order", lambda r: r.get("verified"))
    if found:
        return f"Pedido #{found['order']['order_id']}", True
    found = last("get_product", lambda r: r.get("found"))
    if found:
        return _title(found["product"]["name"]), True
    search = next((t for t in reversed(traces) if t.name == "search_products" and "results" in t.result
                   and t.arguments.get("category") and t.arguments.get("max_price") is not None), None)
    if search:
        try:
            limit = _money(search.arguments["max_price"])
        except (TypeError, ValueError):
            limit = None
        if limit:
            wanted = str(search.arguments["category"])
            category = toolbox.store.categories.get(toolbox._resolve_category(wanted)) or wanted.strip().capitalize()
            return f"{category} até {limit}", True
    if last("escalate_to_human", lambda r: "protocol" in r):
        return "Atendimento com a equipe", True
    if last("store_hours_now", lambda r: "open_now" in r):
        return "Horário da loja", True
    return _text_title(text), False


def _extras(traces: list[ToolTrace]) -> dict:
    """What the chat renders next to an answer, from the tool calls of that turn.

    Order and return cards only come from a result the tool verified in that very turn.
    """
    def last(names: tuple[str, ...], ok: Callable[[dict], bool]) -> ToolTrace | None:
        return next((t for t in reversed(traces) if t.name in names and ok(t.result)), None)

    handoff = last(("escalate_to_human",), lambda r: "protocol" in r)
    # get_order carries the delivery status (overdue, stale); prefer it over check_return.
    order = (last(("get_order",), lambda r: r.get("verified") and "order" in r)
             or last(("check_return",), lambda r: r.get("verified") and "order" in r))
    returns = last(("check_return",), lambda r: r.get("verified"))
    store = last(("store_hours_now",), lambda r: "open_now" in r)
    return {
        "tools": [{"name": t.name, "arguments": t.arguments} for t in traces],
        "handoff": handoff.result if handoff else None,
        "order": {"order": order.result["order"], "delivery": order.result.get("delivery", {})} if order else None,
        "returns": {k: v for k, v in returns.result.items() if k != "order"} if returns else None,
        "store": store.result if store else None,
    }


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def mask_cards(text: str) -> str:
    """Replace card numbers (13 to 19 digits, Luhn valid, spaces, dots or hyphens allowed)."""
    def fix(match: re.Match) -> str:
        run = match.group(0)
        groups = list(re.finditer(r"\d+", run))
        pieces, pos, i = [], 0, 0
        while i < len(groups):
            # The longest span of whole groups starting here that is a valid card number. Spans
            # only grow, so the walk stops past 19 digits and a long run stays linear.
            span, digits = None, ""
            for j in range(i, len(groups)):
                digits += groups[j].group(0)
                if len(digits) > CARD_MAX_DIGITS:
                    break
                if len(digits) >= 13 and _luhn(digits):
                    span = j
            if span is None:
                i += 1
                continue
            pieces += [run[pos:groups[i].start()], CARD_MASK]
            pos, i = groups[span].end(), span + 1
        return "".join(pieces) + run[pos:]

    return _DIGIT_RUN.sub(fix, text)


def _json_dict(raw) -> dict:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _calls_escalation(messages: list[dict]) -> bool:
    return any((call.get("function") or {}).get("name") == "escalate_to_human"
               for m in messages if m.get("role") == "assistant" for call in m.get("tool_calls") or [])


def _check_session(session_id: str) -> None:
    if not re.fullmatch(SESSION_PATTERN, session_id):
        raise HTTPException(422, "invalid session id")


def _check_client(client_id: str | None) -> str:
    """Validate the header and return the owner key stored for it (never the raw id)."""
    if not client_id or not re.fullmatch(CLIENT_PATTERN, client_id):
        raise HTTPException(400, "missing or invalid X-Client-Id header")
    return owner_key(client_id)


class SessionLocks:
    """One lock per conversation, so two tabs cannot interleave turns (load, reply, save)."""

    def __init__(self):
        self._guard = threading.Lock()
        self._locks: dict[str, list] = {}  # session -> [lock, how many hold or wait for it]

    def acquire(self, session_id: str) -> Callable[[], None]:
        """Block until this session is free. Returns the release function (any thread may call it)."""
        with self._guard:
            entry = self._locks.setdefault(session_id, [threading.Lock(), 0])
            entry[1] += 1
        entry[0].acquire()

        def release() -> None:
            entry[0].release()
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._locks[session_id]

        return release

    @contextmanager
    def hold(self, session_id: str):
        release = self.acquire(session_id)
        try:
            yield
        finally:
            release()


def create_app(agent: Agent | None = None) -> FastAPI:
    state: dict[str, Agent | None] = {"agent": agent}
    locks = SessionLocks()
    # Sessions each owner has a turn running on, so deleting everything also waits for a
    # conversation whose first turn has not been indexed yet.
    inflight: dict[str, set[str]] = {}
    inflight_guard = threading.Lock()

    def get_agent() -> Agent:
        if state["agent"] is None:
            settings = get_settings()
            if not settings.llm_api_key:
                raise HTTPException(503, "LLM_API_KEY is not configured. See .env.example.")
            state["agent"] = build_agent(settings)
        return state["agent"]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Opening the conversation store applies the retention period, so do it at startup.
        if state["agent"] is None and get_settings().llm_api_key:
            get_agent()
        yield

    app = FastAPI(title="Empório da Música assistant", lifespan=lifespan)

    def owned(session_id: str, owner: str, allow_new: bool = False) -> None:
        """404 unless the session belongs to this client (or is a brand new id, when allowed).

        A deleted or purged id is never new again: the browser holding it is told it is gone.
        """
        memory = get_agent().memory
        stored = memory.owner(session_id)
        if stored == owner:
            return
        if stored is None and allow_new and not memory.exists(session_id) and not memory.was_deleted(session_id):
            return
        raise HTTPException(404, "conversation not found")

    def prepare(request: ChatRequest, session: str, owner: str) -> tuple[Agent, str, list[dict] | None]:
        """Checks and edits before a turn. Runs under the session lock.

        Returns the agent, the masked text and, for an edit, the messages cut from the history
        (run_turn puts them back if the new answer fails).
        """
        agent = get_agent()
        owned(session, owner, allow_new=True)
        removed = None
        if request.replace_from_ts:
            history = agent.memory.load(session)
            cut = next((i for i in range(len(history) - 1, -1, -1) if history[i].get("role") == "user"
                        and history[i].get("ts") == request.replace_from_ts), None)
            if cut is None:
                raise HTTPException(422, "message not found")
            # A protocol was already issued to the team: regenerating would issue a second one.
            if _calls_escalation(history[cut:]):
                raise HTTPException(409, "protocol")
            removed = agent.memory.truncate_from_ts(session, request.replace_from_ts, drop_votes=False)
        with inflight_guard:
            inflight.setdefault(owner, set()).add(session)
        # Card numbers never reach the model provider or the stored history.
        return agent, mask_cards(request.message.strip()), removed

    def settled(session: str, owner: str) -> None:
        """The turn is over (saved or failed). Call before releasing the session lock."""
        with inflight_guard:
            sessions = inflight.get(owner, set())
            sessions.discard(session)
            if not sessions:
                inflight.pop(owner, None)

    def run_turn(agent: Agent, session: str, owner: str, text: str, removed: list[dict] | None,
                 on_event: Callable[[dict], None] | None = None) -> ChatResponse:
        """Answer one turn. An edit's cut only stays when the new answer was saved."""
        def restore() -> None:
            if removed:
                agent.memory.save(session, agent.memory.load(session) + removed)

        try:
            reply = agent.reply(session, text, on_event=on_event)
        except BaseException:
            restore()
            raise
        if reply.error:
            restore()
        elif removed:
            agent.memory.drop_feedback(session, removed)
        return finish(agent, session, owner, text, reply)

    def finish(agent: Agent, session: str, owner: str, text: str, reply: AgentReply) -> ChatResponse:
        memory = agent.memory
        if reply.error:
            info = memory.info(session) or {}
            return ChatResponse(
                session_id=session, title=info.get("title") or _smart_title(text, [], agent.toolbox)[0],
                status=info.get("status") or "open", protocol=info.get("protocol"), reply=reply.text, ts=None,
                error=True, tool_calls=[], cards=[], quick_replies=[],
            )
        extras = _extras(reply.tool_calls)
        title, smart = _smart_title(text, reply.tool_calls, agent.toolbox)
        title = memory.touch(session, owner, title, _preview(reply.text), f"{text}\n{reply.text}", smart=smart)
        if extras["handoff"]:
            memory.set_protocol(session, extras["handoff"]["protocol"])
        info = memory.info(session) or {}
        return ChatResponse(
            session_id=session,
            title=title,
            status=info.get("status") or "open",
            protocol=info.get("protocol"),
            reply=reply.text,
            ts=reply.ts,
            tool_calls=extras["tools"],
            cards=agent.toolbox.product_cards(reply.product_ids),
            quick_replies=reply.quick_replies,
            handoff=extras["handoff"],
            order=extras["order"],
            returns=extras["returns"],
            store=extras["store"],
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    # PWA: the service worker is served from the root so its scope covers the whole app.
    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        return FileResponse(WEB_DIR / "sw.js", media_type="text/javascript",
                            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/products/{product_id}")
    def product(product_id: int) -> dict:
        """One product for the detail sheet: card fields plus specs, and alternatives as cards."""
        toolbox = get_agent().toolbox
        if product_id not in toolbox.store.products:
            raise HTTPException(404, "product not found")
        result = toolbox.get_product(str(product_id))
        if not result.get("found"):
            raise HTTPException(404, "product not found")
        out = {"found": True, "product": {**toolbox.product_cards([product_id])[0], **result["product"]}}
        if "alternatives" in result:
            out["alternatives"] = toolbox.product_cards([a["id"] for a in result["alternatives"]])
        if "availability_note" in result:
            out["availability_note"] = result["availability_note"]
        return out

    @app.get("/api/policy/{topic}")
    def policy(topic: str) -> dict:
        if topic not in POLICY_TOPICS:
            raise HTTPException(404, "topic not found")
        return {"topic": topic, "text": get_agent().toolbox.policies.get(topic)}

    @app.get("/api/store-status")
    def store_status() -> dict:
        return get_agent().toolbox.store_hours_now()

    @app.post("/api/chat", response_model=ChatResponse)
    def chat(request: ChatRequest, x_client_id: str | None = Header(default=None)) -> ChatResponse:
        owner = _check_client(x_client_id)
        session = request.session_id or f"web-{uuid.uuid4().hex[:16]}"
        with locks.hold(session):
            agent, text, removed = prepare(request, session, owner)
            try:
                return run_turn(agent, session, owner, text, removed)
            finally:
                settled(session, owner)

    @app.post("/api/chat/stream")
    def chat_stream(request: ChatRequest, x_client_id: str | None = Header(default=None)) -> StreamingResponse:
        """Same as /api/chat, as server-sent events: one `status` event per tool, then `reply`."""
        owner = _check_client(x_client_id)
        session = request.session_id or f"web-{uuid.uuid4().hex[:16]}"
        release = locks.acquire(session)  # held until the worker thread has saved the turn
        try:
            agent, text, removed = prepare(request, session, owner)
        except BaseException:
            release()
            raise
        events: queue.Queue = queue.Queue()

        def work() -> None:
            try:
                response = run_turn(agent, session, owner, text, removed, on_event=lambda e: events.put(("status", e)))
                events.put(("reply", response.model_dump()))
            except Exception:  # never leave the stream hanging
                log.exception("chat stream failed")
                events.put(("error", {"message": STREAM_ERROR_TEXT, "retryable": True}))
            finally:
                settled(session, owner)
                release()

        threading.Thread(target=work, daemon=True).start()

        def stream():
            while True:
                kind, payload = events.get()
                if kind == "status":
                    # Labels only: tool arguments (phones, e-mails) are never echoed here.
                    payload = {"tool": payload["name"], "label": TOOL_STATUS.get(payload["name"], "Consultando")}
                yield f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if kind != "status":
                    break

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/conversations", response_model=list[ConversationOut])
    def conversations(q: str | None = Query(default=None, max_length=100),
                      x_client_id: str | None = Header(default=None)) -> list[ConversationOut]:
        owner = _check_client(x_client_id)
        return [ConversationOut(**row) for row in get_agent().memory.list_for(owner, q=q)]

    @app.delete("/api/conversations", status_code=204)
    def delete_all(x_client_id: str | None = Header(default=None)) -> None:
        """Deletes each conversation under its lock, so a turn still running cannot bring one back."""
        owner = _check_client(x_client_id)
        memory = get_agent().memory
        with inflight_guard:
            running = set(inflight.get(owner, ()))
        for session in running | set(memory.sessions_of(owner)):
            with locks.hold(session):
                if memory.owner(session) in (owner, None):
                    memory.clear(session)
        memory.clear_client(owner)

    @app.patch("/api/conversations/{session_id}", status_code=204)
    def rename(session_id: str, body: RenameRequest, x_client_id: str | None = Header(default=None)) -> None:
        _check_session(session_id)
        owned(session_id, _check_client(x_client_id))
        get_agent().memory.rename(session_id, " ".join(body.title.split()))

    @app.post("/api/conversations/{session_id}/close", status_code=204)
    def close(session_id: str, body: CloseRequest | None = None,
              x_client_id: str | None = Header(default=None)) -> None:
        _check_session(session_id)
        owner = _check_client(x_client_id)
        body = body or CloseRequest()
        comment = " ".join(body.comment.split()) if body.comment else None
        with locks.hold(session_id):  # after a running turn, which would reopen it
            owned(session_id, owner)
            get_agent().memory.close(session_id, body.rating, comment or None)

    @app.get("/api/chat/{session_id}", response_model=list[HistoryMessage])
    def history(session_id: str, x_client_id: str | None = Header(default=None)) -> list[HistoryMessage]:
        """The conversation as the chat showed it, with each answer's cards and tool extras.

        The extras are rebuilt from the stored tool calls and tool results of each turn.
        """
        _check_session(session_id)
        owned(session_id, _check_client(x_client_id))
        agent = get_agent()
        votes = agent.memory.feedback_for(session_id)
        out: list[HistoryMessage] = []
        calls: dict[str, tuple[str, dict]] = {}
        traces: list[ToolTrace] = []
        for m in agent.memory.load(session_id):
            role = m.get("role")
            if role == "user":
                calls, traces = {}, []
                if m.get("content"):
                    out.append(HistoryMessage(role="user", content=m["content"], ts=m.get("ts")))
            elif role == "assistant" and m.get("tool_calls"):
                for call in m["tool_calls"]:
                    function = call.get("function") or {}
                    calls[call.get("id")] = (function.get("name", ""), _json_dict(function.get("arguments")))
            elif role == "tool":
                name, arguments = calls.get(m.get("tool_call_id"), ("", {}))
                traces.append(ToolTrace(name, arguments, _json_dict(m.get("content"))))
            elif role == "assistant" and m.get("content"):
                text, ids, quick = split_markers(m["content"])
                out.append(HistoryMessage(role="assistant", content=text or FALLBACK_TEXT, ts=m.get("ts"),
                                          cards=agent.toolbox.product_cards(ids), quick_replies=quick,
                                          feedback=votes.get(m.get("ts")), **_extras(traces)))
                calls, traces = {}, []
        return out

    @app.delete("/api/chat/{session_id}", status_code=204)
    def delete(session_id: str, x_client_id: str | None = Header(default=None)) -> None:
        _check_session(session_id)
        owner = _check_client(x_client_id)
        with locks.hold(session_id):  # after a running turn, which would save the old history again
            owned(session_id, owner)
            get_agent().memory.clear(session_id)

    @app.post("/api/feedback", status_code=204)
    def feedback(body: FeedbackRequest, x_client_id: str | None = Header(default=None)) -> None:
        owned(body.session_id, _check_client(x_client_id))
        memory = get_agent().memory
        answers = {m.get("ts") for m in memory.load(body.session_id)
                   if m.get("role") == "assistant" and m.get("content") and not m.get("tool_calls")}
        if body.message_ts not in answers:
            raise HTTPException(422, "message_ts is not an answer in this conversation")
        comment = " ".join(body.comment.split()) if body.comment else None
        memory.set_feedback(body.session_id, body.message_ts, body.rating, body.reason, comment or None)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


app = create_app()
