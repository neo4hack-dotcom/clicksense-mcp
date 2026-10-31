"""The guard, attacked on purpose. No database needed (server parse fallback stubbed)."""
from __future__ import annotations

import pytest

from clicksense.config import Policy
from clicksense.errors import Rejected
from clicksense.guard import Guard

PII = {("shop", "customers"): {"name", "email"}}


def guard(**policy) -> Guard:
    pol = Policy(allowed_databases=["shop"], **policy)
    return Guard(pol, "shop", pii_columns=lambda db, t: PII.get((db, t), set()), server_parse=None)


def rejected(sql: str, code: str | None = None, **policy) -> Rejected:
    with pytest.raises(Rejected) as ei:
        guard(**policy).check(sql, 100)
    if code:
        assert ei.value.code == code, (ei.value.code, ei.value.message)
    assert ei.value.message
    return ei.value


@pytest.mark.parametrize("sql", [
    "SELECT count() FROM shop.orders",
    "SELECT country, sum(amount_eur) FROM orders WHERE order_date >= {d:Date} GROUP BY country",
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    "SELECT 1 UNION ALL SELECT 2",
    "SELECT * FROM numbers(10)",
    "SELECT o.order_id FROM shop.orders AS o INNER JOIN shop.products AS p ON o.product_id = p.product_id",
    "SELECT count() FROM shop.web_events SAMPLE 0.1 WHERE event_date = today()",
    "SELECT arrayMap(x -> x * 2, [1, 2, 3])",
    "SELECT countIf(c.name != '') FROM shop.customers AS c",  # PII inside an aggregate is fine
    "SELECT * EXCEPT (name, email) FROM shop.customers",
    "SELECT formatDateTime(now(), '%Y') AS y",
    "SELECT format('{} {}', 'a', 'b') AS settings",
    "SELECT 1;",
])
def test_allowed(sql):
    r = guard().check(sql, 100)
    assert r.sql


@pytest.mark.parametrize("sql,code", [
    ("DROP TABLE shop.orders", "not_a_select"),
    ("INSERT INTO shop.orders SELECT * FROM shop.orders", "not_a_select"),
    ("ALTER TABLE shop.orders DELETE WHERE 1", "not_a_select"),
    ("TRUNCATE TABLE shop.orders", "not_a_select"),
    ("SYSTEM SHUTDOWN", "not_a_select"),
    ("OPTIMIZE TABLE shop.orders FINAL", "not_a_select"),
    ("SELECT 1; DROP TABLE shop.orders", "multiple_statements"),
    ("SELECT 1; SELECT 2", "multiple_statements"),
    ("SELECT count() FROM shop.orders SETTINGS max_threads = 64", "settings_clause"),
    ("SELECT count() FROM shop.orders SETTINGS max_execution_time=0", "settings_clause"),
    ("SELECT 1 FORMAT JSON", "format_clause"),
    ("SELECT 1 INTO OUTFILE '/tmp/x'", "into_clause"),
    ("SELECT * FROM url('http://evil/x', CSV, 'a String')", "forbidden_function"),
    ("SELECT * FROM s3('https://bucket/x.parquet')", "forbidden_function"),
    ("SELECT * FROM remote('other-host', shop.orders)", "forbidden_function"),
    ("SELECT * FROM file('/etc/passwd', LineAsString)", "forbidden_function"),
    ("SELECT * FROM mysql('h:3306', 'db', 't', 'u', 'p')", "forbidden_function"),
    ("SELECT * FROM merge('shop', '.*')", "forbidden_function"),
    ("SELECT sleep(3)", "forbidden_function"),
    ("SELECT sleepEachRow(1) FROM numbers(100)", "forbidden_function"),
    ("SELECT * FROM generateRandom('a Int8')", "forbidden_function"),
    ("SELECT * FROM system.tables", "system_table"),
    ("SELECT name FROM system.users", "system_table"),
    ("SELECT * FROM INFORMATION_SCHEMA.TABLES", "system_table"),
    ("SELECT * FROM other_db.secret", "database_not_allowed"),
    ("SELECT a FROM (SELECT 1 AS a) AS s JOIN other_db.t USING a", "database_not_allowed"),
    ("SELECT name FROM shop.customers", "pii_column"),
    ("SELECT upper(c.email) AS e FROM shop.customers AS c", "pii_column"),
    ("SELECT * FROM shop.customers", "pii_column"),
    ("SELECT * FROM shop.customers UNION ALL SELECT * FROM shop.customers", "pii_column"),
    ("", "empty"),
])
def test_rejected(sql, code):
    rejected(sql, code)


def test_comment_hiding_does_not_help():
    rejected("SELECT 1 /* harmless */; DROP TABLE shop.orders", "multiple_statements")
    # A ';' inside a string literal is data, not a separator.
    assert guard().check("SELECT ';' AS x", 10)


def test_denied_table():
    rejected("SELECT count() FROM shop.orders", "table_denied", denied_tables=["shop.orders"])


def test_pii_allowed_by_policy():
    assert guard(pii="allow").check("SELECT name FROM shop.customers", 10)


def test_cte_name_is_not_a_table():
    r = guard().check("WITH recent AS (SELECT * FROM shop.orders) SELECT count() FROM recent", 10)
    assert r.tables == [("shop", "orders")]


def test_row_cap_wraps_without_rewriting():
    sql = "SELECT country, count() AS n FROM shop.orders WHERE status = {s:String} GROUP BY country -- trailing"
    r = guard().check(sql, 50)
    assert r.row_cap == 50 and r.fetch_limit == 51
    assert r.sql.startswith("SELECT * FROM (\n") and r.sql.endswith(") LIMIT 51")
    assert sql in r.sql  # the user's text, verbatim (params and comment included)


def test_small_user_limit_runs_as_is():
    r = guard().check("SELECT order_id FROM shop.orders ORDER BY order_id LIMIT 10", 50)
    assert r.row_cap is None and r.sql.endswith("LIMIT 10")


def test_large_user_limit_is_capped():
    r = guard().check("SELECT order_id FROM shop.orders LIMIT 1000000", 50)
    assert r.row_cap == 50


def test_limit_by_is_not_mistaken_for_limit():
    r = guard().check("SELECT country, order_id FROM shop.orders LIMIT 2 BY country", 50)
    assert r.row_cap == 50


def test_too_long():
    with pytest.raises(Rejected) as ei:
        Guard(Policy(), "shop", max_sql_chars=50).check("SELECT " + "1+" * 100 + "1", 10)
    assert ei.value.code == "too_long"


def test_server_parse_fallback():
    """sqlglot cannot parse `LIMIT n BY x LIMIT m`; ClickHouse's AST decides instead."""
    ast = ("SelectWithUnionQuery (children 1)\n ExpressionList (children 1)\n  SelectQuery (children 3)\n"
           "   TablesInSelectQuery (children 1)\n    TablesInSelectQueryElement (children 1)\n"
           "     TableExpression (children 1)\n      TableIdentifier shop.orders\n")
    g = Guard(Policy(allowed_databases=["shop"]), "shop", server_parse=lambda sql: ast)
    r = g.check("SELECT country FROM shop.orders ORDER BY country LIMIT 1 BY country LIMIT 5", 50)
    assert r.parser == "clickhouse" and r.tables == [("shop", "orders")] and r.row_cap == 50

    bad = ast.replace("TableIdentifier shop.orders", "TableIdentifier system.users")
    g = Guard(Policy(), "shop", server_parse=lambda sql: bad)
    with pytest.raises(Rejected) as ei:
        g.check("SELECT name FROM system.users ORDER BY name LIMIT 1 BY name LIMIT 5", 50)
    assert ei.value.code == "system_table"

    fn = ast.replace("TableIdentifier shop.orders", "Function url (children 1)")
    g = Guard(Policy(), "shop", server_parse=lambda sql: fn)
    with pytest.raises(Rejected):
        g.check("SELECT * FROM url('x', CSV, 'a String') ORDER BY a LIMIT 1 BY a LIMIT 5", 50)


def test_fallback_disabled_refuses():
    g = Guard(Policy(server_parse_fallback=False), "shop", server_parse=lambda sql: "SelectWithUnionQuery")
    with pytest.raises(Rejected) as ei:
        g.check("SELECT a FROM shop.orders ORDER BY a LIMIT 1 BY a LIMIT 5", 10)
    assert ei.value.code == "parse_error"
