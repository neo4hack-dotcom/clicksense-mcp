"""The HTTP transport as a remote client sees it: bearer tokens, refusal of unsafe binds."""
from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import urllib.error
import urllib.request

import pytest
from fastmcp import Client

from clicksense.config import Http
from clicksense.server import build_app, http_auth_or_exit

from .conftest import make_settings

TOKEN = "t" * 12 + "-test-token-0123456789"


def post(url: str, token: str | None = None) -> int:
    """Raw MCP request with the standard library: the status code a client would see."""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, body, headers), timeout=2) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server(tmp_path):
    settings = make_settings(tmp_path)
    settings.http = Http(tokens=[TOKEN])
    port = free_port()
    app = build_app(settings, auth=http_auth_or_exit(settings.http, "127.0.0.1"))
    task = asyncio.create_task(app.run_http_async(host="127.0.0.1", port=port, show_banner=False))
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(100):
        try:
            await asyncio.to_thread(post, url)
            break
        except OSError:
            await asyncio.sleep(0.05)
    yield url
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_token_is_required(server):
    assert await asyncio.to_thread(post, server) == 401
    assert await asyncio.to_thread(post, server, "wrong-token-wrong-token-wrong") == 401


async def test_valid_token_lists_tools(server):
    async with Client(server, auth=TOKEN) as c:
        assert len(await c.list_tools()) >= 20


@pytest.mark.parametrize("http,host,ok", [
    (Http(), "127.0.0.1", True),                                  # loopback, no token: allowed
    (Http(), "0.0.0.0", False),                                   # network, no token: refused
    (Http(allow_unauthenticated_remote=True), "0.0.0.0", True),   # explicit opt-out
    (Http(tokens="short"), "127.0.0.1", False),                   # weak token: refused
    (Http(tokens=f"{TOKEN}, {TOKEN}x"), "0.0.0.0", True),         # comma-separated list from env
])
def test_bind_policy(http, host, ok):
    if ok:
        http_auth_or_exit(http, host)
    else:
        with pytest.raises(SystemExit):
            http_auth_or_exit(http, host)


async def test_chat_bridge_reaches_a_remote_server_with_its_token(server, tmp_path):
    from clicksense.agent import connect

    settings = make_settings(tmp_path)
    settings.llm.mcp_url, settings.llm.mcp_token = server, TOKEN
    async with connect(settings) as client:
        assert any(t.name == "get_context" for t in await client.list_tools())
