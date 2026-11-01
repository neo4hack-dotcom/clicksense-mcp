"""One error type for everything a tool can refuse or fail on.

Every error carries a machine-readable `code`, a human `message` and, wherever we can say it,
a `hint`: what the caller should do instead. A refusal that does not say what would have been
accepted just makes the model guess again.

Where errors come from:
* the guard and the tools raise `Rejected` / `ClickSenseError` themselves (codes such as
  `not_a_select`, `too_expensive`, `needs_clarification`);
* ClickHouse errors are translated by `from_clickhouse()` using the table `_CH_CODES` below.

How they reach the caller: server.py turns every ClickSenseError into an MCP result with
`isError: true` and the payload of `to_dict()` ({ok: false, error, message, hint, ...extra}).

HOW TO HANDLE A NEW CLICKHOUSE ERROR
  Add a line to `_CH_CODES`: the numeric code from the ClickHouse message ("Code: 241"), a short
  stable name (it becomes `error` in the payload: never rename an existing one, agents and tests
  rely on them), whether it is an *infrastructure* failure (load/availability: counts towards the
  circuit breaker) and a hint written as an instruction to the agent.
"""
from __future__ import annotations

import re
from typing import Any


class ClickSenseError(Exception):
    """A refusal or failure that a tool reports to its caller instead of crashing.

    code:    stable machine-readable name ("timeout", "unknown_column", "needs_clarification").
    message: what happened, for a person or a model.
    hint:    what to do instead. Always give one when you can: it is what lets an agent recover.
    extra:   any structured detail (candidates, estimate, findings, query_id, infra...). Copied
             into the payload, so keep values JSON-serialisable.
    """

    def __init__(self, code: str, message: str, hint: str | None = None, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        """The payload returned to the MCP client (with isError set by server.py)."""
        out: dict[str, Any] = {"ok": False, "error": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        out.update(self.extra)
        return out


class Rejected(ClickSenseError):
    """The guard refused the statement before it reached ClickHouse."""


# ClickHouse error codes we translate: code -> (name, infra, hint).
# `infra=True` means the failure is about load or availability, not about the SQL: only those feed
# the circuit breaker (executor.CircuitBreaker). Bad SQL repeated 100 times must not pause the
# server for everybody; ten timeouts in a row must.
_CH_CODES: dict[int, tuple[str, bool, str]] = {
    159: ("timeout", True, "The query exceeded max_execution_time. Add a filter on the partition/time "
                           "column or the first ORDER BY key column, aggregate earlier, or use SAMPLE."),
    241: ("memory_limit", True, "The query exceeded max_memory_usage. Reduce GROUP BY cardinality, "
                                "avoid large JOIN right sides (put the smaller table on the right), or pre-aggregate."),
    # 158/307 are refusals ClickHouse makes while bounding the read: they protected the
    # server, so they do not count towards the breaker.
    158: ("too_many_rows_to_read", False, "The query would read too many rows. Filter on the partition key / "
                                         "primary key prefix, or use SAMPLE on tables that support it."),
    307: ("too_many_bytes_to_read", False, "The query would read too many bytes. Select fewer columns and filter "
                                          "on the primary key prefix."),
    202: ("server_busy", True, "Too many simultaneous queries on ClickHouse. Retry in a few seconds."),
    201: ("quota_exceeded", True, "The account quota is exhausted for this interval. Wait for the next interval."),
    210: ("network", True, "ClickHouse is unreachable."),
    209: ("network", True, "Socket timeout talking to ClickHouse."),
    394: ("cancelled", False, "The query was cancelled."),
    47: ("unknown_identifier", False, "A column or alias does not exist. Use describe_table to get exact names "
                                      "(ClickHouse identifiers are case-sensitive)."),
    60: ("unknown_table", False, "Table does not exist. Use list_tables to get exact names."),
    81: ("unknown_database", False, "Database does not exist. Use list_tables."),
    46: ("unknown_function", False, "Unknown function. ClickHouse function names are case-sensitive "
                                    "(e.g. toStartOfMonth, uniqExact, countIf)."),
    62: ("syntax_error", False, "ClickHouse could not parse the SQL."),
    43: ("type_error", False, "Illegal argument type. Cast explicitly (toString, toDate, toDecimal64...)."),
    53: ("type_mismatch", False, "Type mismatch. Compare values of the same type; dates as toDate('YYYY-MM-DD')."),
    386: ("type_error", False, "No common supertype. Cast both sides to the same type."),
    215: ("not_an_aggregate", False, "A selected column is neither aggregated nor in GROUP BY."),
    184: ("aggregate_in_where", False, "Aggregate functions are not allowed in WHERE; use HAVING."),
    452: ("setting_constraint", False, "A setting violates the account profile constraints."),
    497: ("access_denied", False, "The ClickHouse account has no access to this object."),
    164: ("readonly", False, "The account is read-only."),
    36: ("bad_arguments", False, "Bad function arguments."),
    10: ("not_found_column", False, "Column not found in block. Check aliases and GROUP BY."),
    352: ("ambiguous_column", False, "Ambiguous column name: qualify it with the table alias."),
}

_CODE_RE = re.compile(r"Code:\s*(\d+)")


def from_clickhouse(exc: Exception) -> ClickSenseError:
    """Turn any exception raised while talking to ClickHouse into a ClickSenseError.

    1. find the numeric code ("Code: 159") and look it up in _CH_CODES;
    2. keep only the human part of the message (the driver wraps it in URL, version, stack...);
    3. without a code, recognise network failures by their wording (they feed the breaker)."""
    text = str(exc)
    m = _CODE_RE.search(text)
    # clickhouse-connect wraps the server text ("Received ClickHouse exception, code: N, server
    # response: Code: N. DB::Exception: <message>. (NAME) (for url ...)"); keep <message>.
    inner = text.rsplit("DB::Exception:", 1)[-1] if "DB::Exception:" in text else text
    first = inner.strip().splitlines()[0] if inner.strip() else text
    first = re.sub(r"\s*\((for url|version|query:)[^)]*\)", "", first)
    first = re.sub(r"\s*\([A-Z_]{4,}\)\)?\s*$", "", first).strip()[:600]
    if m:
        code = int(m.group(1))
        name, infra, hint = _CH_CODES.get(code, ("clickhouse_error", False, None))
        return ClickSenseError(name, first, hint, clickhouse_code=code, infra=infra)
    lowered = text.lower()
    if any(k in lowered for k in ("connection refused", "timed out", "max retries", "name or service")):
        return ClickSenseError("network", first, "ClickHouse is unreachable.", infra=True)
    return ClickSenseError("clickhouse_error", first, None, infra=False)
