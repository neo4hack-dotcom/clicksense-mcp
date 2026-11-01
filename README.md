# ClickSense — a ClickHouse MCP server with a semantic layer

An MCP server (FastMCP, Python) that lets an AI agent answer business questions on ClickHouse
**correctly, fast, and without being able to take the database down**.

- **Versioned semantic layer**: data dictionary, glossary (with ambiguous terms), governed
  metrics, knowledge bank, verified SQL examples, skills.
- **Ad-hoc queries** that are guarded, pre-estimated, bounded and cached.
- **Control and disambiguation tools**: values ("France" → `'FR'`), columns, terms ("sales" =
  revenue, orders or units?), validation before execution, asking the user through MCP
  elicitation.

The full user guide is in [docs/ClickSense-User-Guide.pdf](docs/ClickSense-User-Guide.pdf); keeping
ClickSense up to date (settings, tables, metrics, terms, examples, rules, skills) is explained step
by step in [docs/UPDATING.md](docs/UPDATING.md) (also as
[docs/ClickSense-Maintenance-Guide.pdf](docs/ClickSense-Maintenance-Guide.pdf)).

## Quick start (≈ 2 minutes)

```bash
cd clicksense
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
```

A demo ClickHouse database (e-commerce, 23M rows) — local binary or Docker:

```bash
brew install --cask clickhouse
```

```bash
scripts/dev-clickhouse.sh start && scripts/dev-clickhouse.sh seed
```

(or `docker compose up -d`, then `docker compose exec -T clickhouse clickhouse-client --multiquery < dev/seed.sql`)

```bash
cp clicksense.example.yaml clicksense.yaml && cp .env.example .env
```

(`.env.example` holds the demo connection: port 18123, user `clicksense`, database `shop`.)

```bash
.venv/bin/clicksense --check
```

```bash
.venv/bin/python -m pytest -q
```

`--check` prints the server state (connection, `readonly` level, limits, semantic-layer
content). The 158 tests cover the guard (55 attacks), the offline semantic layer, the MCP
protocol and — when the local database is running — real integration (circuit breaker,
cancellation, pre-flight estimate, ClickHouse limits).
A scripted fake LLM covers the OpenAI-compatible bridge without any API key, and
`tests/test_windows_compat.py` pins the Windows rules (CRLF, ASCII, UTF-8 with BOM, tzdata...).

### Connect it to Claude Code / Claude Desktop

```json
{
  "mcpServers": {
    "clicksense": {
      "command": "/path/to/clicksense/.venv/bin/clicksense",
      "args": ["--config", "/path/to/clicksense/clicksense.yaml"]
    }
  }
}
```

Over HTTP (streamable, MCP protocol 2026-07-28): `clicksense --transport http --port 3110`
→ `http://127.0.0.1:3110/mcp`. Beyond localhost a bearer token is required (see the Windows
server section, it applies to any OS).

### Use it with an OpenAI-compatible LLM (your own API key)

The model does not need to speak MCP: `clicksense-chat` is the MCP client in between. It hands
the ClickSense tools to any OpenAI-compatible chat API (OpenAI, Azure OpenAI, Mistral, Groq,
DeepSeek, OpenRouter, vLLM, Ollama, LM Studio…) as function tools, runs the calls, and loops
until the model answers.

```bash
uv pip install --python .venv/bin/python -e ".[llm]"
cp .env.example .env        # then put your key in it: OPENAI_API_KEY=sk-...
```

```bash
.venv/bin/clicksense-chat                                      # interactive
```

```bash
.venv/bin/clicksense-chat -q "Revenue by channel in France last month"
```

```bash
.venv/bin/clicksense-chat --base-url https://api.mistral.ai/v1 --model mistral-large-latest
```

Before each question the bridge calls `get_context` itself (`llm.auto_context`), so ambiguities
reach the model even if it would have skipped that step; when the model calls
`ask_user_to_clarify`, the question is asked in the terminal. In code:

```python
from clicksense.agent import Agent, connect
from clicksense.config import load_settings

settings = load_settings("clicksense.yaml")
async with connect(settings) as client:
    agent = Agent(settings.llm, client)
    await agent.start()
    print(await agent.ask("What was revenue in France last month?"))
```

## Install on a Windows server

Same model as DOINg.AI: a local `.venv`, `pip install -r requirements.txt`, settings in `.env`,
and batch scripts to double-click. ClickHouse itself stays where it is (Linux): the Windows
server only needs network access to its HTTP port.

**Prerequisite:** Python 3.11+ from python.org — tick *Add python.exe to PATH* and, on a server,
*Install for all users* (the service account must be able to run it).

```bat
git clone https://github.com/neo4hack-dotcom/clicksense-mcp.git C:\clicksense
cd C:\clicksense
install-windows.bat
```

`install-windows.bat` checks Python, enables long paths (as Administrator), creates `.venv`,
installs the dependencies, creates `clicksense.yaml` and `.env`, **generates a bearer token**
for MCP clients, then runs a connection check. Then:

1. Edit `.env`: `CLICKSENSE_HOST`, `CLICKSENSE_USER`, `CLICKSENSE_PASSWORD`, `CLICKSENSE_DATABASE`
   (the read-only account from `dev/production-account.sql`), and `CLICKSENSE_HTTP_HOST=0.0.0.0`
   if other machines must connect.
2. `check-windows.bat` — connection, `readonly` level, tables.
3. `serve-windows.bat` — the MCP server over HTTP in the foreground (`http://<server>:3110/mcp`).
4. In an elevated PowerShell, run it at boot as a scheduled task (restarts on failure, log in
   `logs\clicksense-http.log`; `-OpenFirewall` opens the port for the Domain/Private profiles):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\service.ps1 install -OpenFirewall
powershell -ExecutionPolicy Bypass -File scripts\windows\service.ps1 status
```

(`start`, `stop`, `restart`, `uninstall` too.) `chat-windows.bat` runs `clicksense-chat` with the
LLM settings from `.env`. Offline networks: pip uses the mirror in `pip.ini`.

**Security on a server.** The server refuses to listen beyond `127.0.0.1` without a token.
Clients send `Authorization: Bearer <token>` (one of `CLICKSENSE_HTTP_TOKENS`, comma-separated,
24+ characters; `clicksense --gen-token` makes one). Traffic is plain HTTP: for TLS, put the
server behind your reverse proxy (IIS/ARR, nginx) or keep it on a trusted network.

Connect a client to the Windows server:

```json
{"mcpServers": {"clicksense": {"url": "http://winserver:3110/mcp",
                               "headers": {"Authorization": "Bearer <token>"}}}}
```

or from another machine with the chat bridge: `CLICKSENSE_MCP_TOKEN=<token> clicksense-chat --mcp-url http://winserver:3110/mcp`.

## What the agent sees

### Tools (23)

| Family | Tool | Purpose |
|---|---|---|
| Discovery | `get_context` | **Entry point.** For a question: relevant tables and columns, metrics, glossary, values spotted in the question, ambiguities, verified SQL examples, rules, suggested skills. |
| | `list_tables`, `describe_table` | Live schema + dictionary: meaning, units, PII, values and synonyms, keys, joins, caveats, performance hints. |
| | `search_knowledge`, `get_knowledge` | BM25 search (accent/case/plural-insensitive) over the whole semantic layer. |
| | `list_skills`, `get_skill`, `list_metrics` | Playbooks and governed metrics. |
| Querying | `query_metric` | Metrics **without writing SQL**: official formula, resolved filter values, explicit period, declared joins. Returns the SQL it ran. |
| | `run_query` | One guarded, pre-estimated, bounded, cached SELECT. |
| | `sample_rows` | A few rows, PII columns removed. |
| Control | `validate_query` | Without running: guard verdict, unknown columns (did-you-mean), literals absent from the data, performance issues, caveats, estimated rows read. |
| | `explain_query` | Plan with index usage (partitions/granules skipped) and advice. |
| | `profile_column`, `check_freshness` | Bounded statistics; freshness vs expected cadence. |
| Disambiguation | `resolve_values` | User word → stored value: exact / case-accent-punctuation / synonym / label / fuzzy / live search (high cardinality). Status `exact`, `resolved`, `ambiguous`, `not_found`. |
| | `locate_value` | Which column holds "Lyon"? Flags a value present in several columns. |
| | `find_columns` | Which columns for "amount", "customer"… with an ambiguity flag. |
| | `ask_user_to_clarify` | Asks the user to choose (MCP elicitation, old and new protocol); otherwise returns `needs_clarification` so the agent asks in chat. |
| Ops | `server_status`, `cancel_query`, `refresh_catalog` | Health, breaker, running queries, caches; KILL of its own queries; reload. |
| Learning | `suggest_knowledge` | Proposes an example / term / caveat into `semantic/inbox/` for **human review** (never used automatically). |

Every tool declares `readOnlyHint` (true except `suggest_knowledge` and `cancel_query`) and
every refusal comes back with `isError` plus a `hint` saying what to do instead.

### Resources, prompts, skills

- Resources: `clicksense://guide`, `clicksense://catalog`, `clicksense://tables/{db}/{table}`,
  `clicksense://glossary`, `clicksense://metrics`, `clicksense://knowledge/{id}`.
- Native MCP skills (`skill://<name>/SKILL.md`, Agent Skills format) + one prompt per skill
  (`skill-<name>`) + an `analyze` prompt.

### Example: "What were sales in France on the website last month?"

`get_context` ranks `shop.orders` first, proposes the `revenue` metric, spots the values
(`website` → `orders.channel = 'web'`, `France` → `country = 'FR'`) and flags one ambiguity:
**sales** = revenue, number of orders, or units sold. Once the user picks revenue:

```json
query_metric {"metrics": ["revenue"], "dimensions": ["channel"],
              "filters": {"country": "France"}, "period": "last_month"}
```

→ formula `sum(o.amount_eur)` with `status = 'completed'`, explicit period, `value_resolutions:
France → FR`, parameterised SQL returned. If the agent writes `WHERE country = 'France'`
itself, `run_query` returns 0 rows **with a note**: "'France' does not occur in
shop.orders.country — Use 'FR'".

## Not taking the database down: three lines of defence

**1. The ClickHouse account (the real boundary).** `dev/clickhouse/users.xml` (dev) and
`dev/production-account.sql` (prod): `readonly = 2`, a profile capping time, memory, threads,
rows/bytes read, per-user concurrency, low priority, spill-to-disk for GROUP BY/ORDER BY,
**CONSTRAINTS** that forbid raising them, an hourly quota, `GRANT SELECT` on allowed databases
only. Tested: even bypassing the guard, `CREATE`, `INSERT`, `DROP` and `SETTINGS max_threads=64`
are refused by the server.

**2. The MCP server, before sending.**
- Guard (sqlglot, falling back to ClickHouse's own `EXPLAIN AST` for syntax sqlglot does not
  know): one statement, SELECT/WITH/UNION only, no `SETTINGS`/`FORMAT`/`INTO OUTFILE`, no
  external table functions (`url`, `s3`, `remote`, `file`, `mysql`, `merge`…), no `sleep`, no
  `system` tables, database allow-list, PII never returned raw.
- Row cap by **wrapping** (`SELECT * FROM (<sql>) LIMIT n+1`): the text that runs is the text
  the agent wrote, and a truncated result says so.
- **Pre-flight estimate**: on big tables, `EXPLAIN ESTIMATE` (index analysis, no data read);
  refused if the query would read more than the budget, with the fixes to apply.
- Admission control: token bucket (queries/min), N concurrent slots with a bounded queue, a
  **circuit breaker** that pauses after N consecutive load failures (timeouts, memory, network —
  not SQL errors).

**3. Every query carries its own limits**: `max_execution_time`, `max_memory_usage`,
`max_threads`, `max_rows_to_read`, `max_bytes_to_read`, spill thresholds,
`cancel_http_readonly_queries_on_client_close`, a unique `query_id` (cancellable) and
`log_comment = clicksense:<tool>` for tracing in `system.query_log`.

## Performance

Measured on the demo database (MacBook, local ClickHouse 26.10, stdio transport):

| Call | Latency |
|---|---|
| `get_context` cold (enumerates LowCardinality column values) | ≈ 280 ms |
| `get_context` warm | ≈ 2 ms |
| `query_metric` (revenue by channel, 1 month, 87k rows read) | ≈ 35 ms |
| same call, result cache | ≈ 2.5 ms |
| `run_query` 30 days of `web_events` (585k rows read) | ≈ 13 ms |
| full scan of 20M rows | ≈ 110 ms |

## Adapting it to your warehouse

Everything lives in `semantic/` (plain text files, versionable, hot-reloaded):

```
semantic/
  dictionary/*.yaml      tables, columns, values + synonyms, PII, joins, dimensions, caveats
  glossary.yaml          business terms; several `senses` = ambiguous -> the agent must settle it
  metrics.yaml           governed metrics (expression, filters, allowed dimensions)
  examples.yaml          question -> verified SQL
  knowledge/*.md         business rules, calendar, conventions (YAML front-matter)
  skills/<name>/SKILL.md playbooks (Agent Skills format)
  inbox/                 agent proposals, to review
```

Without a dictionary the server already works from ClickHouse `COMMENT`s and the live schema;
each entry you add improves answers. Start with `time_column`, caveats, `values` /
`value_synonyms` of frequently filtered columns, and 3 to 5 metrics. Synonyms are user
vocabulary and can be multilingual (the demo keeps a few French words to show it).

`clicksense-admin` does the tedious parts (`admin-windows.bat` on Windows):

```bash
clicksense-admin kb bootstrap shop.returns       # document a table from ClickHouse itself
clicksense-admin kb new metric net_revenue       # or term / example / knowledge / skill, from a commented template
clicksense-admin kb validate --live              # typos, broken references, SQL that no longer runs
clicksense-admin kb search "net revenue by channel last month"   # what the agent will find
clicksense-admin kb resolve shop.orders country France uk        # how words map to stored values
```

Files under `semantic/` are picked up by the running server within seconds. The step-by-step
recipes are in [docs/UPDATING.md](docs/UPDATING.md).

Configuration: every setting is documented in the code (`src/clicksense/config.py`) and in the
generated `clicksense.example.yaml` (`clicksense-admin config --example`; `config --show` prints
the effective values). `${VAR}` / `${VAR:-default}` are read from the environment and `.env`;
each setting with an `[env: ...]` mark can be overridden by that variable.

## Known limits

- The guard is defence in depth, not a security boundary: a read-only ClickHouse account with
  constraints remains mandatory.
- `EXPLAIN ESTIMATE` only covers MergeTree tables; views and exotic engines are not
  pre-estimated (per-query limits still apply).
- Column linting is skipped when a query reads subqueries/CTEs (columns not statically
  knowable); ClickHouse then returns the error, translated with a hint.
- Lexical search (BM25), no embeddings: deliberate (offline, fast), but a synonym missing from
  the dictionary will not be guessed.
- HTTP auth is static bearer tokens (no OAuth, no TLS termination): use a reverse proxy for TLS.

## Code layout

```
src/clicksense/
  server.py     MCP surface (tools, resources, prompts, skills, elicitation)
  service.py    tool logic, testable without MCP
  guard.py      static validation + row cap
  executor.py   the only path to ClickHouse: breaker, rate, slots, cache, limits
  catalog.py    live schema + dictionary
  knowledge.py  loading semantic/ + BM25
  resolver.py   value resolution
  lint.py       advisor (columns, values, performance)
  metrics.py    metric and period compilation
  audit.py      JSONL audit log (without parameter values)
  agent.py      OpenAI-compatible bridge (MCP client + tool-calling loop)
  chat.py       clicksense-chat command line
  kbtools.py    validate / scaffold / bootstrap the semantic layer
  admin.py      clicksense-admin command line
  templates/    commented templates for `clicksense-admin kb new`

Each module starts with a docstring explaining its role and how to extend it; every function and
class is documented. `src/clicksense/__init__.py` has the map.
```
