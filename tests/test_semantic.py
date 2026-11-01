"""Semantic layer, resolver, metric compiler and periods — offline (no ClickHouse)."""
from __future__ import annotations

import datetime as dt
import time

import pytest

from clicksense.catalog import Catalog, Column, Table
from clicksense.errors import ClickSenseError
from clicksense.knowledge import Knowledge, fold, norm, tokens
from clicksense.metrics import MetricCompiler, resolve_period
from clicksense.resolver import Resolver, ValueSet

from .conftest import ROOT


class NoDB:
    """Any attempt to reach ClickHouse fails the test: these paths must be served from caches."""

    def run(self, *a, **k):
        raise AssertionError("unexpected query")

    estimate = explain = run


@pytest.fixture(scope="module")
def knowledge():
    return Knowledge(ROOT / "semantic")


@pytest.fixture
def world(knowledge):
    cat = Catalog(NoDB(), knowledge, ttl_s=1e9, allowed=["shop"], denied=[])
    specs = knowledge.tables

    def table(name, cols, **kw):
        t = Table("shop", name, "MergeTree", spec=specs.get(f"shop.{name}", {}), **kw)
        for cname, ctype in cols.items():
            t.columns[cname] = Column(cname, ctype, spec=(t.spec.get("columns") or {}).get(cname, {}))
        cat.tables[t.fqn] = t
        return t

    orders = table("orders", {"order_id": "UInt64", "order_date": "Date", "customer_id": "UInt32",
                              "product_id": "UInt32", "quantity": "UInt16", "amount_eur": "Decimal(12, 2)",
                              "status": "LowCardinality(String)", "channel": "LowCardinality(String)",
                              "country": "LowCardinality(String)"},
                   total_rows=3_000_000, partition_key="toYYYYMM(order_date)", sorting_key="order_date, customer_id")
    customers = table("customers", {"customer_id": "UInt32", "name": "String", "segment": "LowCardinality(String)",
                                    "city": "LowCardinality(String)", "country": "LowCardinality(String)"},
                      total_rows=200_000)
    table("products", {"product_id": "UInt32", "category": "LowCardinality(String)", "brand": "LowCardinality(String)"},
          total_rows=5000)
    cat.loaded_at = time.monotonic()
    res = Resolver(NoDB(), cat, ttl_s=1e9, max_distinct=2000, scan_max_rows=10**8)
    now = time.monotonic()
    res.cache["shop.orders.country"] = ValueSet([("FR", 900), ("BE", 300), ("CA", 80)], True, "full", now)
    res.cache["shop.orders.status"] = ValueSet([("completed", 900), ("cancelled", 50), ("refunded", 20),
                                                ("pending", 10)], True, "full", now)
    res.cache["shop.orders.channel"] = ValueSet([("web", 1), ("app", 1), ("store", 1), ("marketplace", 1)],
                                                True, "full", now)
    res.cache["shop.customers.city"] = ValueSet([("Paris", 10), ("Saint-Étienne", 3), ("Saint-Denis", 2)],
                                                True, "full", now)
    res.cache["shop.customers.segment"] = ValueSet([("B2C", 9), ("B2B", 3), ("VIP", 1)], True, "full", now)
    return cat, res, orders, customers


# ---------------------------------------------------------------------------------------------- text
def test_fold_and_norm():
    assert fold("Montréal") == "montreal"
    assert norm("Saint-Étienne") == norm("saint etienne") == "saint etienne"
    assert "order" in tokens("The cancelled orders")


def test_knowledge_loads_everything(knowledge):
    assert not knowledge.errors
    assert "shop.orders" in knowledge.tables
    assert knowledge.tables["shop.orders"]["joins"]["customers"]["on"]  # YAML 1.1 `on:` trap
    assert {"revenue", "conversion_rate"} <= set(knowledge.metrics)
    assert "answer-data-question" in knowledge.skills
    assert "revenue-rules" in knowledge.docs


def test_search_ranks_definitions(knowledge):
    top = knowledge.search("average basket", limit=3)
    assert top[0][1].id in ("average_order_value", "average order value")
    assert knowledge.search("why did revenue drop", {"skill"}, 1)[0][1].id == "investigate-metric-change"


def test_glossary_flags_ambiguous_terms(knowledge):
    terms = knowledge.find_terms("What were sales in CA last month?")
    senses = {t["term"]: len(t.get("senses", [])) for t in terms}
    assert senses["sales"] == 3 and senses["CA"] == 2


# ---------------------------------------------------------------------------------------------- resolver
def by_term(results):
    return {r["term"]: r for r in results}


def test_resolver_statuses(world):
    cat, res, orders, customers = world
    r = by_term(res.resolve(orders, orders.columns["country"], ["FR", "France", "fr", "Belgium", "Narnia"],
                            allow_query=False))
    assert r["FR"]["status"] == "exact"
    assert r["France"]["status"] == "resolved" and r["France"]["value"] == "FR"
    assert r["fr"]["value"] == "FR"
    assert r["Belgium"]["value"] == "BE"  # dictionary value synonym
    assert r["Narnia"]["status"] == "not_found" and "complete" in r["Narnia"]["hint"]


def test_resolver_user_wording(world):
    cat, res, orders, _ = world
    terms = ["canceled", "refunds", "delivered", "annulées"]  # synonyms may be multilingual
    r = by_term(res.resolve(orders, orders.columns["status"], terms, allow_query=False))
    assert [r[t]["value"] for t in terms] == ["cancelled", "refunded", "completed", "cancelled"]


def test_resolver_punctuation_and_ambiguity(world):
    cat, res, _, customers = world
    r = by_term(res.resolve(customers, customers.columns["city"], ["saint etienne", "Saint"], allow_query=False))
    assert r["saint etienne"]["value"] == "Saint-Étienne"
    assert r["Saint"]["status"] == "ambiguous"
    assert {c["value"] for c in r["Saint"]["candidates"]} >= {"Saint-Étienne", "Saint-Denis"}


# ---------------------------------------------------------------------------------------------- metrics
def test_compile_resolves_values_and_joins(world, knowledge):
    cat, res, _, _ = world
    mc = MetricCompiler(knowledge, cat, res, "shop")
    c = mc.compile(["turnover"], dimensions=["category"], filters={"country": "France", "segment": "business"},
                   period={"from": "2026-01-01", "to": "2026-03-31"})
    assert "sum(o.amount_eur)" in c.sql and "o.status = 'completed'" in c.sql
    assert "LEFT JOIN `shop`.`products` AS p" in c.sql and "LEFT JOIN `shop`.`customers` AS c" in c.sql
    assert c.params["f0"] == ["FR"] and c.params["f1"] == ["B2B"]
    assert c.params["p_from"] == "2026-01-01" and c.params["p_to"] == "2026-04-01"  # `to` is inclusive
    assert {(r["from"], r["to"]) for r in c.resolutions} == {("France", "FR"), ("business", "B2B")}
    assert "France" not in c.sql  # values are bound, never interpolated


def test_compile_refuses_to_guess(world, knowledge):
    cat, res, _, _ = world
    mc = MetricCompiler(knowledge, cat, res, "shop")
    with pytest.raises(ClickSenseError) as ei:
        mc.compile(["revenue"], filters={"city": "Saint"})
    assert ei.value.code == "needs_clarification" and len(ei.value.extra["candidates"]) >= 2


def test_compile_errors_name_alternatives(world, knowledge):
    cat, res, _, _ = world
    mc = MetricCompiler(knowledge, cat, res, "shop")
    with pytest.raises(ClickSenseError) as ei:
        mc.compile(["revenue"], dimensions=["chanel"])
    assert ei.value.code == "unknown_dimension" and "channel" in ei.value.extra["candidates"]
    with pytest.raises(ClickSenseError) as ei:
        mc.compile(["revenue", "sessions"])
    assert ei.value.code == "mixed_tables"
    with pytest.raises(ClickSenseError) as ei:
        mc.compile(["marge"])
    assert ei.value.code == "unknown_metric"


def test_compile_operators(world, knowledge):
    cat, res, _, _ = world
    mc = MetricCompiler(knowledge, cat, res, "shop")
    c = mc.compile(["orders"], filters={"amount_eur": {">=": 100}, "channel": {"not_in": ["physical store"]}}, grain="month")
    assert "o.`amount_eur` >= {f0:Float64}" in c.sql and c.params["f0"] == 100
    assert "NOT IN {f1:Array(String)}" in c.sql and c.params["f1"] == ["store"]
    assert "toStartOfMonth(o.`order_date`) AS period" in c.sql and "ORDER BY period ASC" in c.sql


# ---------------------------------------------------------------------------------------------- periods
@pytest.mark.parametrize("name,start,end", [
    ("today", "2026-10-31", "2026-11-01"),
    ("yesterday", "2026-10-30", "2026-10-31"),
    ("last_7_days", "2026-10-25", "2026-11-01"),
    ("this_month", "2026-10-01", "2026-11-01"),
    ("last_month", "2026-09-01", "2026-10-01"),
    ("last_quarter", "2026-07-01", "2026-10-01"),
    ("ytd", "2026-01-01", "2026-11-01"),
    ("last_year", "2025-01-01", "2026-01-01"),
    ("last_3_months", "2026-08-01", "2026-11-01"),
])
def test_periods(name, start, end):
    s, e = resolve_period(name, dt.date(2026, 10, 31))
    assert (s.isoformat(), e.isoformat()) == (start, end)


def test_bad_period():
    with pytest.raises(ClickSenseError):
        resolve_period("when pigs fly")


def test_metrics_named_verbatim_come_first(knowledge):
    # "orders" names a metric word-for-word; "basket size" is a synonym of average_order_value.
    assert knowledge.find_metrics("how many orders in Belgium last month")[0] == "orders"
    assert knowledge.find_metrics("average basket by segment")[0] == "average_order_value"
    assert knowledge.find_metrics("what were sales in France") == []  # ambiguous: left to the glossary
