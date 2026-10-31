"""`clicksense-chat`: talk to your ClickHouse data through any OpenAI-compatible LLM.

    export OPENAI_API_KEY=sk-...
    clicksense-chat                                   # interactive
    clicksense-chat -q "Revenue by channel last month"   # one question
    clicksense-chat --base-url https://api.mistral.ai/v1 --model mistral-large-latest

A `.env` file next to the config (or in the current directory) is read for OPENAI_API_KEY,
OPENAI_BASE_URL, CLICKSENSE_LLM_MODEL... Values already set in the environment win.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import warnings

import anyio

from .agent import Agent, connect, resolve_base_url
from .config import ConfigError, load_settings

HELP = "/reset new conversation · /tools list tools · /exit quit"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="clicksense-chat", description="Chat with ClickHouse through an "
                                 "OpenAI-compatible LLM and the ClickSense MCP tools.")
    ap.add_argument("--config", help="clicksense.yaml (default: $CLICKSENSE_CONFIG or ./clicksense.yaml)")
    ap.add_argument("-q", "--question", help="Ask one question and exit.")
    ap.add_argument("--model", help="Model name (overrides llm.model / $CLICKSENSE_LLM_MODEL).")
    ap.add_argument("--base-url", help="OpenAI-compatible base URL (overrides llm.base_url / $OPENAI_BASE_URL).")
    ap.add_argument("--mcp-url", help="Use a running ClickSense HTTP server instead of in-process.")
    ap.add_argument("--quiet", action="store_true", help="Hide the tool-call trace.")
    args = ap.parse_args(argv)

    warnings.filterwarnings("ignore")
    import logging

    logging.getLogger("fastmcp").setLevel(logging.ERROR)  # the model sees refusals; no need to print them twice
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"clicksense-chat: {exc}", file=sys.stderr)
        raise SystemExit(2)
    llm = settings.llm
    llm.model = args.model or os.environ.get("CLICKSENSE_LLM_MODEL") or llm.model
    llm.base_url = args.base_url or llm.base_url
    llm.mcp_url = args.mcp_url or llm.mcp_url
    try:
        asyncio.run(_run(settings, args.question, args.quiet))
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(f"clicksense-chat: {exc}", file=sys.stderr)
        raise SystemExit(2)


async def _run(settings, question: str | None, quiet: bool) -> None:
    from rich.console import Console
    from rich.markdown import Markdown

    console = Console()

    def on_event(kind: str, data: dict) -> None:
        if quiet:
            return
        if kind == "tool_call":
            args = json.dumps(data["arguments"], ensure_ascii=False)
            console.print(f"  [dim]→ {data['name']}({args[:160]}{'…' if len(args) > 160 else ''})[/dim]")
        elif kind == "tool_result" and data["is_error"]:
            console.print(f"  [dim yellow]  {data['name']} refused/failed (the model sees why)[/dim yellow]")
        elif kind == "tool_error":
            console.print(f"  [dim red]  {data['name']}: {data['error'][:200]}[/dim red]")

    async def ask_user(message: str, options: list[str]) -> str | None:
        console.print(f"\n[bold yellow]?[/bold yellow] {message}")
        for i, o in enumerate(options, 1):
            console.print(f"  {i}. {o}")
        raw = (await anyio.to_thread.run_sync(input, "Your choice (number or text, empty to skip): ")).strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        return raw or None

    async with connect(settings, ask_user) as client:
        agent = Agent(settings.llm, client, on_event=on_event)
        await agent.start()
        where = settings.llm.mcp_url or "in-process"
        console.print(f"[dim]ClickSense · {len(agent.tools)} tools ({where}) · model {settings.llm.model} "
                      f"@ {resolve_base_url(settings.llm)}[/dim]")

        async def turn(q: str) -> None:
            with console.status("thinking…", spinner="dots") if quiet else _null():
                answer = await agent.ask(q)
            console.print()
            console.print(Markdown(answer or "_(empty answer)_"))
            console.print()

        if question:
            await turn(question)
            return
        console.print(f"[dim]{HELP}[/dim]\n")
        while True:
            try:
                q = (await anyio.to_thread.run_sync(input, "you › ")).strip()
            except EOFError:
                break
            if not q:
                continue
            if q in ("/exit", "/quit"):
                break
            if q == "/reset":
                agent.reset()
                console.print("[dim]New conversation.[/dim]")
                continue
            if q == "/tools":
                console.print(", ".join(t["function"]["name"] for t in agent.tools))
                continue
            try:
                await turn(q)
            except Exception as exc:  # LLM/API errors: show and keep the session
                console.print(f"[red]LLM error:[/red] {type(exc).__name__}: {str(exc)[:400]}")


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


if __name__ == "__main__":
    main()
