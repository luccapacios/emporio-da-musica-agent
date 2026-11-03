"""HTTP API and web chat.

Run: uvicorn emporio.api:app --reload   then open http://localhost:8000

There are no accounts. The browser generates an unguessable client id and sends it as the
X-Client-Id header; every conversation is tied to the client id that started it, and a
request for a conversation owned by another id gets a 404, as if it did not exist.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent, AgentReply, build_agent, split_markers
from .config import ROOT, get_settings

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


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, pattern=SESSION_PATTERN)


class ToolCallOut(BaseModel):
    name: str
    arguments: dict


class ChatResponse(BaseModel):
    session_id: str
    title: str
    reply: str
    ts: str | None
    tool_calls: list[ToolCallOut]
    cards: list[dict]
    quick_replies: list[str]
    handoff: dict | None = None
    order: dict | None = None


class HistoryMessage(BaseModel):
    role: str
    content: str
    ts: str | None = None
    cards: list[dict] = []
    quick_replies: list[str] = []


class ConversationOut(BaseModel):
    session_id: str
    title: str
    preview: str
    created_at: str
    updated_at: str


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=80)


class FeedbackRequest(BaseModel):
    session_id: str = Field(pattern=SESSION_PATTERN)
    message_ts: str | None = Field(default=None, max_length=40)
    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=500)


def _title(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= 60 else flat[:57].rstrip() + "..."


def _preview(text: str) -> str:
    flat = " ".join(text.replace("*", "").split())
    return flat if len(flat) <= 90 else flat[:87].rstrip() + "..."


def _check_session(session_id: str) -> None:
    if not re.fullmatch(SESSION_PATTERN, session_id):
        raise HTTPException(422, "invalid session id")


def _check_client(client_id: str | None) -> str:
    if not client_id or not re.fullmatch(CLIENT_PATTERN, client_id):
        raise HTTPException(400, "missing or invalid X-Client-Id header")
    return client_id


def create_app(agent: Agent | None = None) -> FastAPI:
    app = FastAPI(title="Empório da Música assistant")
    state: dict[str, Agent | None] = {"agent": agent}

    def get_agent() -> Agent:
        if state["agent"] is None:
            settings = get_settings()
            if not settings.llm_api_key:
                raise HTTPException(503, "LLM_API_KEY is not configured. See .env.example.")
            state["agent"] = build_agent(settings)
        return state["agent"]

    def owned(session_id: str, client_id: str, allow_new: bool = False) -> None:
        """404 unless the session belongs to this client (or is a brand new id, when allowed)."""
        memory = get_agent().memory
        owner = memory.owner(session_id)
        if owner == client_id:
            return
        if owner is None and allow_new and not memory.exists(session_id):
            return
        raise HTTPException(404, "conversation not found")

    def start(request: ChatRequest, client_id: str | None) -> tuple[Agent, str, str]:
        client = _check_client(client_id)
        session = request.session_id or f"web-{uuid.uuid4().hex[:16]}"
        owned(session, client, allow_new=True)
        return get_agent(), session, client

    def finish(agent: Agent, session: str, client: str, text: str, reply: AgentReply) -> ChatResponse:
        if not reply.error:
            agent.memory.touch(session, client, _title(text), _preview(reply.text))
        handoff = next((t.result for t in reply.tool_calls
                        if t.name == "escalate_to_human" and "protocol" in t.result), None)
        # Only an order the tool verified in this very turn is sent back for the order card.
        order = next(({"order": t.result["order"], "delivery": t.result.get("delivery", {})}
                      for t in reversed(reply.tool_calls)
                      if t.name in ("get_order", "check_return") and t.result.get("verified")), None)
        return ChatResponse(
            session_id=session,
            title=_title(text),
            reply=reply.text,
            ts=reply.ts,
            tool_calls=[ToolCallOut(name=t.name, arguments=t.arguments) for t in reply.tool_calls],
            cards=agent.toolbox.product_cards(reply.product_ids),
            quick_replies=reply.quick_replies,
            handoff=handoff,
            order=order,
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
        result = get_agent().toolbox.get_product(str(product_id))
        if not result.get("found"):
            raise HTTPException(404, "product not found")
        return result

    @app.get("/api/store-status")
    def store_status() -> dict:
        return get_agent().toolbox.store_hours_now()

    @app.post("/api/chat", response_model=ChatResponse)
    def chat(request: ChatRequest, x_client_id: str | None = Header(default=None)) -> ChatResponse:
        agent, session, client = start(request, x_client_id)
        text = request.message.strip()
        return finish(agent, session, client, text, agent.reply(session, text))

    @app.post("/api/chat/stream")
    def chat_stream(request: ChatRequest, x_client_id: str | None = Header(default=None)) -> StreamingResponse:
        """Same as /api/chat, as server-sent events: one `status` event per tool, then `reply`."""
        agent, session, client = start(request, x_client_id)
        text = request.message.strip()
        events: queue.Queue = queue.Queue()

        def work() -> None:
            try:
                reply = agent.reply(session, text, on_event=lambda e: events.put(("status", e)))
                events.put(("reply", finish(agent, session, client, text, reply).model_dump()))
            except Exception as exc:  # never leave the stream hanging
                events.put(("error", {"message": f"{type(exc).__name__}"}))

        threading.Thread(target=work, daemon=True).start()

        def stream():
            while True:
                kind, payload = events.get()
                if kind == "status":
                    payload = {"tool": payload["name"], "label": TOOL_STATUS.get(payload["name"], "Consultando")}
                yield f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if kind != "status":
                    break

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/conversations", response_model=list[ConversationOut])
    def conversations(x_client_id: str | None = Header(default=None)) -> list[ConversationOut]:
        client = _check_client(x_client_id)
        return [ConversationOut(**row) for row in get_agent().memory.list_for(client)]

    @app.patch("/api/conversations/{session_id}", status_code=204)
    def rename(session_id: str, body: RenameRequest, x_client_id: str | None = Header(default=None)) -> None:
        _check_session(session_id)
        owned(session_id, _check_client(x_client_id))
        get_agent().memory.rename(session_id, " ".join(body.title.split()))

    @app.get("/api/chat/{session_id}", response_model=list[HistoryMessage])
    def history(session_id: str, x_client_id: str | None = Header(default=None)) -> list[HistoryMessage]:
        _check_session(session_id)
        owned(session_id, _check_client(x_client_id))
        agent = get_agent()
        out = []
        for m in agent.memory.load(session_id):
            if m.get("role") not in ("user", "assistant") or not m.get("content"):
                continue
            if m["role"] == "user":
                out.append(HistoryMessage(role="user", content=m["content"], ts=m.get("ts")))
            else:
                text, ids, quick = split_markers(m["content"])
                out.append(HistoryMessage(role="assistant", content=text, ts=m.get("ts"),
                                          cards=agent.toolbox.product_cards(ids), quick_replies=quick))
        return out

    @app.delete("/api/chat/{session_id}", status_code=204)
    def delete(session_id: str, x_client_id: str | None = Header(default=None)) -> None:
        _check_session(session_id)
        owned(session_id, _check_client(x_client_id))
        get_agent().memory.clear(session_id)

    @app.post("/api/feedback", status_code=204)
    def feedback(body: FeedbackRequest, x_client_id: str | None = Header(default=None)) -> None:
        owned(body.session_id, _check_client(x_client_id))
        record = {**body.model_dump(), "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        path = get_agent().memory.path.parent / "feedback.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


app = create_app()
