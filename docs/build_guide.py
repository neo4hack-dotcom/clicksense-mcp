"""Builds docs/ClickSense-User-Guide.pdf.   .venv/bin/python docs/build_guide.py

The guide is generated from this file (reportlab) so it stays reviewable in git; edit the
content here and rebuild.
"""
from __future__ import annotations

from pathlib import Path

from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (CondPageBreak, KeepTogether, PageBreak, Paragraph, Preformatted, SimpleDocTemplate,
                                Spacer, Table, TableStyle)
from reportlab.platypus.flowables import PageBreakIfNotEmpty
from reportlab.platypus.tableofcontents import TableOfContents

OUT = Path(__file__).resolve().parent / "ClickSense-User-Guide.pdf"
VERSION = "1.0.0"

INK = colors.HexColor("#1f2937")
MUTED = colors.HexColor("#6b7280")
BRAND = colors.HexColor("#b45309")
BRAND_SOFT = colors.HexColor("#fef3c7")
CODE_BG = colors.HexColor("#f3f4f6")
RULE = colors.HexColor("#d1d5db")
OK_SOFT = colors.HexColor("#ecfdf5")
WARN_SOFT = colors.HexColor("#fff7ed")

ss = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=ss["BodyText"], fontName="Helvetica", fontSize=9.6, leading=13.6,
                      textColor=INK, spaceAfter=5)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8.4, leading=11, textColor=MUTED)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=8.4, leading=10.8, spaceAfter=0)
CELL_B = ParagraphStyle("cellb", parent=CELL, fontName="Helvetica-Bold")
H1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName="Helvetica-Bold", fontSize=18, leading=22,
                    textColor=INK, spaceBefore=4, spaceAfter=10)
H2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=12.5, leading=16,
                    textColor=BRAND, spaceBefore=10, spaceAfter=5)
H3 = ParagraphStyle("h3", parent=ss["Heading3"], fontName="Helvetica-Bold", fontSize=10.4, leading=13,
                    textColor=INK, spaceBefore=7, spaceAfter=3)
CODE = ParagraphStyle("code", fontName="Courier", fontSize=7.9, leading=10, textColor=INK, backColor=CODE_BG,
                      borderPadding=(5, 6, 5, 6), leftIndent=6, rightIndent=6, spaceBefore=3, spaceAfter=8)
BULLET = ParagraphStyle("bullet", parent=BODY, leftIndent=12, bulletIndent=2, spaceAfter=2.5)
TOC1 = ParagraphStyle("toc1", fontName="Helvetica-Bold", fontSize=10, leading=15, leftIndent=0, textColor=INK)
TOC2 = ParagraphStyle("toc2", fontName="Helvetica", fontSize=9, leading=12.5, leftIndent=14, textColor=MUTED)


class Guide(SimpleDocTemplate):
    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name in ("h1", "h2"):
            level = 0 if flowable.style.name == "h1" else 1
            text = flowable.getPlainText()
            key = f"h{id(flowable)}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=level, closed=level > 0)
            self.notify("TOCEntry", (level, text, self.page, key))


story: list = []


def h1(t):
    while story and isinstance(story[-1], Spacer):  # a trailing spacer can spill onto a blank page
        story.pop()
    story.append(PageBreakIfNotEmpty())
    story.append(Paragraph(t, H1))


def h2(t, need=40):
    story.append(CondPageBreak(need * mm))
    story.append(Paragraph(t, H2))


def h3(t):
    story.append(CondPageBreak(25 * mm))
    story.append(Paragraph(t, H3))


def p(t, style=BODY):
    story.append(Paragraph(t, style))


def bullets(items):
    for it in items:
        story.append(Paragraph(it, BULLET, bulletText="•"))
    story.append(Spacer(1, 3))


def code(text):
    pre = Preformatted(text.strip("\n"), ParagraphStyle("pre", parent=CODE, backColor=None, borderPadding=0,
                                                         leftIndent=0, rightIndent=0, spaceBefore=0, spaceAfter=0))
    t = Table([[pre]], colWidths=[170 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), CODE_BG), ("LEFTPADDING", (0, 0), (-1, -1), 7),
                           ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                           ("LINEBEFORE", (0, 0), (0, -1), 1.5, RULE)]))
    story.append(t)
    story.append(Spacer(1, 7))


def table(rows, widths, header=True, zebra=True):
    data = [[Paragraph(str(c), CELL_B if (header and i == 0) else CELL) for c in r] for i, r in enumerate(rows)]
    t = Table(data, colWidths=[w * mm for w in widths], repeatRows=1 if header else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
             ("TOPPADDING", (0, 0), (-1, -1), 3.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.2),
             ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), BRAND_SOFT), ("LINEBELOW", (0, 0), (-1, 0), 0.8, BRAND)]
    if zebra:
        for i in range(2 if header else 1, len(rows), 2):
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fafafa")))
    t.setStyle(TableStyle(style))
    story.append(t)
    story.append(Spacer(1, 7))


def callout(title, text, bg=BRAND_SOFT, edge=BRAND):
    t = Table([[Paragraph(f"<b>{title}</b><br/>{text}", CELL)]], colWidths=[170 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("LINEBEFORE", (0, 0), (0, -1), 2.5, edge),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 6),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story.append(KeepTogether([t, Spacer(1, 8)]))


def tool(name, purpose, args, returns, example=None, notes=None):
    block = [Paragraph(f"<font face='Courier-Bold' color='#b45309'>{name}</font>", H3), Paragraph(purpose, BODY)]
    if args:
        rows = [["Argument", "Type / default", "Meaning"]] + args
        data = [[Paragraph(str(c), CELL_B if i == 0 else CELL) for c in r] for i, r in enumerate(rows)]
        t = Table(data, colWidths=[32 * mm, 38 * mm, 100 * mm])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), CODE_BG), ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]))
        block += [t, Spacer(1, 4)]
    block.append(Paragraph(f"<b>Returns.</b> {returns}", BODY))
    if notes:
        block.append(Paragraph(f"<b>Notes.</b> {notes}", BODY))
    story.append(KeepTogether(block))
    if example:
        code(example)


def architecture() -> Drawing:
    d = Drawing(170 * mm, 92 * mm)
    W = 170 * mm

    def box(x, y, w, h, title, sub, fill):
        d.add(Rect(x, y, w, h, rx=5, ry=5, fillColor=fill, strokeColor=INK, strokeWidth=0.6))
        d.add(String(x + w / 2, y + h - 13, title, fontName="Helvetica-Bold", fontSize=8.6, fillColor=INK,
                     textAnchor="middle"))
        for i, line in enumerate(sub):
            d.add(String(x + w / 2, y + h - 25 - i * 9.5, line, fontName="Helvetica", fontSize=7, fillColor=MUTED,
                         textAnchor="middle"))

    def arrow(x1, y1, x2, y2, label=None):
        d.add(Line(x1, y1, x2, y2, strokeColor=INK, strokeWidth=0.8))
        import math
        a = math.atan2(y2 - y1, x2 - x1)
        s = 5
        d.add(Polygon([x2, y2, x2 - s * math.cos(a - 0.4), y2 - s * math.sin(a - 0.4),
                       x2 - s * math.cos(a + 0.4), y2 - s * math.sin(a + 0.4)], fillColor=INK, strokeColor=INK))
        if label:
            d.add(String((x1 + x2) / 2 + 6, (y1 + y2) / 2 - 2, label, fontName="Helvetica-Oblique", fontSize=6.6,
                         fillColor=MUTED))

    top = 92 * mm
    box(W / 2 - 40 * mm, top - 20 * mm, 80 * mm, 17 * mm, "AI agent / MCP client",
        ["Claude Code, Claude Desktop, any MCP client"], colors.white)
    box(8 * mm, top - 54 * mm, 72 * mm, 27 * mm, "MCP surface (server.py)",
        ["23 tools, resources, prompts, skills", "readOnlyHint + isError on refusals", "elicitation (both protocols)"],
        BRAND_SOFT)
    box(90 * mm, top - 54 * mm, 72 * mm, 27 * mm, "Semantic layer (semantic/)",
        ["dictionary, glossary, metrics", "knowledge bank, examples, skills", "BM25 search, hot reload"], OK_SOFT)
    box(8 * mm, top - 84 * mm, 47 * mm, 23 * mm, "Guard + advisor",
        ["one SELECT, no system/ext.", "row cap, PII, lint"], WARN_SOFT)
    box(61 * mm, top - 84 * mm, 47 * mm, 23 * mm, "Executor",
        ["breaker, rate, slots, cache", "pre-flight, per-query limits"], WARN_SOFT)
    box(114 * mm, top - 84 * mm, 48 * mm, 23 * mm, "ClickHouse (HTTP)",
        ["read-only account", "profile CONSTRAINTS + quota"], colors.HexColor("#eff6ff"))
    arrow(W / 2 - 10 * mm, top - 20 * mm, 44 * mm, top - 27 * mm)
    d.add(String(W / 2 - 4 * mm, top - 25 * mm, "tool calls", fontName="Helvetica-Oblique", fontSize=6.6,
                 fillColor=MUTED))
    arrow(80 * mm, top - 40 * mm, 90 * mm, top - 40 * mm)
    arrow(31 * mm, top - 54 * mm, 31 * mm, top - 61 * mm)
    arrow(55 * mm, top - 72 * mm, 61 * mm, top - 72 * mm)
    arrow(108 * mm, top - 72 * mm, 114 * mm, top - 72 * mm)
    arrow(126 * mm, top - 61 * mm, 126 * mm, top - 54 * mm, "live schema")
    return d


def on_page(canv, doc):
    canv.saveState()
    if doc.page > 1:
        canv.setStrokeColor(RULE)
        canv.setLineWidth(0.4)
        canv.line(20 * mm, 14 * mm, 190 * mm, 14 * mm)
        canv.setFont("Helvetica", 7.6)
        canv.setFillColor(MUTED)
        canv.drawString(20 * mm, 9.5 * mm, f"ClickSense {VERSION} — User Guide")
        canv.drawRightString(190 * mm, 9.5 * mm, f"Page {doc.page}")
    canv.restoreState()


def on_first(canv, doc):
    canv.saveState()
    canv.setFillColor(BRAND)
    canv.rect(0, A4[1] - 95 * mm, A4[0], 95 * mm, fill=1, stroke=0)
    canv.setFillColor(colors.white)
    canv.setFont("Helvetica-Bold", 34)
    canv.drawString(20 * mm, A4[1] - 50 * mm, "ClickSense")
    canv.setFont("Helvetica", 15)
    canv.drawString(20 * mm, A4[1] - 62 * mm, "A ClickHouse MCP server with a semantic layer")
    canv.setFont("Helvetica", 11)
    canv.drawString(20 * mm, A4[1] - 74 * mm, f"User Guide — version {VERSION}")
    canv.setFillColor(MUTED)
    canv.setFont("Helvetica", 8.5)
    canv.drawString(20 * mm, 18 * mm, "Generated from docs/build_guide.py — rebuild with: .venv/bin/python docs/build_guide.py")
    canv.restoreState()


# ============================================================================== cover + TOC
story.append(Spacer(1, 92 * mm))
p("<b>What this guide covers.</b> Installing and running ClickSense, connecting it to an MCP client, the "
  "recommended agent workflow, the reference of every tool, resource, prompt and skill, how to author the "
  "semantic layer for your own warehouse, the safety model that keeps ClickHouse up, performance tuning, "
  "operations and troubleshooting.")
p("<b>Who it is for.</b> Data and platform engineers who deploy the server, analytics engineers who curate "
  "the semantic layer, and anyone who builds agents on top of it.")
story.append(Spacer(1, 6))
table([["Audience", "Start with"],
       ["Trying it out", "Chapter 2 (Quick start), then Chapter 4 (Workflow)"],
       ["Deploying to production", "Chapters 3 (Configuration) and 9 (Safety model)"],
       ["Curating business knowledge", "Chapters 7 (Authoring) and 8 (Disambiguation)"],
       ["Building agents on top", "Chapters 4 (Workflow), 5 (Tools) and 6 (Resources)"]], [55, 115])
story.append(PageBreak())
story.append(Paragraph("Contents", ParagraphStyle("toctitle", parent=H1)))
toc = TableOfContents()
toc.levelStyles = [TOC1, TOC2]
story.append(toc)

# ============================================================================== 1 overview
h1("1. Overview")
p("ClickSense is an MCP server (Python, FastMCP) that lets an AI agent answer business questions on a "
  "ClickHouse warehouse. It addresses the three ways such agents usually fail:")
bullets([
    "<b>They answer the wrong question.</b> Ambiguous words (\"sales\": revenue, orders or units?), values in the "
    "user's wording (\"France\" when the column stores <font face='Courier'>'FR'</font>) and re-invented metric "
    "formulas give plausible but wrong numbers. ClickSense ships a semantic layer and disambiguation tools that "
    "surface these issues before a query runs.",
    "<b>They write slow or broken SQL.</b> Unknown columns, missing partition filters, joins in the wrong order. "
    "ClickSense hands the agent the sorting keys, partitions and verified examples up front, lints SQL before "
    "execution and refuses queries whose index estimate exceeds a budget.",
    "<b>They can hurt the database.</b> A runaway loop of heavy queries can saturate a production cluster. "
    "ClickSense combines a constrained read-only account, a static guard, admission control, a circuit breaker "
    "and per-query limits.",
])
h2("1.1 Architecture")
story.append(architecture())
story.append(Spacer(1, 4))
p("Every tool call goes through the MCP surface into <font face='Courier'>service.py</font>, which uses the "
  "semantic layer (files on disk) and the live catalog (ClickHouse system tables). Anything that reaches "
  "ClickHouse passes the guard and then the executor, which is the only code path that talks to the database.",
  SMALL)
h2("1.2 Key capabilities")
table([["Capability", "What it gives you"],
       ["One-call context", "<font face='Courier'>get_context</font> returns tables, columns, keys, metrics, glossary "
        "entries, values spotted in the question, ambiguities, verified examples, rules and suggested skills."],
       ["Governed metrics", "<font face='Courier'>query_metric</font> compiles official formulas with resolved filter "
        "values, explicit periods and declared joins: no SQL written by the model."],
       ["Value resolution", "User wording mapped to stored values (exact, case/accent/punctuation, synonyms, labels, "
        "fuzzy, live search) with an explicit status."],
       ["Pre-execution checks", "Unknown columns with suggestions, literals absent from the data, performance "
        "findings, table caveats, index-based row estimate."],
       ["Human in the loop", "<font face='Courier'>ask_user_to_clarify</font> uses MCP elicitation (both the legacy "
        "and the 2026-07-28 input-required flow) and degrades to a chat question."],
       ["Protection", "Read-only account with constraints, guard, pre-flight, rate limit, concurrency slots, "
        "circuit breaker, per-query limits, query ids and audit log."],
       ["Speed", "Connection pool + lz4, result cache, ClickHouse query cache, cached schema and value sets; "
        "typical calls take 2-40 ms."]], [40, 130])

# ============================================================================== 2 quick start
h1("2. Installation and quick start")
h2("2.1 Requirements")
bullets(["Python 3.11+ and <font face='Courier'>uv</font> (or pip).",
         "A ClickHouse server reachable over HTTP (8123 / 8443). For the demo: the ClickHouse binary "
         "(<font face='Courier'>brew install --cask clickhouse</font>) or Docker.",
         "An MCP client: Claude Code, Claude Desktop or any client supporting stdio or streamable HTTP."])
h2("2.2 Install the server")
code("""
cd clicksense
uv venv .venv
uv pip install --python .venv/bin/python -e ".[dev]"
""")
h2("2.3 Start the demo database")
p("The repository contains a deterministic e-commerce dataset (customers, products, 3M order lines, 20M web "
  "events) and a read-only account configured like a production one.")
code("""
scripts/dev-clickhouse.sh start     # ClickHouse on 127.0.0.1:18123 (HTTP) / 19000 (native)
scripts/dev-clickhouse.sh seed      # loads dev/seed.sql (~6 s)
# other commands: stop | status | reset
""")
p("With Docker instead:")
code("""
docker compose up -d
docker compose exec -T clickhouse clickhouse-client --multiquery < dev/seed.sql
""")
h2("2.4 Configure and check")
code("""
cp clicksense.example.yaml clicksense.yaml
cp .env.example .env                  # demo connection: port 18123, user clicksense, database shop
.venv/bin/clicksense --check          # connection, readonly level, limits, semantic layer counts
.venv/bin/python -m pytest -q         # 100 tests; live tests are skipped if ClickHouse is down
""")
callout("Tip", "<font face='Courier'>--check</font> prints <font face='Courier'>\"readonly\": 2</font> for a "
        "correctly configured account. A value of 1 means per-query limits cannot be sent (see Chapter 9).")
h2("2.5 Connect an MCP client")
h3("Claude Code / Claude Desktop (stdio)")
code("""
{
  "mcpServers": {
    "clicksense": {
      "command": "/path/to/clicksense/.venv/bin/clicksense",
      "args": ["--config", "/path/to/clicksense/clicksense.yaml"]
    }
  }
}
""")
h3("Streamable HTTP")
code("""
.venv/bin/clicksense --config clicksense.yaml --transport http --host 127.0.0.1 --port 3110
# MCP endpoint: http://127.0.0.1:3110/mcp
""")
callout("Security", "Beyond 127.0.0.1 the server requires a bearer token (section 2.8): set CLICKSENSE_HTTP_TOKENS "
        "and clients send Authorization: Bearer &lt;token&gt;.", WARN_SOFT, colors.HexColor("#c2410c"))
h2("2.6 Command-line reference")
table([["Option", "Default", "Meaning"],
       ["--config PATH", "$CLICKSENSE_CONFIG or ./clicksense.yaml", "Configuration file."],
       ["--transport", "stdio", "stdio or http."],
       ["--host / --port", "127.0.0.1 / 3110", "HTTP bind address."],
       ["--check", "off", "Print the status report and exit."],
       ["--gen-token", "", "Print a new random bearer token and exit."]], [35, 60, 75])
p("clicksense-chat takes --config, -q/--question, --model, --base-url, --mcp-url and --quiet (section 2.7).", SMALL)

h2("2.7 Use an OpenAI-compatible LLM with your own API key")
p("The LLM does not need to support MCP. <font face='Courier'>clicksense-chat</font> is the MCP client in between: "
  "it lists the ClickSense tools, gives them to any OpenAI-compatible chat-completions API as function tools, "
  "runs the calls the model makes and loops until the model answers. Works with OpenAI, Azure OpenAI, Mistral, "
  "Groq, DeepSeek, OpenRouter, vLLM, Ollama, LM Studio and others.")
code("""
uv pip install --python .venv/bin/python -e ".[llm]"
cp .env.example .env              # put your key in it: OPENAI_API_KEY=sk-...
.venv/bin/clicksense-chat         # interactive: /reset, /tools, /exit
.venv/bin/clicksense-chat -q "Revenue by channel in France last month"
.venv/bin/clicksense-chat --base-url https://api.mistral.ai/v1 --model mistral-large-latest
.venv/bin/clicksense-chat --mcp-url http://127.0.0.1:3110/mcp   # use a running HTTP server
""")
table([["Setting", "Where", "Meaning"],
       ["API key", "OPENAI_API_KEY or CLICKSENSE_LLM_API_KEY (env or .env), or llm.api_key",
        "Not needed for local servers (localhost)."],
       ["Endpoint", "--base-url, llm.base_url or OPENAI_BASE_URL", "Default https://api.openai.com/v1."],
       ["Model", "--model, CLICKSENSE_LLM_MODEL or llm.model", "Default gpt-4.1-mini. Must support tool calling."]],
      [25, 75, 70])
bullets(["<b>auto_context</b> (default on): before each question the bridge calls <font face='Courier'>get_context</font> "
         "itself and puts the result in the conversation. Models other than Claude often skip \"call this first\" "
         "instructions; without it, an ambiguous word such as \"sales\" can be resolved silently. Tested: with it, the "
         "model asks which meaning of \"sales\" is wanted before querying.",
         "<b>Clarification</b>: when the model calls <font face='Courier'>ask_user_to_clarify</font>, the question and "
         "its numbered options are shown in the terminal.",
         "<b>Bounded</b>: at most llm.max_steps tool rounds per question; tool results above llm.max_tool_chars are "
         "truncated with a note asking the model to aggregate.",
         "<b>History</b>: follow-up questions keep the conversation; /reset starts over."])
p("In your own Python code:")
code("""
from clicksense.agent import Agent, connect
from clicksense.config import load_settings

settings = load_settings("clicksense.yaml")
# ask_user(question, options) -> str | None is called for ask_user_to_clarify
async with connect(settings, ask_user=my_async_ask) as client:
    agent = Agent(settings.llm, client, on_event=lambda kind, data: print(kind, data))
    await agent.start()
    answer = await agent.ask("What was revenue in France last month?")
""")
callout("Data leaves your network", "Tool results (schema, sampled values, query results) are sent to the LLM "
        "provider you configure. Use a provider and contract that fit the data's sensitivity, or a local server "
        "(vLLM, Ollama with a local model). PII columns are never returned raw by ClickSense under policy pii: block.",
        WARN_SOFT, colors.HexColor("#c2410c"))

h2("2.8 Install on a Windows server")
p("Same model as DOINg.AI: a local .venv, <font face='Courier'>pip install -r requirements.txt</font>, settings in "
  "<font face='Courier'>.env</font> and batch scripts. ClickHouse stays where it is; the Windows server only needs "
  "network access to its HTTP port. Prerequisite: Python 3.11+ from python.org with <i>Add python.exe to PATH</i> and, "
  "on a server, <i>Install for all users</i> so the service account can run it.")
code("""
git clone https://github.com/neo4hack-dotcom/clicksense-mcp.git C:\\clicksense
cd C:\\clicksense
install-windows.bat
""")
table([["Script", "What it does"],
       ["install-windows.bat", "Checks Python, enables long paths (as Administrator), creates .venv, installs "
        "requirements.txt and the package, creates clicksense.yaml and .env, generates a bearer token in .env, runs "
        "the connection check. Safe to re-run: existing configuration is never overwritten."],
       ["check-windows.bat", "Connection, readonly level, limits, tables, semantic layer. Changes nothing."],
       ["serve-windows.bat", "The MCP server over HTTP in the foreground. Extra arguments pass through "
        "(e.g. --host 0.0.0.0)."],
       ["chat-windows.bat", "clicksense-chat with the LLM settings from .env."],
       ["scripts\\windows\\service.ps1", "install | status | start | stop | restart | uninstall a scheduled task that "
        "runs serve-windows.bat at boot under SYSTEM (or -User with a domain account), restarts it on failure and "
        "appends to logs\\clicksense-http.log. -OpenFirewall adds an inbound rule (Domain, Private)."]], [42, 128])
code("""
:: elevated PowerShell
powershell -ExecutionPolicy Bypass -File scripts\\windows\\service.ps1 install -OpenFirewall
powershell -ExecutionPolicy Bypass -File scripts\\windows\\service.ps1 status
""")
table([["Variable (.env)", "Meaning"],
       ["CLICKSENSE_HOST / _PORT / _USER / _PASSWORD / _DATABASE / _SECURE", "ClickHouse connection (read-only account)."],
       ["CLICKSENSE_HTTP_HOST / _PORT", "Bind address (127.0.0.1 = this machine only; 0.0.0.0 = network) and port."],
       ["CLICKSENSE_HTTP_TOKENS", "Accepted bearer tokens, comma-separated, 24+ characters (clicksense --gen-token)."],
       ["OPENAI_API_KEY / OPENAI_BASE_URL / CLICKSENSE_LLM_MODEL", "LLM for clicksense-chat."],
       ["CLICKSENSE_MCP_TOKEN", "Token clicksense-chat sends when --mcp-url points at a remote server."]], [70, 100])
callout("Security", "The server refuses to listen beyond 127.0.0.1 without a token, and rejects tokens shorter than "
        "24 characters. Tokens are compared in constant time; a request without a valid "
        "<font face='Courier'>Authorization: Bearer</font> header gets 401 before reaching any tool. Traffic is plain "
        "HTTP: for TLS, put the server behind a reverse proxy (IIS/ARR, nginx) or keep it on a trusted network.",
        WARN_SOFT, colors.HexColor("#c2410c"))
p("Windows-specific details handled for you: CRLF line endings and ASCII-only scripts (cmd.exe and PowerShell 5.1), "
  "UTF-8 with or without the BOM Notepad adds (config, .env, semantic files), <font face='Courier'>PYTHONUTF8=1</font> "
  "for console output, <font face='Courier'>tzdata</font> for time zones, no <font face='Courier'>--reload</font>. "
  "tests/test_windows_compat.py checks these rules from any OS.")
p("A remote MCP client connects with the URL and the header:")
code("""
{"mcpServers": {"clicksense": {"url": "http://winserver:3110/mcp",
                               "headers": {"Authorization": "Bearer <token>"}}}}
""")

# ============================================================================== 3 configuration
h1("3. Configuration")
callout("Generated from the code", "Every setting below is declared, with its description and its environment "
        "variable, in src/clicksense/config.py. <font face='Courier'>clicksense-admin config --example</font> prints "
        "the fully commented file (it is clicksense.example.yaml) and <font face='Courier'>config --show</font> the "
        "effective values with secrets masked. A test fails if the example file and the code drift apart.",
        OK_SOFT, colors.HexColor("#047857"))
p("Configuration is one YAML file. <font face='Courier'>${VAR}</font> and <font face='Courier'>${VAR:-default}</font> "
  "are substituted from the environment; a variable without a default that is not set stops the server at start "
  "(no silent empty password). Relative paths are resolved against the directory of the configuration file.")
code("""
connection:
  host: ${CLICKSENSE_HOST:-localhost}
  port: ${CLICKSENSE_PORT:-18123}
  username: ${CLICKSENSE_USER:-clicksense}
  password: ${CLICKSENSE_PASSWORD:-clicksense}
  database: shop
limits:
  default_rows: 200
  max_execution_time: 20
  preflight_max_rows: 300000000
policy:
  allowed_databases: [shop]
  pii: block
semantic_dir: semantic
audit_path: audit/clicksense.jsonl
""")
h2("3.1 connection")
table([["Key", "Default", "Meaning"],
       ["host / port", "localhost / 8123", "ClickHouse HTTP interface."],
       ["username / password", "default / \"\"", "Use the dedicated read-only account."],
       ["secure", "false", "HTTPS (port 8443 usually)."],
       ["database", "default", "Database for unqualified table names. Must not be a system database."],
       ["connect_timeout / send_receive_timeout", "5 / 90 s", "Socket timeouts."],
       ["pool_size", "8", "HTTP connection pool size."],
       ["compress", "lz4", "Transport compression."]], [52, 33, 85])
p("Environment overrides: CLICKSENSE_HOST, CLICKSENSE_PORT, CLICKSENSE_USER, CLICKSENSE_PASSWORD, "
  "CLICKSENSE_DATABASE, CLICKSENSE_SECURE.", SMALL)
h2("3.2 limits")
table([["Key", "Default", "Meaning"],
       ["default_rows / hard_max_rows", "200 / 5000", "Rows returned by default / maximum a caller may request."],
       ["max_response_bytes", "200000", "Result payload budget; beyond it the result is truncated and says so."],
       ["max_sql_chars", "20000", "Longest statement accepted."],
       ["max_execution_time", "20 s", "Per-query time limit sent to ClickHouse."],
       ["max_memory_usage", "1 GB", "Per-query memory limit."],
       ["max_threads", "4", "Per-query threads."],
       ["max_rows_to_read / max_bytes_to_read", "1e9 / 1e11", "Read limits enforced by ClickHouse."],
       ["max_bytes_before_external_group_by / _sort", "500 MB", "Spill to disk instead of failing on memory."],
       ["preflight / preflight_max_rows", "true / 3e8", "EXPLAIN ESTIMATE before running; refuse above the budget."],
       ["max_concurrent / queue_timeout_s", "4 / 15 s", "Client-side concurrency slots and queue wait."],
       ["rate_per_minute", "240", "Token bucket for queries per minute."],
       ["breaker_failures / breaker_cooldown_s", "4 / 30 s", "Circuit breaker on consecutive load failures."],
       ["result_cache_ttl_s / result_cache_entries", "120 s / 256", "In-process result cache."],
       ["use_query_cache / query_cache_ttl_s", "true / 300 s", "ClickHouse query cache."]], [62, 28, 80])
p("Per-query limits can only <i>tighten</i> what the account profile allows: ClickHouse rejects any setting "
  "above a profile CONSTRAINT.", SMALL)
h2("3.3 policy")
table([["Key", "Default", "Meaning"],
       ["allowed_databases", "[] (all non-system)", "Databases the guard and catalog accept."],
       ["denied_tables", "[]", "\"db.table\" entries hidden and refused."],
       ["allowed_table_functions", "[numbers]", "Table functions permitted in FROM."],
       ["pii", "block", "block: PII columns may be filtered or aggregated but never returned raw. allow: no restriction."],
       ["server_parse_fallback", "true", "If sqlglot cannot parse valid ClickHouse SQL, ask ClickHouse (EXPLAIN AST)."]],
      [45, 38, 87])
h2("3.4 http")
table([["Key", "Default", "Meaning"],
       ["host / port", "127.0.0.1 / 3110", "Also CLICKSENSE_HTTP_HOST / CLICKSENSE_HTTP_PORT."],
       ["tokens", "[]", "Bearer tokens; also CLICKSENSE_HTTP_TOKENS (comma-separated). Required beyond loopback."],
       ["allow_unauthenticated_remote", "false", "Explicit opt-out of the token requirement (not recommended)."]],
      [50, 35, 85])
h2("3.5 Other keys")
table([["Key", "Default", "Meaning"],
       ["semantic_dir", "semantic", "Location of the semantic layer."],
       ["audit_path", "audit/clicksense.jsonl", "JSONL audit log; null disables it."],
       ["catalog_ttl_s", "600", "Live schema cache."],
       ["value_cache_ttl_s", "3600", "Cache of column value enumerations."],
       ["value_cache_max_distinct", "2000", "Above this, a column is high-cardinality and searched live."],
       ["value_scan_max_rows", "5e7", "Larger tables are sampled or time-windowed when enumerating values."]],
      [45, 45, 80])

h2("3.6 llm (clicksense-chat)")
table([["Key", "Default", "Meaning"],
       ["base_url", "$OPENAI_BASE_URL or OpenAI", "OpenAI-compatible endpoint."],
       ["api_key", "env", "Prefer OPENAI_API_KEY / CLICKSENSE_LLM_API_KEY in the environment or .env."],
       ["model", "gpt-4.1-mini", "Any model with tool calling."],
       ["temperature", "provider default", "Some reasoning models reject it; leave unset for those."],
       ["auto_context", "true", "Call get_context before the model's first turn."],
       ["max_steps / max_tool_chars", "16 / 12000", "Tool rounds per question / tool result size sent to the model."],
       ["timeout_s, extra_headers", "120 / {}", "HTTP timeout; extra headers (e.g. for gateways)."],
       ["mcp_url", "none (in-process)", "Talk to a running ClickSense HTTP server instead."]], [45, 40, 85])

# ============================================================================== 4 workflow
h1("4. The recommended agent workflow")
p("The server instructions (sent to every client) and the <font face='Courier'>answer-data-question</font> skill "
  "describe the same loop. Following it is what makes answers reliable.")
table([["Step", "Tool(s)", "Why"],
       ["1. Context", "get_context", "One call returns everything relevant, including ambiguities and examples."],
       ["2. Ambiguity", "ask_user_to_clarify", "Never pick a meaning silently. Use the context first; ask when it "
        "does not settle the question."],
       ["3. Period", "check_freshness", "State the period used. Check freshness before \"today\" / \"yesterday\"."],
       ["4a. Metric", "query_metric", "When a governed metric fits: official formula, resolved values."],
       ["4b. SQL", "describe_table, resolve_values, validate_query, run_query", "Exact names, real literals, "
        "checked before running."],
       ["5. Check", "(result fields)", "truncated = incomplete; 0 rows = read notes first; sanity-check magnitudes."],
       ["6. Answer", "suggest_knowledge", "Give numbers with the definition, filters, period and caveats; propose "
        "reusable learnings."]], [22, 52, 96])
h2("4.1 Worked example")
p("Question: <i>\"What were sales in France on the website last month?\"</i>")
p("<b>get_context</b> ranks <font face='Courier'>shop.orders</font> first, proposes the metrics revenue, orders "
  "and units_sold, spots <font face='Courier'>website → channel = 'web'</font> and "
  "<font face='Courier'>France → country = 'FR'</font>, and reports an ambiguity: <b>sales</b> has three senses. "
  "The agent asks; the user picks revenue. Then:")
code("""
query_metric {"metrics": ["revenue"], "dimensions": ["channel"],
              "filters": {"country": "France"}, "period": "last_month"}
""")
p("Real response (abridged):")
code("""
{ "ok": true, "columns": ["channel", "revenue"], "row_count": 4,
  "rows": [["web", 13949044.75], ["app", 9209137.36], ["store", 4835205.24],
           ["marketplace", 4666537.69]],
  "truncated": false,
  "metric_definitions": [{"name": "revenue", "expression": "sum(o.amount_eur)", "unit": "EUR"}],
  "period": {"from": "2026-09-01", "to_exclusive": "2026-10-01", "column": "order_date"},
  "value_resolutions": [{"dimension": "country", "from": "France", "to": "FR", "match": "label"}],
  "sql": "SELECT o.`channel` AS `channel`, sum(o.amount_eur) AS `revenue`
          FROM `shop`.`orders` AS o
          WHERE (o.status = 'completed')
            AND o.`order_date` >= {p_from:Date} AND o.`order_date` < {p_to:Date}
            AND o.`country` IN {f0:Array(String)}
          GROUP BY `channel` ORDER BY `revenue` DESC LIMIT 100",
  "params": {"p_from": "2026-09-01", "p_to": "2026-10-01", "f0": ["FR"]} }
""")
p("Had the agent written <font face='Courier'>WHERE country = 'France'</font> by hand, "
  "<font face='Courier'>run_query</font> would have returned zero rows with this note:")
code("""
{"severity": "error", "code": "unknown_value",
 "message": "'France' does not occur in shop.orders.country.", "fix": "Use 'FR'.",
 "candidates": [{"value": "FR", "score": 0.96, "match": "label", "label": "France", "rows": 1064411}]}
""")

# ============================================================================== 5 tools
h1("5. Tool reference")
p("All tools return JSON objects with <font face='Courier'>\"ok\": true</font> on success. A refusal or failure "
  "returns <font face='Courier'>isError: true</font> with <font face='Courier'>{ok: false, error, message, hint, "
  "...}</font>: <font face='Courier'>error</font> is a stable code (see section 11.2), <font face='Courier'>hint</font> "
  "says what to do instead. Every tool declares <font face='Courier'>readOnlyHint</font> (true except "
  "<font face='Courier'>suggest_knowledge</font> and <font face='Courier'>cancel_query</font>) and "
  "<font face='Courier'>openWorldHint: false</font>.")
h2("5.1 Discovery", need=90)
tool("get_context", "Start here. Everything relevant to a question in one call.",
     [["question", "string", "The user's question, verbatim."], ["limit", "int = 6 (1-20)", "Caps tables (max 3), "
      "metrics and examples."]],
     "<font face='Courier'>tables</font> (summary, time column, sorting key, brief columns, joins, caveats), "
     "<font face='Courier'>metrics</font>, <font face='Courier'>glossary</font>, <font face='Courier'>values_spotted</font>, "
     "<font face='Courier'>ambiguities</font> (business terms with several senses, values present in several columns), "
     "<font face='Courier'>examples</font> (verified SQL), <font face='Courier'>knowledge</font> excerpts, "
     "<font face='Courier'>skills</font>, <font face='Courier'>next_steps</font>.",
     notes="Tables are ranked from lexical matches on the live schema and dictionary, boosted by matching metrics "
           "and glossary terms and by values found in the question. Two-letter words count as values only when "
           "written in capitals (\"CA\", not \"de\").")
tool("list_tables", "Tables available, with description, row count and grain.",
     [["search", "string?", "Keywords to filter."]],
     "<font face='Courier'>tables</font>; <font face='Courier'>warning</font> when the live catalog is unavailable; "
     "<font face='Courier'>dictionary_only</font> lists tables documented but absent from ClickHouse.")
tool("describe_table", "Full description of one table.",
     [["table", "string", "database.table or a table of the default database."],
      ["include_samples", "bool = false", "Adds 5 sample rows (PII hidden)."]],
     "Engine, size, sorting/partition/sampling keys, time column, every column (type, description, keys, unit, PII, "
     "values, value synonyms, caveats), skip indices, joins, dimensions, related knowledge and metrics, "
     "<font face='Courier'>performance_hints</font>.",
     notes="Unknown names return candidates (\"Did you mean ...\").")
tool("search_knowledge", "BM25 search over the whole semantic layer.",
     [["query", "string", "Free text."], ["kinds", "list?", "table, column, metric, term, knowledge, skill, example."],
      ["limit", "int = 8", "Maximum results."]],
     "<font face='Courier'>results</font> with kind, id, title, score and a kind-specific payload (excerpt, "
     "definition and senses, expression, SQL...).")
tool("get_knowledge", "Full text of one knowledge document.", [["doc_id", "string", "From get_context or search."]],
     "id, title, tags, applies_to, body.")
tool("list_skills / get_skill", "List the playbooks; return one playbook's step-by-step instructions.",
     [["name", "string", "get_skill only."]], "name, description, when_to_use, body.")
tool("list_metrics", "Governed metrics.", [], "name, label, description, table, unit, dimensions, synonyms.")

h2("5.2 Querying", need=150)
tool("query_metric", "Compute governed metrics without writing SQL.",
     [["metrics", "list[string]", "Names or synonyms, e.g. [\"revenue\", \"orders\"]. Same table and built-in filters."],
      ["dimensions", "list?", "Group-by dimensions: table columns or declared dimensions (joins added automatically)."],
      ["filters", "object?", "dimension → value | [values] | {op: value}, op in = != &gt; &gt;= &lt; &lt;= in not_in. "
       "User wording is resolved to stored values."],
      ["period", "string | object?", "today, yesterday, this_week, last_week, this_month/mtd, last_month, "
       "this_quarter/qtd, last_quarter, this_year/ytd, last_year, last_N_days|weeks|months, or {from, to} (inclusive)."],
      ["grain", "enum?", "hour, day, week (Monday), month, quarter, year."],
      ["order_by / descending", "string? / true", "A metric, a dimension or 'period'."],
      ["limit", "int = 100", "Rows."], ["format", "json | markdown | csv", "Output format."]],
     "Same fields as run_query plus <font face='Courier'>metric_definitions</font>, <font face='Courier'>period</font>, "
     "<font face='Courier'>value_resolutions</font>, <font face='Courier'>sql</font> and <font face='Courier'>params</font>.",
     notes="An ambiguous or unknown filter value stops compilation with <font face='Courier'>needs_clarification</font> "
           "and the candidates: the compiler never guesses. Values are always bound as typed parameters.")
tool("run_query", "Run one read-only SELECT through the guard, the pre-flight and the executor.",
     [["sql", "string", "SELECT, WITH ... SELECT or UNION. One statement."],
      ["params", "object?", "Values for {name:Type} placeholders."],
      ["max_rows", "int? (1-5000)", "Default 200."], ["format", "json | markdown | csv", "Default json."]],
     "<font face='Courier'>columns</font>, <font face='Courier'>types</font>, <font face='Courier'>rows</font> (or table / "
     "csv), <font face='Courier'>row_count</font>, <font face='Courier'>truncated</font> (+ reason and hint), "
     "<font face='Courier'>row_cap</font>, <font face='Courier'>stats</font> (query_id, elapsed_ms, read_rows, "
     "read_bytes, cached), <font face='Courier'>notes</font> (errors/warnings from the advisor), "
     "<font face='Courier'>empty_hint</font>.",
     example="""
run_query {"sql": "SELECT device, uniq(session_id) AS sessions FROM shop.web_events
                   WHERE event_date >= {d:Date} GROUP BY device ORDER BY sessions DESC",
           "params": {"d": "2026-10-01"}}
""")
tool("sample_rows", "A few raw rows to see the data.",
     [["table", "string", ""], ["n", "int = 10 (1-100)", ""], ["columns", "list?", ""],
      ["where", "string?", "SQL condition without WHERE (guarded)."]],
     "As run_query, plus <font face='Courier'>hidden_pii_columns</font>.")

h2("5.3 Control", need=90)
tool("validate_query", "Check a query without running it.",
     [["sql", "string", ""], ["params", "object?", ""], ["check_values", "bool = true", "Look up filter literals."]],
     "<font face='Courier'>valid</font>, <font face='Courier'>verdict</font> (ok | review_warnings | fix_before_running | "
     "rejected), <font face='Courier'>findings</font>, <font face='Courier'>estimate</font>, "
     "<font face='Courier'>estimated_rows</font>, <font face='Courier'>sql_to_run</font>, <font face='Courier'>parser</font>.")
p("Findings raised by the advisor:", SMALL)
table([["Code", "Severity", "Meaning"],
       ["unknown_column", "error", "Column absent; suggestions include case-only differences and synonyms."],
       ["ambiguous_column", "warning", "Unqualified column present in several joined tables."],
       ["unknown_value", "error / warning", "Filter literal not stored in the column; candidates given."],
       ["no_partition_filter", "warning", "Large partitioned table without a filter on its time column."],
       ["primary_key_unused", "info", "No filter on the first ORDER BY key."],
       ["select_star", "warning", "SELECT * on a column store."],
       ["join_order", "warning", "Right side of a JOIN larger than the left (hash table built from the right)."],
       ["final / count_distinct / leading_wildcard", "info", "Costly constructs, with the cheaper alternative."],
       ["too_expensive", "error", "Estimated rows to read above the pre-flight budget."],
       ["caveat", "info", "Caveats documented for the tables used."]], [52, 25, 93])
tool("explain_query", "Execution plan with index usage, the estimate, and optimisation findings.",
     [["sql", "string", ""], ["params", "object?", ""]],
     "<font face='Courier'>plan</font> (EXPLAIN indexes = 1, truncated at 8000 chars), <font face='Courier'>estimate</font>, "
     "<font face='Courier'>estimated_rows</font>, <font face='Courier'>findings</font>.")
tool("profile_column", "Bounded statistics for one column.", [["table", "string", ""], ["column", "string", ""]],
     "rows, nulls, distinct_approx, min/max, avg and p50/p90/p99 (numeric), empty and avg_length (text), top_values "
     "(not for PII); <font face='Courier'>scope</font> says full, sample or last_90_days.")
tool("check_freshness", "Latest timestamp and lag.", [["table", "string", ""]],
     "latest, server_now, lag_hours, expected_refresh (from the dictionary).")

h2("5.4 Disambiguation", need=100)
tool("resolve_values", "Map user wording to stored values of one column.",
     [["table", "string", ""], ["column", "string", ""], ["terms", "list[string]", "As the user said them."]],
     "Per term: <font face='Courier'>status</font> (exact | resolved | ambiguous | not_found), "
     "<font face='Courier'>value</font> to use when resolved, <font face='Courier'>candidates</font> with score, match type, "
     "label and row count, <font face='Courier'>coverage</font>; <font face='Courier'>all_resolved</font> overall.",
     example="""
{"term": "canceled", "status": "resolved", "value": "cancelled",
 "candidates": [{"value": "cancelled", "score": 0.96, "match": "synonym",
                 "label": "Cancelled before shipping", "rows": 150027}], "coverage": "full"}
""")
tool("locate_value", "Which table/column stores a value (low-cardinality columns).",
     [["term", "string", "e.g. 'Lyon'."], ["tables", "list?", "Restrict the search."]],
     "<font face='Courier'>matches</font> (table, column, value, score, rows); <font face='Courier'>ambiguous</font> when "
     "the value exists in several columns.")
tool("find_columns", "Which columns a business word may refer to.", [["term", "string", ""]],
     "Ranked <font face='Courier'>candidates</font>; <font face='Courier'>ambiguous</font> when several fit closely.")
tool("ask_user_to_clarify", "Ask the user to choose between interpretations.",
     [["question", "string", "Short question."], ["options", "list[string] (2-8)", "Candidate meanings or values."]],
     "<font face='Courier'>answered</font> + <font face='Courier'>choice</font>; <font face='Courier'>declined</font> / "
     "<font face='Courier'>cancel</font>; or <font face='Courier'>needs_clarification</font> when the client cannot elicit, "
     "in which case the agent asks in chat.",
     notes="Legacy clients get a server-initiated elicitation. On the 2026-07-28 protocol the tool returns an "
           "InputRequiredResult and reads the answer when the client calls back. Clients that do not advertise "
           "elicitation are never sent a request.")

h2("5.5 Operations and learning", need=80)
tool("server_status", "Health and protection state.", [],
     "ClickHouse reachability, version, readonly level; breaker state, failures, slots, running queries; per-query "
     "limits; cache statistics; totals (queries, errors, rows and bytes read); catalog and knowledge counts and load "
     "errors.")
tool("cancel_query", "Cancel a running query started by this server.", [["query_id", "string", "From stats or status."]],
     "<font face='Courier'>kill requested</font>. Ids not issued by this server are refused.")
tool("refresh_catalog", "Reload semantic files and the live schema; clear caches.", [],
     "Table count and load errors.")
tool("suggest_knowledge", "Propose an addition for human review.",
     [["kind", "example | term | caveat | metric | note", ""], ["title", "string", ""], ["content", "string", ""],
      ["sql", "string?", "For examples."]],
     "The inbox file written. Never used until a person moves it into the semantic layer.")

# ============================================================================== 6 resources
h1("6. Resources, prompts and skills")
h2("6.1 Resources")
table([["URI", "Content"],
       ["clicksense://guide", "Server instructions and SQL tips (markdown)."],
       ["clicksense://catalog", "All tables (JSON)."],
       ["clicksense://tables/{database}/{table}", "One table, as describe_table (JSON)."],
       ["clicksense://glossary", "The glossary (JSON)."],
       ["clicksense://metrics", "Metric definitions (JSON)."],
       ["clicksense://knowledge/{doc_id}", "One knowledge document (markdown)."],
       ["skill://{name}/SKILL.md", "Native MCP skills (FastMCP skills provider), plus a _manifest per skill."]],
      [65, 105])
h2("6.2 Prompts")
bullets(["<font face='Courier'>analyze(question)</font> — runs the recommended workflow on a question.",
         "<font face='Courier'>skill-&lt;name&gt;(question)</font> — one prompt per skill, with the playbook body."])
h2("6.3 Skills shipped")
table([["Skill", "Use it for"],
       ["answer-data-question", "Any question needing a number, a list or a trend."],
       ["investigate-metric-change", "Why a metric rose or dropped: confirm, rule out freshness, decompose by "
        "dimension, split volume vs value, drill down, report drivers."],
       ["data-quality-check", "Freshness, nulls, duplicates on the grain, referential integrity."],
       ["write-fast-clickhouse-sql", "Partition/primary-key filters, approximate functions, conditional aggregates, "
        "join order, SAMPLE."],
       ["disambiguate-request", "Turn a vague request into precise parameters before querying."]], [50, 120])
p("Skills are exposed three ways — as tools (list_skills / get_skill), as prompts and as native MCP skills — "
  "so every client can use them. They follow the Agent Skills format and can also be copied into "
  "<font face='Courier'>~/.claude/skills</font>.")

# ============================================================================== 7 authoring
h1("7. Authoring the semantic layer")
callout("Step-by-step recipes", "This chapter is the reference. The task-by-task recipes (document a table, add a "
        "metric, a term, an example, a rule, a skill, review the inbox, ship a change) are in the companion "
        "<b>ClickSense Maintenance Guide</b> (docs/UPDATING.md, also as PDF). The tool that does the tedious parts is "
        "<font face='Courier'>clicksense-admin kb</font>: bootstrap, new, validate --live, search, resolve, inbox.",
        OK_SOFT, colors.HexColor("#047857"))
p("Everything lives in <font face='Courier'>semantic/</font> as plain files: review them in pull requests like "
  "code. Files are re-read automatically when they change (checked at most every 2 seconds); "
  "<font face='Courier'>refresh_catalog</font> forces a reload. Load errors are reported by "
  "<font face='Courier'>server_status</font> instead of crashing the server.")
code("""
semantic/
  dictionary/*.yaml      tables, columns, value labels and synonyms, joins, dimensions, caveats
  glossary.yaml          business terms; several senses = ambiguous
  metrics.yaml           governed metrics
  examples.yaml          verified question -> SQL pairs
  knowledge/*.md         rules, conventions, calendars (YAML front-matter)
  skills/<name>/SKILL.md playbooks (Agent Skills format)
  inbox/                 suggestions from the agent, for review
""")
callout("Where to start", "Without any file the server already works from ClickHouse COMMENTs and the live schema. "
        "The highest-value additions, in order: <b>time_column</b> and <b>caveats</b> of the main tables, "
        "<b>values / value_synonyms</b> of frequently filtered columns, then <b>3-5 metrics</b> and a few verified "
        "examples.", OK_SOFT, colors.HexColor("#047857"))
h2("7.1 Data dictionary")
code("""
tables:
  shop.orders:
    description: Order lines (one row per product per order), all channels.
    grain: one row per ordered product
    alias: o                       # used by query_metric
    time_column: order_date        # default period column
    refresh: daily, around 06:00 Europe/Paris
    caveats:
      - Official revenue only counts status = 'completed'.
    joins:
      customers: {table: shop.customers, alias: c, on: "o.customer_id = c.customer_id"}
    dimensions:
      segment: {sql: c.segment, join: customers, column: shop.customers.segment}
    columns:
      status:
        description: Order status
        values: {completed: Delivered and paid, cancelled: Cancelled before shipping}
        value_synonyms:
          cancelled: [canceled, cancellation, annulée]
      email: {description: Email address, pii: true}
""")
table([["Table key", "Meaning"],
       ["description, grain, owner, tags, synonyms", "Shown to the agent and indexed for search."],
       ["alias", "Table alias in compiled metric SQL (default t)."],
       ["time_column", "Period filters, grains, freshness, partition lint. Inferred from the partition key if absent."],
       ["refresh", "Expected cadence, returned by check_freshness."],
       ["caveats", "Returned by get_context, describe_table and as validate_query findings."],
       ["joins", "name → {table, alias, on, type (default LEFT)}. Used by dimensions."],
       ["dimensions", "name → {sql, join?, column?, description}. column links to the stored values for resolution."]],
      [55, 115])
table([["Column key", "Meaning"],
       ["description, unit, synonyms, caveats", "Documentation and search."],
       ["pii: true", "Never returned raw under policy pii: block; not enumerated by the resolver."],
       ["values", "stored value → human label. Labels are matched by the resolver."],
       ["value_synonyms", "stored value → list of user words (may be multilingual)."],
       ["values_complete", "Default true: values is the complete list (used to lint literals without a query)."],
       ["enumerable", "Force value enumeration for a non-LowCardinality column."]], [55, 115])
callout("YAML pitfall handled", "In YAML 1.1, <font face='Courier'>on</font>, <font face='Courier'>yes</font>, "
        "<font face='Courier'>no</font> are booleans. ClickSense's loader only treats true/false as booleans, so a "
        "join's <font face='Courier'>on:</font> key works as written.")
h2("7.2 Glossary")
code("""
terms:
  - term: sales
    definition: Ambiguous word — it can mean money, orders or units.
    senses:
      - {meaning: Revenue in EUR, maps_to: "metric:revenue"}
      - {meaning: Number of orders, maps_to: "metric:orders"}
  - term: revenue
    synonyms: [turnover, GMV]
    definition: Sum of amounts incl. VAT of delivered order lines.
    maps_to: "metric:revenue"
""")
p("A term with several <font face='Courier'>senses</font> is reported in <font face='Courier'>ambiguities</font> "
  "whenever it appears in a question. <font face='Courier'>maps_to: \"metric:x\"</font> boosts that metric and its "
  "table in get_context.")
h2("7.3 Metrics")
code("""
metrics:
  - name: revenue
    label: Revenue
    description: Sum of amounts including VAT of delivered order lines.
    table: shop.orders
    expression: sum(o.amount_eur)          # an aggregate over the table alias
    filters: ["o.status = 'completed'"]    # always applied
    time_column: order_date
    unit: EUR
    synonyms: [turnover, gmv]
    dimensions: [country, channel, category, segment]   # allowed (table columns always allowed)
""")
bullets(["Metrics combined in one call must share the table and the built-in filters.",
         "Ratios are fine: <font face='Courier'>round(sum(o.amount_eur) / count(), 2)</font>.",
         "Expressions and filters are trusted (authored by people, reviewed in git); filter <i>values</i> from the "
         "agent are always bound as parameters."])
h2("7.4 Examples and knowledge")
code("""
examples:
  - question: Top 10 products by revenue last month
    tags: [top, products, revenue]
    sql: |
      SELECT p.name, sum(o.amount_eur) AS revenue
      FROM shop.orders AS o INNER JOIN shop.products AS p ON o.product_id = p.product_id
      WHERE o.status = 'completed' AND o.order_date >= toStartOfMonth(today() - toIntervalMonth(1))
        AND o.order_date < toStartOfMonth(today())
      GROUP BY p.name ORDER BY revenue DESC LIMIT 10
    notes: The small table is on the right of the JOIN.
""")
code("""
---
id: revenue-rules
title: Revenue calculation rules
tags: [revenue, finance, vat]
applies_to: [shop.orders]        # listed by describe_table for these tables
---
Official revenue is the sum of amount_eur over lines with status = 'completed'. ...
""")
h2("7.5 Skills")
code("""
semantic/skills/investigate-metric-change/SKILL.md
---
name: investigate-metric-change
description: Find what drives a rise or drop of a metric between two periods.
when_to_use: Why did revenue drop or rise, variance analysis, anomaly.
---
1. Pin the metric and the two periods ...
""")
h2("7.6 The inbox loop")
p("When the agent discovers something reusable (a verified query, a definition, a caveat) it calls "
  "<font face='Courier'>suggest_knowledge</font>. A markdown file lands in <font face='Courier'>semantic/inbox/</font> "
  "with <font face='Courier'>status: proposed</font>. A person reviews it and moves the content into the right file. "
  "Nothing in the inbox is ever used automatically, so a wrong conclusion cannot poison future answers.")

# ============================================================================== 8 disambiguation
h1("8. Disambiguation in depth")
p("Most wrong answers from data agents are not SQL errors: the query runs and returns a plausible number for a "
  "slightly different question. ClickSense treats ambiguity as a first-class result.")
h2("8.1 Kinds of ambiguity and how each is surfaced")
table([["Ambiguity", "Example", "Surfaced by"],
       ["Business term with several meanings", "\"sales\", \"CA\"", "glossary senses → get_context ambiguities"],
       ["Column choice", "\"country\": delivery vs customer", "glossary, find_columns, values in several columns"],
       ["Value wording", "\"France\" vs 'FR', \"canceled\" vs 'cancelled'", "resolve_values, query_metric, unknown_value lint"],
       ["Value shared by columns", "a city that is also a store name", "locate_value, get_context"],
       ["Period", "\"recently\", \"this year\"", "explicit period in query_metric responses; skill guidance"],
       ["Data not yet loaded", "\"today\" on a daily batch", "check_freshness, knowledge 'data-calendar'"]],
      [45, 55, 70])
h2("8.2 How values are resolved")
p("Sources, cheapest first:")
bullets(["<b>Dictionary</b> labels and value synonyms — no query.",
         "<b>Cached enumeration</b> of the column's distinct values with frequencies (up to 2000), computed with a "
         "bounded query — sampled (SAMPLE) or windowed (last 365 days) on very large tables — and cached for an hour.",
         "<b>Live search</b> for high-cardinality text columns: accent/case-insensitive contains, then n-gram distance; "
         "20 most frequent matches, bounded by read limits."])
table([["Match", "Score", "Example"],
       ["exact", "1.00", "'FR' → 'FR'"],
       ["case/accent/punctuation", "0.97", "'saint etienne' → 'Saint-Étienne'"],
       ["label / synonym", "0.96", "'France' → 'FR', 'business' → 'B2B'"],
       ["prefix / contains", "0.82 / 0.75", "'Saint-É' → 'Saint-Étienne'"],
       ["fuzzy (WRatio ≥ 72)", "≤ 0.88", "'Lyonn' → 'Lyon'"],
       ["live_search", "0.65-0.97", "names in a 10M-row column"]], [55, 25, 90])
table([["Status", "Rule", "What the agent should do"],
       ["exact", "Top score 1.0", "Use the value."],
       ["resolved", "One candidate ≥ 0.9 clearly ahead", "Use value; mention the mapping in the answer."],
       ["ambiguous", "Several strong candidates, or best &lt; 0.9", "Ask the user with the candidates."],
       ["not_found", "No candidate", "If coverage is complete, the value does not occur: say so. Else try another "
        "spelling or locate_value."]], [25, 60, 85])

# ============================================================================== 9 safety
h1("9. Safety model: not taking the database down")
callout("Principle", "The ClickHouse account is the security boundary. Everything in the MCP server is defence in "
        "depth: it prevents mistakes and load spikes, and explains refusals so the agent can fix its query, but it "
        "is not a substitute for a read-only account with constraints.")
h2("9.1 Line 1 — the ClickHouse account")
p("<font face='Courier'>dev/production-account.sql</font> creates the production setup with SQL-driven access "
  "control (<font face='Courier'>dev/clickhouse/users.xml</font> is the XML equivalent used by the demo):")
code("""
CREATE SETTINGS PROFILE IF NOT EXISTS clicksense_ro SETTINGS
    readonly = 2 CONST,  allow_ddl = 0 CONST,  allow_introspection_functions = 0 CONST,
    max_execution_time = 30 MAX 60,          max_memory_usage = 2000000000 MAX 4000000000,
    max_threads = 4 MAX 8,                   max_rows_to_read = 2000000000 MAX 5000000000,
    max_concurrent_queries_for_user = 8 MAX 8,  priority = 5 MIN 5,
    max_bytes_before_external_group_by = 1000000000,
    cancel_http_readonly_queries_on_client_close = 1;
CREATE QUOTA clicksense_quota FOR INTERVAL 1 hour
    MAX queries = 5000, errors = 1000, read_rows = 200000000000, execution_time = 7200 TO clicksense;
CREATE USER clicksense IDENTIFIED WITH sha256_password BY 'change-me'
    HOST IP '10.0.0.0/8' SETTINGS PROFILE 'clicksense_ro';
GRANT SELECT ON shop.* TO clicksense;
GRANT SELECT ON system.data_skipping_indices TO clicksense;
""")
bullets(["<b>readonly = 2</b>: data can be read and settings changed — required so the server can send tighter "
         "per-query limits. readonly = 1 would silently drop them.",
         "<b>MAX / MIN / CONST</b> constraints: no client (and no model) can raise time, memory, threads or priority.",
         "<b>priority = 5 MIN 5</b>: agent queries always yield to interactive and ETL workloads.",
         "<b>Quota</b>: an hourly ceiling on queries, errors, rows read and execution time.",
         "Optional on recent versions: a dedicated workload (CREATE WORKLOAD) with its own CPU share.",
         "Verified by the test suite: with the guard bypassed, CREATE, INSERT, DROP and SETTINGS max_threads = 64 are "
         "refused by ClickHouse itself."])
h2("9.2 Line 2 — the server, before sending")
h3("Static guard")
table([["Rule", "Refusal code"],
       ["Exactly one statement (a ';' outside string literals is refused)", "multiple_statements"],
       ["Only SELECT / WITH ... SELECT / UNION / INTERSECT / EXCEPT", "not_a_select"],
       ["No SETTINGS, FORMAT or INTO OUTFILE clause", "settings_clause, format_clause, into_clause"],
       ["No external or time-burning functions: url, s3, gcs, hdfs, file, remote, cluster, mysql, postgresql, jdbc, "
        "odbc, mongodb, redis, sqlite, executable, input, iceberg, deltaLake, hudi, merge, generateRandom, sleep, "
        "sleepEachRow, dictionary, ...", "forbidden_function"],
       ["No system / information_schema tables (use the tools)", "system_table"],
       ["Database allow-list and table deny-list", "database_not_allowed, table_denied"],
       ["PII columns not selected raw (SELECT * included; aggregates allowed)", "pii_column"],
       ["Length limit", "too_long"]], [120, 50])
p("Parsing uses sqlglot. For valid ClickHouse syntax that sqlglot does not know (for example "
  "<font face='Courier'>LIMIT 1 BY x LIMIT 5</font>), the guard asks ClickHouse's own parser "
  "(<font face='Courier'>EXPLAIN AST</font>, parse only) and applies the same table and function rules to its tree.")
h3("Row cap by wrapping")
p("A query without a LIMIT, or with one above the cap, runs as <font face='Courier'>SELECT * FROM (&lt;your sql&gt;) "
  "LIMIT cap+1</font>. The model's text is never regenerated, and fetching cap+1 rows tells the server reliably "
  "whether the result was truncated.")
h3("Pre-flight estimate")
p("When the tables referenced are large enough to exceed the budget even in theory, the server runs "
  "<font face='Courier'>EXPLAIN ESTIMATE</font> (primary index analysis, no data read). Above "
  "<font face='Courier'>preflight_max_rows</font> the query is refused with <font face='Courier'>too_expensive</font>, "
  "the estimate per table and the advisor's findings (typically: add a filter on the partition column). Small "
  "tables skip the round trip.")
h3("Admission control and circuit breaker")
bullets(["<b>Token bucket</b>: rate_per_minute queries; above it, rate_limited with a retry delay.",
         "<b>Concurrency slots</b>: max_concurrent queries in flight; others wait up to queue_timeout_s, then busy.",
         "<b>Circuit breaker</b>: after breaker_failures consecutive <i>load</i> failures (timeout, memory limit, too many "
         "simultaneous queries, quota, network) the server stops sending queries for breaker_cooldown_s and answers "
         "circuit_open. SQL errors and bounded refusals (too many rows to read) do not count: bad SQL is not a reason "
         "to stop talking to the database."])
h2("9.3 Line 3 — every query carries its limits")
table([["Setting sent", "Purpose"],
       ["max_execution_time + timeout_overflow_mode=throw", "Hard time limit."],
       ["max_memory_usage, max_threads", "Resource caps per query."],
       ["max_rows_to_read, max_bytes_to_read (read_overflow_mode=throw)", "Read caps enforced while reading."],
       ["max_bytes_before_external_group_by / _sort", "Spill to disk instead of OOM."],
       ["max_result_rows", "Backstop to the row cap."],
       ["cancel_http_readonly_queries_on_client_close", "A dropped client cancels its query."],
       ["query_id = cs-&lt;uuid&gt;", "Cancellation by id (cancel_query) and tracing."],
       ["log_comment = clicksense:&lt;tool&gt;", "Find agent queries in system.query_log."],
       ["use_query_cache (+ nondeterministic/system handling = ignore)", "ClickHouse-side result reuse."],
       ["wait_end_of_query = 1", "Errors arrive as HTTP errors; exact read statistics."]], [95, 75])
h2("9.4 PII")
p("Columns marked <font face='Courier'>pii: true</font> can be filtered and aggregated (count, uniq) but not "
  "returned raw; <font face='Courier'>sample_rows</font> drops them; the resolver does not enumerate them; the "
  "audit log never contains parameter values.")

# ============================================================================== 10 perf
h1("10. Performance and tuning")
h2("10.1 Measured latencies")
p("Demo database on a MacBook, local ClickHouse 26.10, through the stdio transport:")
table([["Call", "Latency"],
       ["get_context, cold (enumerates LowCardinality values)", "≈ 280 ms"],
       ["get_context, warm", "≈ 2 ms"],
       ["query_metric, revenue by channel, 1 month (87k rows read)", "≈ 35 ms"],
       ["same call from the result cache", "≈ 2.5 ms"],
       ["run_query, 30 days of web_events (585k rows read)", "≈ 13 ms"],
       ["full scan of 20M rows", "≈ 110 ms"]], [120, 50])
h2("10.2 What makes it fast")
bullets(["Pooled HTTP connections with lz4 compression and no server sessions (sessions serialise queries).",
         "In-process result cache (TTL + LRU) and the ClickHouse query cache.",
         "Catalog, value sets and search indexes kept in memory; the semantic layer is parsed once per change.",
         "Server-side parameter binding ({name:Type}) — no string formatting, plan-friendly.",
         "Compiled metrics filter on the time column, which is the partition key and the ORDER BY prefix.",
         "Above all: better queries. The agent receives sorting keys, partitions, verified examples and lint findings "
         "before it runs anything."])
h2("10.3 Tuning guide")
table([["Symptom", "Adjust"],
       ["Legitimate queries refused as too_expensive", "Raise limits.preflight_max_rows, or document a time_column so "
        "metrics and lint push partition filters."],
       ["Timeouts on heavy but valid analyses", "Raise max_execution_time (within the profile MAX) or pre-aggregate "
        "with a materialized view and document it."],
       ["busy / rate_limited during agent bursts", "Raise max_concurrent / rate_per_minute — only if the cluster has "
        "headroom."],
       ["Slow first get_context on a wide warehouse", "Restrict allowed_databases; mark only useful columns "
        "enumerable; the cost is paid once per value_cache_ttl_s."],
       ["Stale answers", "Lower result_cache_ttl_s / query_cache_ttl_s, or set use_query_cache: false."]], [65, 105])

# ============================================================================== 11 ops
h1("11. Operations and troubleshooting")
h2("11.1 Audit log")
p("One JSON line per tool call that touched ClickHouse or was refused: timestamp, tool, status, SQL (truncated), "
  "parameter <i>names</i>, query_id, rows, rows/bytes read, cache hit, duration, error code. Join it with "
  "<font face='Courier'>system.query_log</font> on query_id for server-side details.")
code("""
{"ts": "2026-10-31T23:07:22+0100", "tool": "run_query", "status": "ok", "sql": "SELECT ...",
 "params": ["d"], "query_id": "cs-1bdaa266-...", "rows": 3, "read_rows": 585192,
 "read_bytes": 6437163, "cached": false, "ms": 13.1}
""")
code("""
-- all agent queries in the last hour, slowest first
SELECT query_id, log_comment, query_duration_ms, read_rows, memory_usage, exception
FROM system.query_log
WHERE log_comment LIKE 'clicksense:%' AND event_time > now() - INTERVAL 1 HOUR AND type != 'QueryStart'
ORDER BY query_duration_ms DESC LIMIT 20
""")
h2("11.2 Error codes")
table([["Code", "Origin", "Meaning and fix"],
       ["timeout, memory_limit", "ClickHouse 159 / 241", "Filter on partition/primary key, aggregate earlier, smaller "
        "JOIN right side. Counts for the breaker."],
       ["too_many_rows_to_read / _bytes_", "ClickHouse 158 / 307", "Read limit hit. Add filters or SAMPLE."],
       ["server_busy, quota_exceeded, network", "ClickHouse 202 / 201 / 210", "Load or availability. Counts for the "
        "breaker."],
       ["unknown_identifier, unknown_table, unknown_function", "ClickHouse 47 / 60 / 46", "Use describe_table; "
        "identifiers and functions are case-sensitive. Findings are attached when possible."],
       ["syntax_error, type_error, type_mismatch, not_an_aggregate", "ClickHouse 62 / 43 / 53 / 215", "SQL to fix."],
       ["access_denied, readonly, setting_constraint", "ClickHouse 497 / 164 / 452", "The account refused: by design."],
       ["too_expensive", "pre-flight", "Estimate above budget; see findings."],
       ["circuit_open, busy, rate_limited", "executor", "Back off and retry later with a cheaper query."],
       ["needs_clarification", "query_metric", "Ambiguous value: ask the user with the candidates."],
       ["unknown_metric / _dimension, mixed_tables, mixed_filters, bad_period, bad_grain, bad_filter", "metrics",
        "The message lists valid options."],
       ["guard codes", "guard", "See section 9.2."]], [52, 38, 80])
h2("11.3 Troubleshooting")
table([["Problem", "Check"],
       ["--check shows reachable: false", "Host/port (HTTP port, not 9000), credentials, network ACL (HOST IP of the user)."],
       ["readonly: 1 warning", "Set readonly = 2 in the profile; with 1, per-query limits are ignored."],
       ["setting_constraint on every query", "A limit in clicksense.yaml exceeds the profile MAX: lower it."],
       ["Live catalog unavailable", "The user needs SELECT on the allowed databases; system.tables/columns are readable "
        "by default."],
       ["Knowledge errors in server_status", "Invalid YAML or front-matter; the file is skipped and named."],
       ["Elicitation never shown", "The client does not advertise elicitation: the tool returns needs_clarification and "
        "the agent must ask in chat."]], [55, 115])

# ============================================================================== 12 testing & limits
h1("12. Testing, limits and FAQ")
h2("12.1 Test suite")
table([["File", "Covers"],
       ["tests/test_guard.py", "55 cases: allowed queries, attacks (writes, stacked statements, SETTINGS, external "
        "functions, system tables, PII), row cap, server-parse fallback."],
       ["tests/test_semantic.py", "Loader, search, glossary, resolver statuses, metric compiler, periods — offline."],
       ["tests/test_mcp.py", "readOnlyHint on every tool, isError on refusals, resources, prompts, native skills, "
        "elicitation with and without client support."],
       ["tests/test_http.py", "HTTP transport: 401 without or with a wrong token, access with the right one, bind "
        "policy, chat bridge against a remote server."],
       ["tests/test_windows_compat.py", "Windows rules checked from any OS: CRLF + ASCII scripts, .gitattributes, "
        "PYTHONUTF8, no --reload, requirements vs pyproject, tzdata, UTF-8 BOM, .env precedence."],
       ["tests/test_agent.py", "The OpenAI-compatible bridge with a scripted fake LLM: tool mapping, auto-context, "
        "refusals and bad arguments, clarification, step limit."],
       ["tests/test_live.py", "Against the demo database: limits, account boundary, pre-flight, breaker, slots, "
        "cancellation, cache, end-to-end tools, audit content. Skipped when ClickHouse is down."]], [42, 128])
h2("12.2 Known limits")
bullets(["The guard is defence in depth, not a security boundary (section 9).",
         "HTTP auth is static bearer tokens; TLS is left to a reverse proxy.",
         "EXPLAIN ESTIMATE covers MergeTree tables only; other engines are not pre-estimated (limits still apply).",
         "Column linting is skipped for queries over subqueries or CTEs; ClickHouse's error is translated instead.",
         "Search is lexical (BM25) and offline: a synonym missing from the dictionary will not be guessed.",
         "Metrics combine only when they share a table and built-in filters."])
h2("12.3 FAQ")
for q, a in [
    ("Can the agent write data?", "No. The guard only accepts SELECT, and the account is read-only. The only write "
     "is suggest_knowledge, which creates a file in semantic/inbox/."),
    ("Does it need an LLM or embeddings service?", "No. Everything runs locally and offline."),
    ("How do I add a second database?", "Grant SELECT on it to the account, add it to policy.allowed_databases and "
     "add a dictionary file for it."),
    ("Can synonyms be in several languages?", "Yes. Synonyms are user vocabulary; the demo keeps a few French words "
     "to show it."),
    ("Why wrap the query instead of adding LIMIT?", "So the SQL that runs is the SQL the model wrote, parameters and "
     "comments included, and truncation is detected exactly."),
]:
    p(f"<b>{q}</b> {a}")


def build() -> None:
    doc = Guide(str(OUT), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm,
                bottomMargin=20 * mm, title="ClickSense User Guide", author="ClickSense",
                subject="ClickHouse MCP server with a semantic layer")
    doc.multiBuild(story, onFirstPage=on_first, onLaterPages=on_page)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    build()
