"""The agent: a tool calling loop over any OpenAI compatible chat completions API."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

import openai
from openai import OpenAI

from .config import Settings
from .memory import ConversationStore, trim_history
from .prompts import build_system_prompt
from .tools import ToolBox

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6
MAX_USER_TURNS = 12

FALLBACK_TEXT = (
    "Desculpa, me enrolei aqui para responder isso. Quer que eu passe seu atendimento para "
    "alguém da equipe?"
)
PROVIDER_ERROR_TEXT = (
    "Tive um problema técnico para responder agora. Pode tentar de novo em instantes? "
    "Se preferir, fale com a gente pelo (67) 3341-4444."
)


# Display hints the model appends to its reply (see prompts.py): product ids to show as
# cards and short quick replies. They are stripped before the customer sees the text.
MARKER = re.compile(r"\[\[\s*(cards|quick)\s*:\s*(.*?)\]\]", re.IGNORECASE | re.DOTALL)
MAX_CARDS = 6
MAX_QUICK_REPLIES = 3


def split_markers(text: str) -> tuple[str, list[int], list[str]]:
    """Return (clean text, product ids, quick replies) from a raw model reply."""
    ids: list[int] = []
    quick: list[str] = []
    for kind, body in MARKER.findall(text):
        if kind.lower() == "cards":
            ids += [int(n) for n in re.findall(r"\d+", body)]
        else:
            quick += [q.strip() for q in body.split("|") if q.strip()]
    clean = re.sub(r"\n{3,}", "\n\n", MARKER.sub("", text)).strip()
    return clean, list(dict.fromkeys(ids))[:MAX_CARDS], [q[:40] for q in quick[:MAX_QUICK_REPLIES]]


@dataclass
class ToolTrace:
    name: str
    arguments: dict
    result: dict


@dataclass
class AgentReply:
    session_id: str
    text: str
    tool_calls: list[ToolTrace] = field(default_factory=list)
    error: str | None = None
    product_ids: list[int] = field(default_factory=list)
    quick_replies: list[str] = field(default_factory=list)
    ts: str | None = None  # when the reply was saved (store time, ISO 8601)


class Agent:
    def __init__(self, settings: Settings, toolbox: ToolBox, client=None, memory: ConversationStore | None = None):
        self.settings = settings
        self.toolbox = toolbox
        self.memory = memory
        self.client = client or OpenAI(
            base_url=settings.llm_base_url, api_key=settings.llm_api_key or "missing", timeout=60, max_retries=3
        )
        self._tools = toolbox.schemas()

    def reply(self, session_id: str, text: str, on_event: Callable[[dict], None] | None = None) -> AgentReply:
        """Answer one customer message. `on_event` hears about each tool call as it starts."""
        history = self.memory.load(session_id) if self.memory else []
        system = {"role": "system", "content": build_system_prompt(self.toolbox.clock())}
        user = {"role": "user", "content": text}
        # Stored messages carry a timestamp for the chat UI; the model never sees it.
        new_messages: list[dict] = [{**user, "ts": self._stamp()}]
        messages = [system, *map(_for_model, trim_history(history, MAX_USER_TURNS)), user]
        traces: list[ToolTrace] = []

        try:
            answer = None
            for _ in range(MAX_TOOL_ROUNDS):
                message = self._complete(messages)
                # model_dump keeps provider specific fields, such as Gemini's thought
                # signatures on tool calls, which must be sent back on the next request.
                assistant = message.model_dump(exclude_none=True)
                messages.append(assistant)
                if not message.tool_calls:
                    answer = (message.content or "").strip()
                    new_messages.append({**assistant, "ts": self._stamp()})
                    break
                new_messages.append(assistant)
                for call in message.tool_calls:
                    arguments = _parse_arguments(call.function.arguments)
                    if on_event:
                        on_event({"type": "tool", "name": call.function.name, "arguments": arguments})
                    result = self.toolbox.call(call.function.name, arguments)
                    traces.append(ToolTrace(call.function.name, arguments, result))
                    tool_message = {"role": "tool", "tool_call_id": call.id,
                                    "content": json.dumps(result, ensure_ascii=False)}
                    messages.append(tool_message)
                    new_messages.append(tool_message)
        except openai.OpenAIError as exc:
            log.exception("model provider error")
            return AgentReply(session_id, PROVIDER_ERROR_TEXT, traces, error=f"{type(exc).__name__}: {exc}")

        if not answer:
            answer = FALLBACK_TEXT
            new_messages.append({"role": "assistant", "content": answer, "ts": self._stamp()})
        if self.memory:
            self.memory.save(session_id, history + new_messages)
        text, product_ids, quick = split_markers(answer)
        known = [i for i in product_ids if i in self.toolbox.store.products]
        return AgentReply(session_id, text or FALLBACK_TEXT, traces, product_ids=known, quick_replies=quick,
                          ts=new_messages[-1].get("ts"))

    def _stamp(self) -> str:
        return self.toolbox.clock().isoformat(timespec="seconds")

    def _complete(self, messages: list[dict]):
        response = self.client.chat.completions.create(
            model=self.settings.llm_model,
            messages=messages,
            tools=self._tools,
            temperature=self.settings.temperature,
        )
        return response.choices[0].message


def reply_with_retry(agent: Agent, session_id: str, text: str, attempts: int = 3, wait: float = 60.0) -> AgentReply:
    """For batch scripts on rate limited tiers: retry a turn that hit a rate limit.

    A failed turn is not saved to memory, so retrying never duplicates history.
    """
    for attempt in range(attempts):
        reply = agent.reply(session_id, text)
        if not (reply.error and "RateLimit" in reply.error) or attempt == attempts - 1:
            return reply
        log.warning("rate limited; retrying in %.0fs", wait)
        time.sleep(wait)
    return reply


def _for_model(message: dict) -> dict:
    return {k: v for k, v in message.items() if k != "ts"}


def _parse_arguments(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_agent(settings: Settings) -> Agent:
    """Wire the agent with the real data, policy and memory."""
    from .config import make_clock
    from .data import load_store
    from .policy import Policies

    store = load_store(settings.data_dir / "raw")
    policies = Policies.load(settings.data_dir / "processed" / "policies.md")
    toolbox = ToolBox(store, policies, make_clock(settings), settings.runtime_dir / "handoffs.jsonl")
    memory = ConversationStore(settings.runtime_dir / "conversations.db")
    return Agent(settings, toolbox, memory=memory)
