"""The only code path that talks to ClickHouse.

Layers, outermost first — each one exists to keep a burst of agent calls from hurting the
database behind it:

1. circuit breaker: after N consecutive infrastructure failures, fail fast for a cooldown;
2. rate limiter (token bucket) and a bounded concurrency semaphore with a queue timeout;
3. result cache (in-process, TTL + LRU) for identical (sql, params);
4. per-query settings: time, memory, threads, rows/bytes read, spill-to-disk thresholds,
   lower priority, cancel-on-disconnect, ClickHouse query cache, `log_comment` for tracing;
5. a unique `query_id` per call, so a runaway query can be killed by id.

`wait_end_of_query=1` makes ClickHouse buffer the (already row-capped) result before
answering: errors arrive as HTTP errors instead of mid-stream garbage, and the
X-ClickHouse-Summary header carries the real read_rows / read_bytes.
"""
from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import ipaddress
import json
import math
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .errors import ClickSenseError, from_clickhouse


@dataclass
class QueryOutcome:
    columns: list[str]
    types: list[str]
    rows: list[list[Any]]
    query_id: str
    elapsed_ms: float
    read_rows: int | None = None
    read_bytes: int | None = None
    cached: bool = False
    truncated: bool = False
    truncated_reason: str | None = None

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"query_id": self.query_id, "elapsed_ms": round(self.elapsed_ms, 1),
                               "rows": len(self.rows), "cached": self.cached}
        if self.read_rows is not None:
            out["read_rows"] = self.read_rows
        if self.read_bytes is not None:
            out["read_bytes"] = self.read_bytes
        return out


def to_jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() and abs(v) < 2**53 else float(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, (uuid.UUID, ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", errors="replace")
    if isinstance(v, dict):
        return {str(to_jsonable(k)): to_jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [to_jsonable(x) for x in v]
    return str(v)


class CircuitBreaker:
    def __init__(self, threshold: int, cooldown_s: float):
        self.threshold, self.cooldown_s = threshold, cooldown_s
        self.failures = 0
        self.open_until = 0.0
        self.last_error: str | None = None
        self._lock = threading.Lock()

    def before(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self.open_until > now:
                raise ClickSenseError(
                    "circuit_open",
                    f"ClickHouse protection engaged after {self.failures} consecutive load/availability "
                    f"failures (last: {self.last_error}).",
                    f"Retry in {math.ceil(self.open_until - now)} s. Make the next query cheaper: "
                    "filter on the partition key, aggregate, or use SAMPLE.",
                )

    def success(self) -> None:
        with self._lock:
            self.failures = 0
            self.open_until = 0.0

    def failure(self, err: ClickSenseError) -> None:
        if not err.extra.get("infra"):
            return  # bad SQL is not a reason to stop talking to the database
        with self._lock:
            self.failures += 1
            self.last_error = err.code
            if self.failures >= self.threshold:
                self.open_until = time.monotonic() + self.cooldown_s

    @property
    def state(self) -> str:
        return "open" if self.open_until > time.monotonic() else ("degraded" if self.failures else "closed")


class TokenBucket:
    def __init__(self, per_minute: int):
        self.capacity = max(1, per_minute)
        self.tokens = float(self.capacity)
        self.rate = self.capacity / 60.0
        self.stamp = time.monotonic()
        self._lock = threading.Lock()

    def take(self) -> None:
        with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.rate)
            self.stamp = now
            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                raise ClickSenseError("rate_limited", "Too many queries per minute from this server.",
                                      f"Retry in {wait:.1f} s, and batch questions into fewer queries.")
            self.tokens -= 1


class TTLCache:
    def __init__(self, ttl_s: float, max_entries: int):
        self.ttl, self.max = ttl_s, max_entries
        self._d: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._d.get(key)
            if item and item[0] > time.monotonic():
                self._d.move_to_end(key)
                self.hits += 1
                return item[1]
            if item:
                del self._d[key]
            self.misses += 1
            return None

    def put(self, key: str, value: Any) -> None:
        if self.ttl <= 0 or self.max <= 0:
            return
        with self._lock:
            self._d[key] = (time.monotonic() + self.ttl, value)
            self._d.move_to_end(key)
            while len(self._d) > self.max:
                self._d.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()

    def __len__(self) -> int:
        return len(self._d)


@dataclass
class RunningQuery:
    query_id: str
    tool: str
    sql: str
    started: float = field(default_factory=time.time)


class Executor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.limits = settings.limits
        self.breaker = CircuitBreaker(self.limits.breaker_failures, self.limits.breaker_cooldown_s)
        self.bucket = TokenBucket(self.limits.rate_per_minute)
        self.slots = threading.BoundedSemaphore(self.limits.max_concurrent)
        self.cache = TTLCache(self.limits.result_cache_ttl_s, self.limits.result_cache_entries)
        self.running: dict[str, RunningQuery] = {}
        self.issued: OrderedDict[str, float] = OrderedDict()  # ids we may KILL
        self._client = None
        self._client_lock = threading.Lock()
        self.server_version: str | None = None
        self.readonly_level: int | None = None
        self.totals = {"queries": 0, "errors": 0, "read_rows": 0, "read_bytes": 0}

    # ------------------------------------------------------------------ connection
    @property
    def client(self):
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = self._connect()
        return self._client

    def _connect(self):
        import clickhouse_connect
        from clickhouse_connect.driver import httputil

        c = self.settings.connection
        try:
            client = clickhouse_connect.get_client(
                host=c.host, port=c.port, username=c.username, password=c.password, secure=c.secure,
                database=c.database, connect_timeout=c.connect_timeout,
                send_receive_timeout=c.send_receive_timeout, compress=c.compress,
                autogenerate_session_id=False,  # sessions serialise queries; we want a pool
                pool_mgr=httputil.get_pool_manager(maxsize=c.pool_size, num_pools=1),
            )
            self.server_version = client.server_version
            self.readonly_level = int(client.command("SELECT getSetting('readonly')") or 0)
        except Exception as exc:
            raise from_clickhouse(exc) from None
        return client

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except ClickSenseError:
            return False

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ------------------------------------------------------------------ settings
    def query_settings(self, tool: str, query_id: str, *, use_query_cache: bool | None = None,
                       overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        lim = self.limits
        s: dict[str, Any] = {
            "query_id": query_id,
            "max_execution_time": lim.max_execution_time,
            "timeout_overflow_mode": "throw",
            "max_memory_usage": lim.max_memory_usage,
            "max_threads": lim.max_threads,
            "max_rows_to_read": lim.max_rows_to_read,
            "max_bytes_to_read": lim.max_bytes_to_read,
            "read_overflow_mode": "throw",
            "max_bytes_before_external_group_by": lim.max_bytes_before_external_group_by,
            "max_bytes_before_external_sort": lim.max_bytes_before_external_sort,
            # The guard already caps rows with LIMIT; this is the backstop. 'throw', because the
            # ClickHouse query cache refuses to run with any non-throw overflow mode.
            "max_result_rows": lim.hard_max_rows + 1,
            "result_overflow_mode": "throw",
            "cancel_http_readonly_queries_on_client_close": 1,
            "wait_end_of_query": 1,
            "log_comment": f"clicksense:{tool}",
        }
        if use_query_cache if use_query_cache is not None else lim.use_query_cache:
            s.update(use_query_cache=1, query_cache_ttl=lim.query_cache_ttl_s,
                     query_cache_nondeterministic_function_handling="ignore",
                     query_cache_system_table_handling="ignore")
        if overrides:
            s.update(overrides)
        return s

    # ------------------------------------------------------------------ run
    def run(self, sql: str, params: dict[str, Any] | None = None, *, tool: str, fetch_limit: int | None = None,
            use_cache: bool = True, settings: dict[str, Any] | None = None, admit: bool = True) -> QueryOutcome:
        key = hashlib.sha256(json.dumps([sql, params or {}, fetch_limit, settings or {}], sort_keys=True,
                                        default=str).encode()).hexdigest()
        if use_cache:
            hit = self.cache.get(key)
            if hit is not None:
                return QueryOutcome(**{**hit.__dict__, "cached": True})

        self.breaker.before()
        if admit:
            self.bucket.take()
            if not self.slots.acquire(timeout=self.limits.queue_timeout_s):
                raise ClickSenseError("busy", f"All {self.limits.max_concurrent} query slots are in use.",
                                      "Retry shortly; avoid firing many heavy queries in parallel.")
        query_id = f"cs-{uuid.uuid4()}"
        self.running[query_id] = RunningQuery(query_id, tool, sql[:500])
        self._remember(query_id)
        started = time.perf_counter()
        try:
            result = self.client.query(
                sql, parameters=params or None,
                settings=self.query_settings(tool, query_id, use_query_cache=use_cache, overrides=settings),
            )
        except ClickSenseError as err:
            self.breaker.failure(err)
            self.totals["errors"] += 1
            raise
        except Exception as exc:
            err = from_clickhouse(exc)
            err.extra["query_id"] = query_id
            self.breaker.failure(err)
            self.totals["errors"] += 1
            raise err from None
        finally:
            self.running.pop(query_id, None)
            if admit:
                self.slots.release()
        elapsed = (time.perf_counter() - started) * 1000
        self.breaker.success()
        rows = result.result_rows
        truncated = False
        if fetch_limit is not None and len(rows) >= fetch_limit:
            rows, truncated = rows[: fetch_limit - 1], True
        summary = getattr(result, "summary", None) or {}
        out = QueryOutcome(
            columns=list(result.column_names),
            types=[t.name for t in result.column_types],
            rows=[[to_jsonable(v) for v in r] for r in rows],
            query_id=query_id,
            elapsed_ms=elapsed,
            read_rows=_int(summary.get("read_rows")),
            read_bytes=_int(summary.get("read_bytes")),
            truncated=truncated,
            truncated_reason="row_cap" if truncated else None,
        )
        self.totals["queries"] += 1
        self.totals["read_rows"] += out.read_rows or 0
        self.totals["read_bytes"] += out.read_bytes or 0
        if use_cache:
            self.cache.put(key, out)
        return out

    def explain(self, sql: str, params: dict[str, Any] | None = None, *, kind: str = "",
                fmt: str = "TabSeparatedRaw") -> str:
        """EXPLAIN [kind] as raw text. Index analysis / parsing only: reads no table data.

        Raw text rather than `query()`: EXPLAIN output is indented free text, which the Native
        decoder mistakes for type names."""
        self.breaker.before()
        query_id = f"cs-{uuid.uuid4()}"
        settings = self.query_settings("explain", query_id, use_query_cache=False,
                                       overrides={"max_execution_time": 10})
        try:
            raw = self.client.raw_query(f"EXPLAIN {kind} {sql}", parameters=params or None,
                                        settings=settings, fmt=fmt)
        except Exception as exc:
            err = from_clickhouse(exc)
            self.breaker.failure(err)
            raise err from None
        return raw.decode("utf-8", errors="replace")

    def explain_ast(self, sql: str) -> str:
        """ClickHouse's own parser, used by the guard when sqlglot cannot parse."""
        return self.explain(sql, kind="AST")

    def estimate(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Rows/marks/parts each MergeTree table would read, according to the primary index.
        Empty when ClickHouse can answer from metadata alone (e.g. a bare count())."""
        text = self.explain(sql, params, kind="ESTIMATE", fmt="TabSeparatedWithNames")
        lines = [ln.split("\t") for ln in text.splitlines() if ln]
        if not lines:
            return []
        head = lines[0]
        return [{k: (int(v) if v.isdigit() else v) for k, v in zip(head, row)} for row in lines[1:]]

    def kill(self, query_id: str) -> dict[str, Any]:
        if query_id not in self.issued:
            raise ClickSenseError("not_ours", "This server only cancels queries it started.",
                                  "Use the query_id returned by run_query or server_status.")
        try:
            self.client.command("KILL QUERY WHERE query_id = {qid:String} ASYNC", parameters={"qid": query_id})
        except Exception as exc:
            raise from_clickhouse(exc) from None
        return {"ok": True, "query_id": query_id, "status": "kill requested"}

    def _remember(self, query_id: str) -> None:
        self.issued[query_id] = time.time()
        while len(self.issued) > 2000:
            self.issued.popitem(last=False)


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
