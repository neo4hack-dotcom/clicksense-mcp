"""Use ClickSense from any OpenAI-compatible LLM (OpenAI, Azure OpenAI, Mistral, Groq, DeepSeek,
OpenRouter, vLLM, Ollama, LM Studio...).

The model does not speak MCP; this module is the MCP *client* in between. It lists the
ClickSense tools, hands them to the model as OpenAI function tools, executes the calls the model
makes, and feeds the results back until the model answers:

    question -> [chat.completions with tools] -> tool_calls -> MCP call_tool -> results -> ... -> answer

The server instructions become the system prompt, so the model follows the same workflow as
Claude does (get_context first, settle ambiguities, query_metric when a metric fits...).
`ask_user_to_clarify` reaches the person through `ask_user`, the elicitation callback.

Embed it:

    async with connect(settings) as client:
        agent = Agent(settings.llm, client)
        await agent.start()
        print(await agent.ask("What was revenue in France last month?"))
"""
from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import mcp.types as mcp_types
from fastmcp import Client

from .config import LLM, Settings

AskUser = Callable[[str, list[str]], Awaitable[str | None]]
OnEvent = Callable[[str, dict[str, Any]], None]

GUIDANCE = """\
You are a data analyst answering questions about a ClickHouse warehouse through the tools provided.
Rules:
- Call tools to get facts; never invent table names, columns, values or numbers.
- A get_context result for the question is provided before your first turn (or call get_context
  yourself). If its `ambiguities` list is not empty, you MUST settle each one before any query:
  call ask_user_to_clarify with the senses as options, unless the question itself already decides
  it. Never pick a meaning silently.
- Prefer query_metric when a governed metric fits; otherwise validate_query before run_query.
- In the final answer, give the figures, then the definition used (metric, filters, period) and
  any caveat. Answer in the language of the question.
"""


def resolve_api_key(llm: LLM) -> str:
    key = llm.api_key or os.environ.get("CLICKSENSE_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    base = resolve_base_url(llm)
    if any(h in base for h in ("localhost", "127.0.0.1", "[::1]")):
        return "not-needed"  # local servers (Ollama, LM Studio, vLLM) ignore the key
    raise RuntimeError("No API key: set OPENAI_API_KEY (or CLICKSENSE_LLM_API_KEY, or llm.api_key in the config).")


def resolve_base_url(llm: LLM) -> str:
    return llm.base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"


def to_openai_tool(tool: mcp_types.Tool) -> dict[str, Any]:
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {"type": "object"}
    schema = {k: v for k, v in schema.items() if k not in ("$schema", "title")}
    schema.setdefault("properties", {})
    return {"type": "function",
            "function": {"name": tool.name, "description": (tool.description or "")[:1024], "parameters": schema}}


def tool_payload(result: Any, limit: int) -> str:
    """MCP tool result -> compact text for the model, truncated to `limit` characters."""
    data = getattr(result, "structured_content", None)
    if data is not None:
        text = json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))
    else:
        text = "\n".join(getattr(c, "text", "") for c in (getattr(result, "content", None) or []))
    if getattr(result, "is_error", False) and data is None:
        text = json.dumps({"ok": False, "error": text})
    if len(text) > limit:
        text = text[:limit] + f'... [truncated {len(text) - limit} chars: ask for fewer rows or aggregate]'
    return text


def elicitation_handler(ask_user: AskUser):
    """MCP elicitation -> ask_user(question, options). Used by ask_user_to_clarify."""

    async def handler(message, response_type, params, context):
        schema = getattr(params, "requested_schema", None) or getattr(params, "requestedSchema", None) or {}
        prop = (schema.get("properties") or {}).get("value", {})
        options = list(prop.get("enum") or [o.get("const") for o in prop.get("oneOf", []) if "const" in o])
        answer = await ask_user(message, options)
        if not answer:
            return mcp_types.ElicitResult(action="decline")
        return answer

    return handler


@contextlib.asynccontextmanager
async def connect(settings: Settings, ask_user: AskUser | None = None) -> AsyncIterator[Client]:
    """An MCP client on ClickSense: in-process by default, or over HTTP when llm.mcp_url is set."""
    handler = elicitation_handler(ask_user) if ask_user else None
    if settings.llm.mcp_url:
        target: Any = settings.llm.mcp_url
    else:
        from .server import build_app

        target = build_app(settings)
    async with Client(target, elicitation_handler=handler) as client:
        yield client


class Agent:
    def __init__(self, llm: LLM, client: Client, *, openai_client: Any = None, on_event: OnEvent | None = None):
        self.llm = llm
        self.client = client
        self.on_event = on_event or (lambda kind, data: None)
        if openai_client is None:
            from openai import AsyncOpenAI

            openai_client = AsyncOpenAI(api_key=resolve_api_key(llm), base_url=resolve_base_url(llm),
                                        timeout=llm.timeout_s, default_headers=llm.extra_headers or None)
        self.openai = openai_client
        self.tools: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    async def start(self) -> None:
        self.tools = [to_openai_tool(t) for t in await self.client.list_tools()]
        init = getattr(self.client, "initialize_result", None)
        instructions = getattr(init, "instructions", None) or ""
        self.system = f"{GUIDANCE}\n\nServer instructions:\n{instructions}".strip()
        self.reset()

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self.system}]

    async def ask(self, question: str) -> str:
        """One user turn. Conversation history is kept, so follow-up questions work."""
        self.messages.append({"role": "user", "content": question})
        if self.llm.auto_context:
            await self._inject("get_context", {"question": question})
        for step in range(self.llm.max_steps):
            kwargs: dict[str, Any] = {"model": self.llm.model, "messages": self.messages, "tools": self.tools}
            if self.llm.temperature is not None:
                kwargs["temperature"] = self.llm.temperature
            resp = await self.openai.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            calls = list(msg.tool_calls or [])
            entry: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if calls:
                entry["tool_calls"] = [{"id": c.id, "type": "function",
                                        "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                                       for c in calls]
            self.messages.append(entry)
            if not calls:
                self.on_event("answer", {"steps": step})
                return msg.content or ""
            for c in calls:
                self.messages.append({"role": "tool", "tool_call_id": c.id, "content": await self._run(c)})
        self.on_event("max_steps", {"steps": self.llm.max_steps})
        return (f"(Stopped after {self.llm.max_steps} tool rounds without a final answer. "
                "Try a narrower question or raise llm.max_steps.)")

    async def _inject(self, name: str, args: dict[str, Any]) -> None:
        """Run a tool on the model's behalf and record it as if the model had called it."""
        self._seq = getattr(self, "_seq", 0) + 1
        call_id = f"auto_{self._seq}"
        call = _Call(call_id, name, json.dumps(args, ensure_ascii=False))
        self.messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": call.function.arguments}}]})
        self.messages.append({"role": "tool", "tool_call_id": call_id, "content": await self._run(call)})

    async def _run(self, call: Any) -> str:
        name = call.function.name
        try:
            args = json.loads(call.function.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as exc:
            self.on_event("tool_error", {"name": name, "error": f"bad arguments: {exc}"})
            return json.dumps({"ok": False, "error": "bad_arguments", "message": str(exc)})
        self.on_event("tool_call", {"name": name, "arguments": args})
        try:
            result = await self.client.call_tool(name, args, raise_on_error=False)
        except Exception as exc:  # unknown tool, schema validation, transport
            self.on_event("tool_error", {"name": name, "error": str(exc)})
            return json.dumps({"ok": False, "error": "tool_failed", "message": str(exc)[:500]})
        payload = tool_payload(result, self.llm.max_tool_chars)
        self.on_event("tool_result", {"name": name, "is_error": bool(getattr(result, "is_error", False)),
                                      "chars": len(payload)})
        return payload


class _Fn:
    def __init__(self, name: str, arguments: str):
        self.name, self.arguments = name, arguments


class _Call:
    def __init__(self, call_id: str, name: str, arguments: str):
        self.id, self.function = call_id, _Fn(name, arguments)
