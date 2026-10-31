"""Against the real local ClickHouse (scripts/dev-clickhouse.sh start && seed). Skipped otherwise."""
from __future__ import annotations

import json
import threading
import time

import pytest

from clicksense.errors import ClickSenseError, Rejected
from clicksense.executor import Executor
from clicksense.service import ClickSense

from .conftest import CH_UP, make_settings

pytestmark = pytest.mark.integration

# Slow on CPU but reads only 200k rows: a query that *reads* billions of rows to be slow would
# eat the account's hourly read_rows quota and make every later test fail with quota_exceeded.
SLOW = "SELECT count() FROM numbers(100000) AS a, numbers(100000) AS b WHERE a.number != b.number"


def test_run_query_basic(live):
    r = live.run_query("SELECT country, count() AS n FROM shop.orders WHERE order_date >= {d:Date} "
                       "GROUP BY country ORDER BY n DESC", {"d": "2026-01-01"})
    assert r["ok"] and r["columns"] == ["country", "n"] and r["rows"][0][0] == "FR"
    assert r["stats"]["read_rows"] > 0 and r["truncated"] is False


def test_row_cap_truncates_and_says_so(live):
    r = live.run_query("SELECT order_id FROM shop.orders WHERE order_date = '2026-03-01'", max_rows=10)
    assert r["row_count"] == 10 and r["truncated"] and r["truncated_reason"] == "row_cap"
    assert "NOT the full answer" in r["truncation_hint"]


def test_response_byte_budget(tmp_path):
    if not CH_UP:
        pytest.skip("no ClickHouse")
    cs = ClickSense(make_settings(tmp_path, max_response_bytes=2000))
    r = cs.run_query("SELECT order_id, status, channel FROM shop.orders WHERE order_date = '2026-03-01'",
                     max_rows=1000)
    assert r["truncated"] and r["truncated_reason"] == "response_bytes" and r["row_count"] < 1000


def test_wrong_literal_is_explained(live):
    r = live.run_query("SELECT count() AS n FROM shop.orders WHERE country = 'France' AND order_date >= '2026-01-01'")
    assert r["rows"] == [[0]]
    note = next(n for n in r["notes"] if n["code"] == "unknown_value")
    assert "'FR'" in note["fix"]


def test_result_cache(live):
    sql = "SELECT count() FROM shop.orders WHERE order_date = '2026-02-02'"
    a, b = live.run_query(sql), live.run_query(sql)
    assert not a["stats"]["cached"] and b["stats"]["cached"] and a["rows"] == b["rows"]


def test_preflight_refuses_full_scan(tmp_path):
    if not CH_UP:
        pytest.skip("no ClickHouse")
    cs = ClickSense(make_settings(tmp_path, preflight_max_rows=1_000_000))
    with pytest.raises(Rejected) as ei:
        cs.run_query("SELECT page, count() FROM shop.web_events GROUP BY page")
    assert ei.value.code == "too_expensive" and ei.value.extra["estimate"]
    assert any(f["code"] == "no_partition_filter" for f in ei.value.extra["findings"])
    # The same question on one day passes: the estimate sees the partition pruning.
    ok = cs.run_query("SELECT page, count() FROM shop.web_events WHERE event_date = '2026-03-01' GROUP BY page")
    assert ok["ok"]


def test_clickhouse_limits_are_enforced(live):
    with pytest.raises(ClickSenseError) as ei:
        live.executor.run("SELECT count() FROM shop.web_events WHERE page != ''", tool="t", use_cache=False,
                          settings={"max_rows_to_read": 1000})
    assert ei.value.code == "too_many_rows_to_read"


def test_account_is_the_real_boundary(live):
    """Bypass the guard entirely: the ClickHouse account still refuses writes and limit raises."""
    for sql in ("CREATE TABLE shop.x (a Int8) ENGINE = Memory", "INSERT INTO shop.products (product_id) VALUES (1)",
                "DROP TABLE shop.orders"):
        with pytest.raises(ClickSenseError) as ei:
            live.executor.run(sql, tool="t", use_cache=False)
        assert ei.value.code in ("access_denied", "readonly"), ei.value.code
    with pytest.raises(ClickSenseError) as ei:
        live.executor.run("SELECT 1", tool="t", use_cache=False, settings={"max_threads": 64})
    assert ei.value.code == "setting_constraint"


def test_server_parse_fallback_real(live):
    r = live.run_query("SELECT country, order_id FROM shop.orders WHERE order_date = '2026-03-01' "
                       "ORDER BY country, order_id LIMIT 1 BY country LIMIT 3")
    assert r["ok"] and r["row_count"] == 3
    with pytest.raises(Rejected):
        live.run_query("SELECT name FROM system.users ORDER BY name LIMIT 1 BY name LIMIT 3")


def test_circuit_breaker_opens_on_timeouts(tmp_path):
    if not CH_UP:
        pytest.skip("no ClickHouse")
    ex = Executor(make_settings(tmp_path, breaker_failures=2, breaker_cooldown_s=2))
    slow = SLOW
    for _ in range(2):
        with pytest.raises(ClickSenseError) as ei:
            ex.run(slow, tool="t", use_cache=False, settings={"max_execution_time": 1, "max_rows_to_read": 0})
        assert ei.value.code == "timeout"
    with pytest.raises(ClickSenseError) as ei:
        ex.run("SELECT 1", tool="t", use_cache=False)
    assert ei.value.code == "circuit_open"
    time.sleep(2.1)
    assert ex.run("SELECT 1", tool="t", use_cache=False).rows == [[1]]
    assert ex.breaker.state == "closed"


def test_bad_sql_does_not_trip_the_breaker(live):
    for _ in range(6):
        with pytest.raises(ClickSenseError):
            live.executor.run("SELECT nope FROM shop.orders", tool="t", use_cache=False)
    assert live.executor.breaker.state == "closed"


def test_concurrency_slots(tmp_path):
    if not CH_UP:
        pytest.skip("no ClickHouse")
    ex = Executor(make_settings(tmp_path, max_concurrent=1, queue_timeout_s=0.2))
    t = threading.Thread(target=lambda: _swallow(ex.run, SLOW, tool="t", use_cache=False,
                                                 settings={"max_execution_time": 2, "max_rows_to_read": 0}))
    t.start()
    time.sleep(0.5)
    with pytest.raises(ClickSenseError) as ei:
        ex.run("SELECT 1", tool="t", use_cache=False)
    assert ei.value.code == "busy"
    t.join()


def test_cancel_only_own_queries(live):
    with pytest.raises(ClickSenseError) as ei:
        live.executor.kill("someone-elses-query")
    assert ei.value.code == "not_ours"
    done = {}
    t = threading.Thread(target=lambda: done.setdefault("e", _swallow(
        live.executor.run, SLOW, tool="t",
        use_cache=False, settings={"max_execution_time": 20, "max_rows_to_read": 0})))
    t.start()
    time.sleep(0.7)
    qid = next(iter(live.executor.running))
    assert live.executor.kill(qid)["ok"]
    t.join(10)
    assert not t.is_alive() and done["e"].code == "cancelled"


def test_get_context_end_to_end(live):
    r = live.get_context("What were sales in France on the website last month?")
    assert r["tables"][0]["table"] == "shop.orders"
    assert r["metrics"][0]["name"] == "revenue"
    assert any(a["term"] == "sales" for a in r["ambiguities"])
    spotted = {v["term"]: v for v in r["values_spotted"]}
    assert spotted["website"]["matches"][0]["value"] == "web"
    assert any(m["value"] == "FR" for m in spotted["france"]["matches"])
    r = live.get_context("orders in Germany de facto")
    assert "de" not in {v["term"] for v in r.get("values_spotted", [])}  # a word, not Germany's code


def test_query_metric_end_to_end(live):
    r = live.query_metric(["revenue", "orders"], dimensions=["channel"], filters={"country": "France"},
                          period="last_month")
    assert r["ok"] and r["value_resolutions"][0]["to"] == "FR" and r["row_count"] == 4
    assert r["period"]["from"].endswith("-01")
    with pytest.raises(ClickSenseError) as ei:
        live.query_metric(["revenue"], filters={"city": "Saint"})
    assert ei.value.code == "needs_clarification"


def test_validate_and_explain(live):
    v = live.validate_query("SELECT devise, count() FROM shop.web_events GROUP BY devise")
    assert not v["valid"]
    f = next(x for x in v["findings"] if x["code"] == "unknown_column")
    assert "device" in f["fix"]
    v = live.validate_query("SELECT device, count() FROM shop.web_events WHERE event_date = today() GROUP BY device")
    assert v["valid"] and v["estimated_rows"] < 1_000_000
    e = live.explain_query("SELECT count() FROM shop.orders WHERE order_date = '2026-01-15' AND channel = 'web'")
    assert "PrimaryKey" in e["plan"] or "Partition" in e["plan"]


def test_disambiguation_tools(live):
    r = live.resolve_values("orders", "status", ["canceled", "Completed", "unknown"])
    statuses = {x["term"]: x["status"] for x in r["results"]}
    assert statuses == {"canceled": "resolved", "Completed": "resolved", "unknown": "not_found"}
    assert live.locate_value("Lyon")["matches"][0]["column"] == "city"
    assert live.find_columns("amount")["candidates"][0]["column"] == "amount_eur"
    with pytest.raises(Rejected):
        live.resolve_values("customers", "email", ["x"])


def test_profile_freshness_sample(live):
    p = live.profile_column("orders", "amount_eur")
    assert p["stats"]["rows"] == 3_000_000 and p["stats"]["min"] > 0
    f = live.check_freshness("orders")
    assert f["latest"] and "expected_refresh" in f
    s = live.sample_rows("customers", n=3)
    assert "email" not in s["columns"] and "email" in s["hidden_pii_columns"]


def test_audit_has_no_param_values(live, tmp_path):
    live.run_query("SELECT count() FROM shop.customers WHERE city = {c:String}", {"c": "Secret-City"})
    text = live.settings.audit_path.read_text()
    assert "Secret-City" not in text
    rec = json.loads(text.strip().splitlines()[-1])
    assert rec["params"] == ["c"] and rec["status"] == "ok" and rec["query_id"].startswith("cs-")


def _swallow(fn, *a, **k):
    try:
        fn(*a, **k)
    except ClickSenseError as exc:
        return exc
