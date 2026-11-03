"""HTTP API and web chat.

Run: uvicorn emporio.api:app --reload   then open http://localhost:8000
"""

from __future__ import annotations

import re
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent, build_agent, split_markers
from .config import ROOT, get_settings

WEB_DIR = ROOT / "web"
SESSION_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, pattern=SESSION_PATTERN)


class ToolCallOut(BaseModel):
    name: str
    arguments: dict


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    tool_calls: list[ToolCallOut]
    cards: list[dict]
    quick_replies: list[str]


class HistoryMessage(BaseModel):
    role: str
    content: str
    cards: list[dict] = []


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

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/store-status")
    def store_status() -> dict:
        return get_agent().toolbox.store_hours_now()

    @app.post("/api/chat", response_model=ChatResponse)
    def chat(request: ChatRequest) -> ChatResponse:
        agent = get_agent()
        session = request.session_id or f"web-{uuid.uuid4().hex[:12]}"
        reply = agent.reply(session, request.message.strip())
        return ChatResponse(
            session_id=session,
            reply=reply.text,
            tool_calls=[ToolCallOut(name=t.name, arguments=t.arguments) for t in reply.tool_calls],
            cards=agent.toolbox.product_cards(reply.product_ids),
            quick_replies=reply.quick_replies,
        )

    @app.get("/api/chat/{session_id}", response_model=list[HistoryMessage])
    def history(session_id: str) -> list[HistoryMessage]:
        _check_session(session_id)
        agent = get_agent()
        out = []
        for m in agent.memory.load(session_id):
            if m.get("role") not in ("user", "assistant") or not m.get("content"):
                continue
            if m["role"] == "user":
                out.append(HistoryMessage(role="user", content=m["content"]))
            else:
                text, ids, _ = split_markers(m["content"])
                out.append(HistoryMessage(role="assistant", content=text, cards=agent.toolbox.product_cards(ids)))
        return out

    @app.delete("/api/chat/{session_id}", status_code=204)
    def reset(session_id: str) -> None:
        _check_session(session_id)
        get_agent().memory.clear(session_id)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


def _check_session(session_id: str) -> None:
    if not re.fullmatch(SESSION_PATTERN, session_id):
        raise HTTPException(422, "invalid session id")


app = create_app()
