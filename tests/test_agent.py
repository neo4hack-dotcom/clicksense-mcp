"""The OpenAI-compatible bridge, with a scripted fake LLM (no API key, no network)."""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from clicksense.agent import Agent, connect, resolve_api_key, to_openai_tool, tool_payload
from clicksense.config import LLM

from .conftest import make_settings


def call(cid, name, args):
    return NS(id=cid, type="function", function=NS(name=name, arguments=args if isinstance(args, str)
                                                    else json.dumps(args)))


def reply(content=None, calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls))])


class FakeLLM:
    """Plays back scripted replies and records what it was sent."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.chat = NS(completions=NS(create=self.create))

    async def create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs, default=str)))
        return self.script.pop(0)


async def run(tmp_path, script, question, ask_user=None, **llm):
    settings = make_settings(tmp_path)
    settings.llm = LLM(model="fake", **llm)
    fake = FakeLLM(script)
    events = []
    async with connect(settings, ask_user) as client:
        agent = Agent(settings.llm, client, openai_client=fake, on_event=lambda k, d: events.append((k, d)))
        await agent.start()
        answer = await agent.ask(question)
    return answer, fake, events, agent


async def test_tools_are_exposed_as_openai_functions(tmp_path):
    _, fake, _, agent = await run(tmp_path, [reply("ok")], "hi", auto_context=False)
    names = {t["function"]["name"] for t in fake.requests[0]["tools"]}
    assert {"get_context", "query_metric", "run_query", "ask_user_to_clarify"} <= names
    assert all(t["type"] == "function" and t["function"]["parameters"]["type"] == "object" for t in agent.tools)
    assert "get_context" in fake.requests[0]["messages"][0]["content"]  # server instructions in system prompt


async def test_tool_loop_and_auto_context(tmp_path):
    script = [reply(calls=[call("c1", "list_metrics", {})]), reply("Revenue is the main metric.")]
    answer, fake, events, agent = await run(tmp_path, script, "what metrics exist?")
    assert answer == "Revenue is the main metric."
    roles = [m["role"] for m in fake.requests[0]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]  # get_context injected before turn 1
    injected = fake.requests[0]["messages"][2]["tool_calls"][0]["function"]
    assert injected["name"] == "get_context" and json.loads(injected["arguments"]) == {"question": "what metrics exist?"}
    second = fake.requests[1]["messages"]
    tool_msg = next(m for m in second if m.get("tool_call_id") == "c1")
    assert "revenue" in tool_msg["content"]
    assert [k for k, _ in events].count("tool_call") == 2


async def test_refusals_and_bad_arguments_reach_the_model(tmp_path):
    script = [reply(calls=[call("a", "run_query", {"sql": "DROP TABLE shop.orders"}),
                           call("b", "run_query", "{not json")]),
              reply("I cannot do that.")]
    _, fake, events, _ = await run(tmp_path, script, "drop it", auto_context=False)
    msgs = {m.get("tool_call_id"): m["content"] for m in fake.requests[1]["messages"] if m["role"] == "tool"}
    assert json.loads(msgs["a"])["error"] == "not_a_select"
    assert json.loads(msgs["b"])["error"] == "bad_arguments"


async def test_clarification_reaches_the_user(tmp_path):
    asked = {}

    async def ask_user(message, options):
        asked.update(message=message, options=options)
        return options[1]

    script = [reply(calls=[call("q", "ask_user_to_clarify",
                                {"question": "Which sales?", "options": ["Revenue", "Orders", "Units"]})]),
              reply("Orders it is.")]
    _, fake, _, _ = await run(tmp_path, script, "sales?", ask_user=ask_user, auto_context=False)
    assert asked == {"message": "Which sales?", "options": ["Revenue", "Orders", "Units"]}
    result = next(m for m in fake.requests[1]["messages"] if m.get("tool_call_id") == "q")
    assert json.loads(result["content"])["choice"] == "Orders"


async def test_max_steps_stops_a_looping_model(tmp_path):
    script = [reply(calls=[call(f"c{i}", "list_tables", {})]) for i in range(3)]
    answer, _, events, _ = await run(tmp_path, script, "loop", auto_context=False, max_steps=3)
    assert "Stopped after 3" in answer and events[-1][0] == "max_steps"


def test_helpers(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CLICKSENSE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert resolve_api_key(LLM(base_url="http://localhost:11434/v1")) == "not-needed"
    with pytest.raises(RuntimeError):
        resolve_api_key(LLM())
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert resolve_api_key(LLM()) == "sk-test"
    big = NS(structured_content={"rows": ["x" * 100] * 100}, content=[], is_error=False)
    assert "truncated" in tool_payload(big, 500)
    tool = NS(name="t", description="d", input_schema={"$schema": "x", "title": "T", "type": "object"})
    assert to_openai_tool(tool)["function"]["parameters"] == {"type": "object", "properties": {}}
