"""HTTP API and web chat.

Run: uvicorn emporio.api:app --reload   then open http://localhost:8000
"""

from __future__ import annotations

import re
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .agent import Agent, build_agent
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


class HistoryMessage(BaseModel):
    role: str
    content: str


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

    @app.post("/api/chat", response_model=ChatResponse)
    def chat(request: ChatRequest) -> ChatResponse:
        session = request.session_id or f"web-{uuid.uuid4().hex[:12]}"
        reply = get_agent().reply(session, request.message.strip())
        return ChatResponse(
            session_id=session,
            reply=reply.text,
            tool_calls=[ToolCallOut(name=t.name, arguments=t.arguments) for t in reply.tool_calls],
        )

    @app.get("/api/chat/{session_id}", response_model=list[HistoryMessage])
    def history(session_id: str) -> list[HistoryMessage]:
        _check_session(session_id)
        messages = get_agent().memory.load(session_id)
        return [HistoryMessage(role=m["role"], content=m["content"])
                for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]

    @app.delete("/api/chat/{session_id}", status_code=204)
    def reset(session_id: str) -> None:
        _check_session(session_id)
        get_agent().memory.clear(session_id)

    return app


def _check_session(session_id: str) -> None:
    if not re.fullmatch(SESSION_PATTERN, session_id):
        raise HTTPException(422, "invalid session id")


app = create_app()
