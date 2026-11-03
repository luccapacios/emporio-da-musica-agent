"""Terminal chat with Duda.

Usage:
    python -m emporio.cli                 # new conversation
    python -m emporio.cli --session ID    # resume a saved conversation
    python -m emporio.cli --debug         # also print the tools the agent called
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from rich.console import Console
from rich.markup import escape

from .agent import build_agent
from .config import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chat with the Empório da Música assistant.")
    parser.add_argument("--session", help="session id to resume")
    parser.add_argument("--new", action="store_true", help="clear the given session before starting")
    parser.add_argument("--debug", action="store_true", help="show tool calls")
    args = parser.parse_args(argv)

    console = Console()
    settings = get_settings()
    if not settings.llm_api_key:
        console.print("[red]LLM_API_KEY is not set.[/] Copy .env.example to .env and add your key.")
        return 1

    agent = build_agent(settings)
    session = args.session or f"cli-{uuid.uuid4().hex[:8]}"
    if args.new:
        agent.memory.clear(session)

    console.print("[bold]Empório da Música[/] · chat com a Duda, assistente virtual")
    console.print(f"[dim]sessão {session} · modelo {settings.llm_model} · /sair para encerrar[/]\n")

    while True:
        try:
            text = console.input("[bold]Você:[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text.lower() in ("/sair", "/exit", "/quit"):
            break
        with console.status("Duda está digitando..."):
            reply = agent.reply(session, text)
        if args.debug:
            for call in reply.tool_calls:
                console.print(f"  [dim]consultou {call.name}({escape(json.dumps(call.arguments, ensure_ascii=False))})[/]")
            if reply.error:
                console.print(f"  [red]{escape(reply.error)}[/]")
        console.print(f"[bold #1B4F72]Duda:[/] {escape(reply.text)}")
        for card in agent.toolbox.product_cards(reply.product_ids):
            console.print(f"  [#C8813A]▪[/] {escape(card['name'])}: {escape(card['display'])}")
        if reply.quick_replies:
            console.print(f"  [dim]sugestões: {escape(' | '.join(reply.quick_replies))}[/]")
        console.print()

    console.print(f"\n[dim]Conversa salva. Para continuar: python -m emporio.cli --session {session}[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
