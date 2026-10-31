"""MCP protocol contracts, through FastMCP's in-memory client."""
from __future__ import annotations

import pytest
from fastmcp import Client

from clicksense.server import build_app

from .conftest import make_settings

WRITES = {"suggest_knowledge", "cancel_query"}


@pytest.fixture
def app(tmp_path):
    return build_app(make_settings(tmp_path))


async def test_every_tool_declares_its_hints(app):
    async with Client(app) as c:
        tools = await c.list_tools()
    assert len(tools) >= 20
    for t in tools:
        ann = t.annotations
        assert ann is not None and ann.read_only_hint is not None, t.name
        assert ann.read_only_hint is (t.name not in WRITES), t.name
        assert ann.open_world_hint is False, t.name
        assert t.description and len(t.description) > 30, t.name


async def test_refusals_set_is_error(app):
    async with Client(app) as c:
        for sql in ("DROP TABLE shop.orders", "SELECT * FROM system.users", "SELECT 1 SETTINGS max_threads=99"):
            r = await c.call_tool("run_query", {"sql": sql}, raise_on_error=False)
            assert r.is_error, sql
            assert r.structured_content["ok"] is False and r.structured_content["hint"]


async def test_resources_prompts_and_skills(app):
    async with Client(app) as c:
        uris = {str(r.uri) for r in await c.list_resources()}
        assert {"clicksense://guide", "clicksense://glossary", "clicksense://metrics"} <= uris
        assert "skill://answer-data-question/SKILL.md" in uris
        templates = {t.uri_template for t in await c.list_resource_templates()}
        assert "clicksense://tables/{database}/{table}" in templates
        prompts = {p.name for p in await c.list_prompts()}
        assert "analyze" in prompts and "skill-investigate-metric-change" in prompts
        text = (await c.read_resource("clicksense://knowledge/revenue-rules"))[0].text
        assert "completed" in text
        skill = (await c.read_resource("skill://answer-data-question/SKILL.md"))[0].text
        assert "get_context" in skill
        p = await c.get_prompt("skill-investigate-metric-change", {"question": "why did revenue drop?"})
        assert "why did revenue drop" in p.messages[0].content.text


async def test_clarify_without_elicitation_falls_back(app):
    async with Client(app) as c:
        r = await c.call_tool("ask_user_to_clarify", {"question": "CA?", "options": ["Revenue", "Canada"]})
    assert r.structured_content["status"] == "needs_clarification"


async def test_clarify_with_elicitation(app):
    async def handler(message, response_type, params, context):
        return "Canada"

    async with Client(app, elicitation_handler=handler) as c:
        r = await c.call_tool("ask_user_to_clarify", {"question": "CA?", "options": ["Revenue", "Canada"]})
    assert r.structured_content == {"ok": True, "status": "answered", "choice": "Canada"}
