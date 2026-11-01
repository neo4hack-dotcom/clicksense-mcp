"""MCP surface (FastMCP): tools, resources and prompts over the ClickSense service.

Two protocol contracts are kept deliberately:

* `readOnlyHint` on every tool. Everything here reads, except `suggest_knowledge` (writes a
  file in semantic/inbox) and `cancel_query`; orchestrators cache and retry on that hint.
* `isError` on every failure: a refusal returned as a normal payload is indistinguishable,
  at the protocol level, from data, and a caller would treat it as an answer.

HOW TO ADD A TOOL (three steps)
  1. Write the logic as a method of ClickSense in service.py: plain arguments in, a dict with
     "ok": True out, `raise ClickSenseError(code, message, hint)` to refuse.
  2. Declare it in build_app() below with @mcp.tool(annotations=RO or RO_LIVE or a write
     annotation, tags={...}). Its docstring is what the model reads to decide when to call it:
     say what it returns and when to use it. Arguments use Annotated[..., Field(description=...)].
     Call the method through `_call(...)` so refusals become isError results.
  3. Add it to tests/test_mcp.py (hints) and test the logic in tests/test_live.py.
  Resources (@mcp.resource) and prompts (@mcp.prompt) follow the same pattern.

HOW TO CHANGE WHAT THE MODEL IS TOLD
  * INSTRUCTIONS below: sent to every client at connection; the workflow in a few lines.
  * Each tool's docstring and argument descriptions.
  * Skills (semantic/skills/*/SKILL.md): longer playbooks, editable without restart.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Annotated, Any, Literal

import mcp.types as mcp_types
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from pydantic import Field

from .config import ConfigError, Settings, load_settings
from .errors import ClickSenseError
from .service import ClickSense

INSTRUCTIONS = """\
ClickSense answers data questions on ClickHouse with a curated semantic layer (data
dictionary, glossary, governed metrics, knowledge bank, verified examples, skills) and a
guarded, bounded SQL executor. It is read-only.

Recommended workflow:
1. get_context(question) FIRST: relevant tables and columns, matching metrics, glossary
   terms, values spotted in the question, ambiguities, verified example queries, rules.
2. If `ambiguities` is not empty, settle them: ask_user_to_clarify, or the context itself.
   Never silently pick a meaning for an ambiguous term or value.
3. A governed metric fits? Use query_metric: the formula is the official one, filter values are
   resolved for you (France -> 'FR'), and the period is computed explicitly.
4. Otherwise write SQL: describe_table for exact column names (case-sensitive),
   resolve_values for every literal you filter on, validate_query, then run_query.
5. Read `notes`, `truncated` and `empty_hint` in results. A truncated result is not an answer;
   0 rows often means a wrong literal.

ClickHouse SQL tips: filter on the partition/time column and the ORDER BY prefix; aggregate in
SQL (count(), sum(), uniq(), quantile()); countIf/sumIf for conditional aggregates; put the
smaller table on the right of a JOIN; pass values as {name:Type} parameters in `params`.
No SETTINGS/FORMAT clauses, no system tables (use the tools), one statement per call.
"""

# Tool annotations. RO: read-only and idempotent (same call, same answer: clients may cache or
# retry it). RO_LIVE: read-only but the answer depends on live data or state (not idempotent).
# Writes (suggest_knowledge, cancel_query) declare readOnlyHint False explicitly.
RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
RO_LIVE = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}

# Module scope on purpose: pydantic resolves Annotated aliases against the module namespace.
SqlArg = Annotated[str, Field(description="One ClickHouse SELECT statement (WITH/UNION allowed).")]
ParamsArg = Annotated[dict[str, Any] | None, Field(
    description="Values for {name:Type} placeholders, e.g. SQL `WHERE country = {c:String}` with {\"c\": \"FR\"}.")]
TableArg = Annotated[str, Field(description="Table as database.table (or table in the default database).")]
FormatArg = Annotated[Literal["json", "markdown", "csv"], Field(
    description="json: columns + rows arrays (default). markdown: a table to show a person. csv: export.")]


def _clarified(action: str | None, choice: Any) -> dict:
    """Payload of ask_user_to_clarify once the client answered (accept + choice) or declined."""
    if action == "accept" and choice is not None:
        return {"ok": True, "status": "answered", "choice": choice}
    return {"ok": True, "status": action or "declined",
            "action": "The user did not choose. Do not guess: state the ambiguity in your answer."}


def _client_can_elicit(ctx: Context) -> bool:
    """Whether the connected client declared the elicitation capability. Unknown -> True (try, and fall back on
    the error).
    """
    session = getattr(ctx.request_context, "session", None)
    caps = getattr(session, "client_capabilities", None)
    return True if caps is None else caps.elicitation is not None  # unknown: try, and fall back on error


def _no_elicitation(question: str, options: list[str], exc: Exception | None) -> dict:
    """Payload telling the agent to ask the question in chat itself, because the client cannot show a choice
    dialog.
    """
    out = {"ok": True, "status": "needs_clarification", "question": question, "options": options,
           "action": "Elicitation is unavailable in this client: ask the user this question in your reply and "
                     "wait for the answer."}
    if exc is not None:
        out["detail"] = type(exc).__name__
    return out


def _err(exc: ClickSenseError) -> ToolResult:
    """A ClickSenseError as an MCP tool result with isError=true: callers must see a refusal as a failure, not as
    data.
    """
    payload = exc.to_dict()
    return ToolResult(content=json.dumps(payload, ensure_ascii=False, default=str), structured_content=payload,
                      is_error=True)


def _call(fn, *args, **kwargs):
    """Run a service method; turn ClickSenseError into an isError result, let anything else propagate (a bug)."""
    try:
        return fn(*args, **kwargs)
    except ClickSenseError as exc:
        return _err(exc)


def bearer_auth(tokens: list[str]):
    """Static bearer tokens, compared in constant time. Each MCP client sends
    `Authorization: Bearer <token>`; anything else gets 401 before reaching a tool."""
    import hmac

    from fastmcp.server.auth.providers.debug import DebugTokenVerifier

    expected = [t.encode() for t in tokens]

    def valid(token: str) -> bool:
        """Constant-time comparison against every accepted token."""
        given = token.encode()
        return any(hmac.compare_digest(given, t) for t in expected)

    return DebugTokenVerifier(validate=valid, client_id="clicksense-token")


def build_app(settings: Settings | None = None, service: ClickSense | None = None, *,
              auth: Any = None) -> FastMCP:
    """Create the FastMCP application: tools, resources, prompts and skills over one ClickSense service.

    `auth` (HTTP transport only) is the bearer-token verifier from bearer_auth(); None for stdio.
    """
    cs = service or ClickSense(settings or load_settings())
    mcp = FastMCP("clicksense", instructions=INSTRUCTIONS, version="1.0.0", auth=auth)
    mcp.clicksense = cs  # type: ignore[attr-defined]  # handy for tests and embedding

    # ------------------------------------------------------------------ discovery
    @mcp.tool(annotations=RO, tags={"discovery"})
    def get_context(
        question: Annotated[str, Field(description="The user's question, verbatim.")],
        limit: Annotated[int, Field(ge=1, le=20)] = 6,
    ) -> dict:
        """START HERE. Everything relevant to a question in one call: candidate tables with columns and
        keys, governed metrics, glossary definitions, values recognised in the question (e.g. 'France' ->
        orders.country = 'FR'), ambiguities to settle, verified example SQL, knowledge-bank rules, and
        suggested skills."""
        return _call(cs.get_context, question, limit)

    @mcp.tool(annotations=RO, tags={"discovery"})
    def list_tables(search: Annotated[str | None, Field(description="Optional keywords to filter.")] = None) -> dict:
        """Tables available, with description, row count and grain."""
        return _call(cs.list_tables, search)

    @mcp.tool(annotations=RO, tags={"discovery"})
    def describe_table(table: TableArg, include_samples: bool = False) -> dict:
        """Full description of one table: columns (type, meaning, unit, PII, allowed values and their
        synonyms), sorting/partition keys, declared joins and dimensions, caveats, related metrics and
        performance hints. Optionally 5 sample rows (PII hidden)."""
        return _call(cs.describe_table, table, include_samples)

    @mcp.tool(annotations=RO, tags={"knowledge"})
    def search_knowledge(
        query: str,
        kinds: Annotated[list[Literal["table", "column", "metric", "term", "knowledge", "skill", "example"]] | None,
                         Field(description="Restrict to these kinds.")] = None,
        limit: Annotated[int, Field(ge=1, le=30)] = 8,
    ) -> dict:
        """Search the knowledge bank, data dictionary, glossary, metrics, skills and verified examples."""
        return _call(cs.search_knowledge, query, kinds, limit)

    @mcp.tool(annotations=RO, tags={"knowledge"})
    def get_knowledge(doc_id: str) -> dict:
        """Full text of one knowledge-bank document (ids come from get_context / search_knowledge)."""
        return _call(cs.get_knowledge, doc_id)

    @mcp.tool(annotations=RO, tags={"knowledge"})
    def list_skills() -> dict:
        """Playbooks for recurring analyses (how to investigate a drop, check data quality, ...)."""
        return _call(cs.list_skills)

    @mcp.tool(annotations=RO, tags={"knowledge"})
    def get_skill(name: str) -> dict:
        """The step-by-step instructions of one skill. Follow them."""
        return _call(cs.get_skill, name)

    @mcp.tool(annotations=RO, tags={"metrics"})
    def list_metrics() -> dict:
        """Governed business metrics: name, meaning, table, allowed dimensions."""
        return _call(cs.list_metrics)

    # ------------------------------------------------------------------ querying
    @mcp.tool(annotations=RO_LIVE, tags={"query", "metrics"})
    def query_metric(
        metrics: Annotated[list[str], Field(description="Metric names (or synonyms), e.g. [\"revenue\"].")],
        dimensions: Annotated[list[str] | None, Field(description="Group-by dimensions, e.g. [\"country\"].")] = None,
        filters: Annotated[dict[str, Any] | None, Field(
            description="Dimension -> value, list of values, or {op: value} with op in = != > >= < <= in not_in. "
                        "Values in the user's words are fine: they are resolved to stored values.")] = None,
        period: Annotated[str | dict[str, str] | None, Field(
            description="last_30_days, this_month, last_month, ytd, last_year, last_N_days|weeks|months, ... "
                        "or {\"from\": \"YYYY-MM-DD\", \"to\": \"YYYY-MM-DD\"} (inclusive).")] = None,
        grain: Annotated[Literal["hour", "day", "week", "month", "quarter", "year"] | None,
                         Field(description="Time bucket for a trend.")] = None,
        order_by: Annotated[str | None, Field(description="A metric, a dimension, or 'period'.")] = None,
        descending: bool = True,
        limit: Annotated[int, Field(ge=1, le=5000)] = 100,
        format: FormatArg = "json",
    ) -> dict:
        """Compute governed metrics without writing SQL: official formula, resolved filter values, explicit
        period, guarded execution. Returns the SQL it ran. Prefer this whenever a metric exists."""
        return _call(cs.query_metric, metrics, dimensions, filters, period, grain, order_by, descending, limit,
                     format)

    @mcp.tool(annotations=RO_LIVE, tags={"query"})
    def run_query(
        sql: SqlArg,
        params: ParamsArg = None,
        max_rows: Annotated[int | None, Field(ge=1, le=5000, description="Rows to return (default 200).")] = None,
        format: FormatArg = "json",
    ) -> dict:
        """Run one read-only SELECT. Guarded (single SELECT, no system tables or external table functions),
        pre-flighted (refused if the index estimate exceeds the read budget), bounded (time, memory, threads,
        rows read, rows returned) and cached. Check `truncated`, `notes` and `empty_hint` in the result."""
        return _call(cs.run_query, sql, params, max_rows, format)

    @mcp.tool(annotations=RO, tags={"control"})
    def validate_query(sql: SqlArg, params: ParamsArg = None, check_values: bool = True) -> dict:
        """Check a query WITHOUT running it: guard verdict, unknown columns (did-you-mean), filter literals
        that do not exist in the data (e.g. 'France' vs 'FR'), performance issues (no partition filter,
        SELECT *, JOIN order, ...), table caveats, and the index-based estimate of rows to read."""
        return _call(cs.validate_query, sql, params, check_values)

    @mcp.tool(annotations=RO, tags={"control"})
    def explain_query(sql: SqlArg, params: ParamsArg = None) -> dict:
        """Execution plan with index usage (partitions/granules skipped), the rows estimate, and
        optimisation findings. Use it when a query is slow or refused as too expensive."""
        return _call(cs.explain_query, sql, params)

    @mcp.tool(annotations=RO_LIVE, tags={"query"})
    def sample_rows(
        table: TableArg,
        n: Annotated[int, Field(ge=1, le=100)] = 10,
        columns: list[str] | None = None,
        where: Annotated[str | None, Field(description="Optional SQL condition, without WHERE.")] = None,
    ) -> dict:
        """A few raw rows to see what the data looks like (PII columns are left out)."""
        return _call(cs.sample_rows, table, n, columns, where)

    # ------------------------------------------------------------------ disambiguation
    @mcp.tool(annotations=RO, tags={"control"})
    def resolve_values(
        table: TableArg,
        column: str,
        terms: Annotated[list[str], Field(description="Values as the user said them, e.g. [\"France\", \"cancelled orders\"].")],
    ) -> dict:
        """Map user wording to stored values of a column: exact, case/accent-insensitive, dictionary synonyms
        and labels, fuzzy, and live search on high-cardinality columns. Each term gets a status
        (exact | resolved | ambiguous | not_found), candidates with row counts, and a value to use."""
        return _call(cs.resolve_values, table, column, terms)

    @mcp.tool(annotations=RO, tags={"control"})
    def locate_value(
        term: Annotated[str, Field(description="A value, e.g. 'Lyon', 'VIP', 'marketplace'.")],
        tables: list[str] | None = None,
    ) -> dict:
        """Which table/column stores this value? Searches low-cardinality columns. Flags a value present in
        several columns (e.g. a city name that is also a store name)."""
        return _call(cs.locate_value, term, tables)

    @mcp.tool(annotations=RO, tags={"control"})
    def find_columns(term: Annotated[str, Field(description="A business word, e.g. 'client', 'montant'.")]) -> dict:
        """Which columns a business term may refer to, ranked, with a flag when several fit."""
        return _call(cs.find_columns, term)

    @mcp.tool(annotations=RO_LIVE, tags={"control"})
    def profile_column(table: TableArg, column: str) -> dict:
        """Bounded statistics for one column: rows, nulls, approx distinct, min/max, quantiles, top values.
        Sampled or windowed on very large tables (the `scope` field says which)."""
        return _call(cs.profile_column, table, column)

    @mcp.tool(annotations=RO_LIVE, tags={"control"})
    def check_freshness(table: TableArg) -> dict:
        """Latest timestamp in the table and the lag versus now, plus the expected refresh cadence when
        documented. Use before answering 'today' / 'yesterday' questions."""
        return _call(cs.check_freshness, table)

    @mcp.tool(annotations=RO_LIVE, tags={"control"})
    async def ask_user_to_clarify(
        question: Annotated[str, Field(description="A short question for the user.")],
        options: Annotated[list[str], Field(min_length=2, max_length=8,
                                            description="The candidate interpretations or values.")],
        ctx: Context,
    ) -> Any:
        """Ask the user to choose between interpretations when a term or value is ambiguous. Uses MCP
        elicitation when the client supports it; otherwise returns `needs_clarification` so you ask in chat."""
        # Second round of the 2026-07-28 input-required flow: the client's answer is here.
        answer = (ctx.input_responses or {}).get("choice")
        if answer is not None:
            return _clarified(getattr(answer, "action", None), (getattr(answer, "content", None) or {}).get("value"))
        if not _client_can_elicit(ctx):
            return _no_elicitation(question, options, None)
        try:
            res = await ctx.elicit(question, response_type=options)
        except ToolError as exc:
            if "unavailable on" in str(exc):
                # Modern protocol: no server-initiated requests. Ask through an InputRequiredResult;
                # the client answers and calls the tool again (handled just above).
                schema = {"type": "object", "required": ["value"],
                          "properties": {"value": {"type": "string", "title": "Choice", "enum": options}}}
                return mcp_types.InputRequiredResult(inputRequests={"choice": mcp_types.ElicitRequest(
                    params=mcp_types.ElicitRequestFormParams(message=question, requested_schema=schema))})
            return _no_elicitation(question, options, exc)
        except Exception as exc:  # client without elicitation support
            return _no_elicitation(question, options, exc)
        return _clarified(getattr(res, "action", None), getattr(res, "data", None))

    # ------------------------------------------------------------------ ops
    @mcp.tool(annotations=RO_LIVE, tags={"ops"})
    def server_status() -> dict:
        """Health and protection state: ClickHouse reachability/version, circuit breaker, running queries,
        caches, per-query limits, knowledge-layer counts and load errors."""
        return _call(cs.status)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                           "openWorldHint": False}, tags={"ops"})
    def cancel_query(query_id: Annotated[str, Field(description="A query_id issued by this server.")]) -> dict:
        """Cancel a running query that this server started (KILL QUERY by id)."""
        return _call(cs.executor.kill, query_id)

    @mcp.tool(annotations=RO_LIVE, tags={"ops"})
    def refresh_catalog() -> dict:
        """Reload the semantic layer files and the live schema, and clear caches."""
        return _call(cs.refresh)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                           "openWorldHint": False}, tags={"knowledge"})
    def suggest_knowledge(
        kind: Literal["example", "term", "caveat", "metric", "note"],
        title: str,
        content: Annotated[str, Field(description="What was learned, in plain words.")],
        sql: Annotated[str | None, Field(description="For examples: the verified SQL.")] = None,
    ) -> dict:
        """Propose an addition to the knowledge bank (a verified query, a definition, a caveat discovered).
        Written to semantic/inbox for human review; never used automatically."""
        return _call(cs.suggest_knowledge, kind, title, content, sql)

    # ------------------------------------------------------------------ resources
    @mcp.resource("clicksense://guide", mime_type="text/markdown", name="guide")
    def guide() -> str:
        """How to use this server and write fast ClickHouse SQL."""
        return INSTRUCTIONS

    @mcp.resource("clicksense://catalog", mime_type="application/json", name="catalog")
    def catalog() -> str:
        """All tables with descriptions and sizes."""
        return json.dumps(cs.list_tables(), ensure_ascii=False, default=str)

    @mcp.resource("clicksense://tables/{database}/{table}", mime_type="application/json", name="table")
    def table_resource(database: str, table: str) -> str:
        """Full description of one table."""
        return json.dumps(cs.describe_table(f"{database}.{table}"), ensure_ascii=False, default=str)

    @mcp.resource("clicksense://glossary", mime_type="application/json", name="glossary")
    def glossary() -> str:
        """Business glossary."""
        cs.knowledge.maybe_reload()
        return json.dumps(cs.knowledge.glossary, ensure_ascii=False, default=str)

    @mcp.resource("clicksense://metrics", mime_type="application/json", name="metrics")
    def metrics_resource() -> str:
        """Governed metric definitions."""
        cs.knowledge.maybe_reload()
        return json.dumps(list(cs.knowledge.metrics.values()), ensure_ascii=False, default=str)

    @mcp.resource("clicksense://knowledge/{doc_id}", mime_type="text/markdown", name="knowledge")
    def knowledge_resource(doc_id: str) -> str:
        """One knowledge-bank document."""
        d = cs.get_knowledge(doc_id)
        return f"# {d['title']}\n\n{d['body']}"

    # ------------------------------------------------------------------ prompts
    @mcp.prompt(name="analyze", description="Answer a data question with the recommended workflow.")
    def analyze(question: str) -> str:
        """Prompt `analyze`: run the recommended workflow on a question."""
        return (f"Answer this question using the clicksense tools: {question}\n\n"
                "Start with get_context. Settle every ambiguity before querying (ask me if needed). Prefer "
                "query_metric when a governed metric fits; otherwise validate_query then run_query. In the answer, "
                "state the period, filters and metric definition used, and any caveat.")

    for skill in cs.knowledge.skills.values():
        _register_skill_prompt(mcp, cs, skill["name"], skill["description"])
    # The same playbooks as native MCP skills (skill:// resources), for clients that load them.
    skills_dir = cs.settings.semantic_dir / "skills"
    if skills_dir.exists():
        from fastmcp.server.providers.skills import SkillsDirectoryProvider

        mcp.add_provider(SkillsDirectoryProvider(roots=skills_dir, reload=True))

    return mcp


def _register_skill_prompt(mcp: FastMCP, cs: ClickSense, name: str, description: str) -> None:
    """Expose a skill as an MCP prompt named skill-<name>; the body is read at call time, so edits apply without
    restart.
    """
    def prompt(question: str = "") -> str:
        """The skill body, prefixed with the question when one is given."""
        s = cs.get_skill(name)
        head = f"Follow the skill '{name}'" + (f" for: {question}" if question else "") + "\n\n"
        return head + s["body"]

    prompt.__name__ = f"skill_{name.replace('-', '_')}"
    mcp.prompt(name=f"skill-{name}", description=description or f"Skill {name}")(prompt)


LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MIN_TOKEN_CHARS = 24


def http_auth_or_exit(http, host: str) -> Any:
    """Auth for the HTTP transport, or exit with a clear message when the setup is unsafe."""
    weak = [t for t in http.tokens if len(t) < MIN_TOKEN_CHARS]
    if weak:
        _die(f"HTTP tokens must be at least {MIN_TOKEN_CHARS} characters. Generate one with: clicksense --gen-token")
    if http.tokens:
        return bearer_auth(http.tokens)
    if host not in LOOPBACK and not http.allow_unauthenticated_remote:
        _die(f"Refusing to listen on {host} without authentication. Set CLICKSENSE_HTTP_TOKENS in .env "
             "(generate one with: clicksense --gen-token), or listen on 127.0.0.1.")
    return None


def _die(message: str) -> None:
    """Print an error for the operator and exit with status 2 (configuration problem)."""
    print(f"clicksense: {message}", file=sys.stderr)
    raise SystemExit(2)


def main(argv: list[str] | None = None) -> None:
    """The `clicksense` command: stdio MCP server by default, `--transport http` for remote clients, `--check`
    for a status report, `--gen-token` for a new bearer token.
    """
    ap = argparse.ArgumentParser(prog="clicksense", description="ClickHouse semantic MCP server")
    ap.add_argument("--config", help="Path to clicksense.yaml (default: $CLICKSENSE_CONFIG or ./clicksense.yaml)")
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--host", help="HTTP bind address (default: http.host, 127.0.0.1)")
    ap.add_argument("--port", type=int, help="HTTP port (default: http.port, 3110)")
    ap.add_argument("--check", action="store_true", help="Validate config + connectivity, print status, exit")
    ap.add_argument("--gen-token", action="store_true", help="Print a new random bearer token and exit")
    args = ap.parse_args(argv)
    if args.gen_token:
        import secrets

        print(secrets.token_urlsafe(32))
        return
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        _die(str(exc))
    service = ClickSense(settings)
    if args.check:
        service.catalog.refresh()  # the catalog is lazy; load it so the report shows real table counts
        print(json.dumps(service.status(), indent=2, default=str))
        return
    if args.transport == "http":
        host = args.host or settings.http.host
        port = args.port or settings.http.port
        auth = http_auth_or_exit(settings.http, host)
        print(f"clicksense: MCP endpoint http://{host}:{port}/mcp  (auth: "
              f"{'bearer token' if auth else 'none, loopback only'})", file=sys.stderr)
        build_app(settings, service, auth=auth).run(transport="http", host=host, port=port)
    else:
        build_app(settings, service).run(transport="stdio", show_banner=False)
