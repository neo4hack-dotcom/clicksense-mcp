"""Governed metrics: compile (metrics, dimensions, filters, period, grain) into one SQL query.

The model never writes the metric formula: it is taken from metrics.yaml, so "revenue" means
the same thing in every answer. Filter values are resolved against stored values first
(`France` -> 'FR') and bound as typed parameters; an ambiguous value stops compilation with
the candidates instead of guessing. The generated SQL then goes through the same guard and
executor as hand-written SQL.

What a compiled query looks like (revenue by channel, France, last month):

    SELECT o.`channel` AS `channel`, sum(o.amount_eur) AS `revenue`
    FROM `shop`.`orders` AS o
    WHERE (o.status = 'completed')                                    <- metric filters
      AND o.`order_date` >= {p_from:Date} AND o.`order_date` < {p_to:Date}   <- period
      AND o.`country` IN {f0:Array(String)}                           <- filters, bound
    GROUP BY `channel` ORDER BY `revenue` DESC LIMIT 100               params: f0 = ['FR']

HOW TO EXTEND
  * A new metric, dimension or join: edit metrics.yaml / the dictionary, no code
    (`clicksense-admin kb new metric NAME` scaffolds it).
  * A new period name ("last_fiscal_year"...): add it to the `table` dict in resolve_period().
  * A new time grain: add a ClickHouse expression to GRAINS ({c} is the time column).
  * A new filter operator: add it to OPS (left: what callers write, right: the SQL operator).
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from .catalog import Catalog, Column, Table
from .errors import ClickSenseError
from .knowledge import Knowledge, fold
from .resolver import Resolver

# Time buckets for `grain`: ClickHouse expression applied to the time column ({c}).
GRAINS = {
    "hour": "toStartOfHour({c})", "day": "toDate({c})", "week": "toMonday({c})",
    "month": "toStartOfMonth({c})", "quarter": "toStartOfQuarter({c})", "year": "toStartOfYear({c})",
}
# Filter operators accepted in query_metric filters ({"amount_eur": {">=": 100}}) -> SQL.
OPS = {"=": "=", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<=", "in": "IN", "not_in": "NOT IN"}
# A dimension name usable as-is as an output column alias.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def resolve_period(period: str | dict[str, str] | None, today: dt.date | None = None) -> tuple[dt.date, dt.date] | None:
    """A period -> [start, end) dates (end exclusive), or None when no period is given.

    Accepted: today, yesterday, this_week, last_week (weeks start on Monday), this_month/mtd,
    last_month, this_quarter/qtd, last_quarter, this_year/ytd, last_year, last_N_days,
    last_N_weeks, last_N_months (the current day/month included), or an explicit
    {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"} where `to` is INCLUSIVE (people say "to March 31").
    `today` is the server's local date; the resolved range is returned to the agent, so the
    answer can state it explicitly."""
    if not period:
        return None
    today = today or dt.date.today()
    if isinstance(period, dict):
        try:
            start = dt.date.fromisoformat(str(period["from"])[:10])
            end = dt.date.fromisoformat(str(period.get("to") or today.isoformat())[:10]) + dt.timedelta(days=1)
        except (KeyError, ValueError):
            raise ClickSenseError("bad_period", f"Invalid period {period}.",
                                  'Use {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"} or a name like last_30_days.')
        return start, end
    p = period.strip().lower()
    m = re.fullmatch(r"last_(\d+)_(day|week|month)s?", p)
    tomorrow = today + dt.timedelta(days=1)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "day":
            return today - dt.timedelta(days=n - 1), tomorrow
        if unit == "week":
            return today - dt.timedelta(weeks=n) + dt.timedelta(days=1), tomorrow
        return _add_months(today.replace(day=1), -n + 1), tomorrow
    month0 = today.replace(day=1)
    q0 = today.replace(month=(today.month - 1) // 3 * 3 + 1, day=1)
    y0 = today.replace(month=1, day=1)
    table = {
        "today": (today, tomorrow), "yesterday": (today - dt.timedelta(days=1), today),
        "this_week": (today - dt.timedelta(days=today.weekday()), tomorrow),
        "last_week": (today - dt.timedelta(days=today.weekday() + 7), today - dt.timedelta(days=today.weekday())),
        "this_month": (month0, tomorrow), "mtd": (month0, tomorrow),
        "last_month": (_add_months(month0, -1), month0),
        "this_quarter": (q0, tomorrow), "qtd": (q0, tomorrow), "last_quarter": (_add_months(q0, -3), q0),
        "this_year": (y0, tomorrow), "ytd": (y0, tomorrow), "last_year": (y0.replace(year=y0.year - 1), y0),
    }
    if p in table:
        return table[p]
    raise ClickSenseError("bad_period", f"Unknown period '{period}'.",
                          "Use today, yesterday, this_week, last_week, this_month/mtd, last_month, this_quarter, "
                          "last_quarter, this_year/ytd, last_year, last_N_days|weeks|months, or {from, to}.")


def _add_months(d: dt.date, n: int) -> dt.date:
    """First-of-month arithmetic (d is always a 1st here, so no day overflow)."""
    y, m = divmod(d.month - 1 + n, 12)
    return d.replace(year=d.year + y, month=m + 1)


@dataclass
class Dimension:
    """Something a metric can be grouped or filtered by: a column of the metric's table, or a
    dictionary `dimensions:` entry (an SQL expression, possibly on a joined table). `column` points
    at the stored values used to resolve filter words; `join` names the join to add."""

    name: str
    sql: str
    join: str | None
    table: Table | None = None
    column: Column | None = None
    description: str = ""


@dataclass
class Compiled:
    """The compiled query and what query_metric reports back: params, the metric definitions used,
    the resolved period and every user-word -> stored-value resolution."""

    sql: str
    params: dict[str, Any]
    metrics: list[dict[str, Any]]
    dimensions: list[str]
    period: dict[str, str] | None
    resolutions: list[dict[str, Any]] = field(default_factory=list)


class MetricCompiler:
    """Turns query_metric arguments into one parameterised SQL statement."""

    def __init__(self, knowledge: Knowledge, catalog: Catalog, resolver: Resolver, default_db: str):
        self.k = knowledge
        self.catalog = catalog
        self.resolver = resolver
        self.default_db = default_db

    def get(self, name: str) -> dict[str, Any]:
        """A metric by name, label or synonym (case/accent-insensitive)."""
        self.k.maybe_reload()
        if name in self.k.metrics:
            return self.k.metrics[name]
        f = fold(name)
        for m in self.k.metrics.values():
            if f in [fold(m["name"]), fold(m.get("label", ""))] + [fold(s) for s in m.get("synonyms", [])]:
                return m
        raise ClickSenseError("unknown_metric", f"Unknown metric '{name}'.",
                              f"Available: {', '.join(sorted(self.k.metrics)) or 'none defined'}.")

    def dimensions_for(self, table: Table) -> dict[str, Dimension]:
        """Every dimension of a table: its columns, then its declared dimensions (which win)."""
        alias = table.spec.get("alias", "t")
        dims: dict[str, Dimension] = {}
        for c in table.columns.values():
            dims[c.name] = Dimension(c.name, f"{alias}.`{c.name}`", None, table, c, c.description)
        for name, d in (table.spec.get("dimensions") or {}).items():
            d = d or {}
            tbl, col = table, None
            if d.get("column"):
                db, tname, cname = d["column"].rsplit(".", 2) if d["column"].count(".") == 2 else \
                    (table.database, table.name, d["column"])
                try:
                    tbl = self.catalog.resolve(f"{db}.{tname}", self.default_db)
                    col = tbl.columns.get(cname)
                except ClickSenseError:
                    tbl, col = None, None
            dims[name] = Dimension(name, d["sql"], d.get("join"), tbl, col, d.get("description", ""))
        return dims

    def compile(self, metrics: list[str], *, dimensions: list[str] | None = None,
                filters: dict[str, Any] | None = None, period: str | dict[str, str] | None = None,
                grain: str | None = None, order_by: str | None = None, descending: bool = True,
                limit: int = 100) -> Compiled:
        """Compile. Rules: metrics in one call share a table and built-in filters; dimensions must
        exist (and be allowed by every metric's `dimensions:` list, columns always allowed); filter
        words are resolved or the call stops with needs_clarification; values are always bound."""
        if not metrics:
            raise ClickSenseError("no_metric", "At least one metric is required.", "See list_metrics.")
        specs = [self.get(m) for m in metrics]
        tnames = {s["table"] for s in specs}
        if len(tnames) > 1:
            raise ClickSenseError("mixed_tables", "Metrics from different tables cannot be combined in one call.",
                                  "Call query_metric once per table and combine the answers.")
        fsets = {tuple(sorted(s.get("filters") or [])) for s in specs}
        if len(fsets) > 1:
            raise ClickSenseError("mixed_filters", "These metrics have different built-in filters.",
                                  "Query them in separate calls.")
        table = self.catalog.resolve(specs[0]["table"], self.default_db)
        alias = table.spec.get("alias", "t")
        dims = self.dimensions_for(table)
        allowed = None
        for s in specs:
            if s.get("dimensions"):
                allowed = set(s["dimensions"]) if allowed is None else allowed & set(s["dimensions"])
        params: dict[str, Any] = {}
        joins: list[str] = []
        select: list[str] = []
        group: list[str] = []
        where: list[str] = [f"({f})" for f in (specs[0].get("filters") or [])]
        resolutions: list[dict[str, Any]] = []

        def need_join(name: str | None) -> None:
            """Add a declared join once (in first-use order)."""
            if not name or name in joins:
                return
            if name not in (table.spec.get("joins") or {}):
                raise ClickSenseError("bad_dictionary", f"Join '{name}' is not declared for {table.fqn}.")
            joins.append(name)

        def dim(name: str) -> Dimension:
            """A dimension by name, checked against the metrics' allowed dimensions; unknown names get close
            matches.
            """
            if name not in dims:
                from rapidfuzz import process

                close = [c[0] for c in process.extract(name, list(dims), limit=4, score_cutoff=55)]
                raise ClickSenseError("unknown_dimension", f"Unknown dimension '{name}' for {table.fqn}.",
                                      f"Did you mean: {', '.join(close)}" if close else
                                      f"Available: {', '.join(sorted(allowed or dims))}", candidates=close)
            if allowed is not None and name not in allowed and name not in table.columns:
                raise ClickSenseError("dimension_not_allowed",
                                      f"Dimension '{name}' is not allowed for {', '.join(metrics)}.",
                                      f"Allowed: {', '.join(sorted(allowed))}")
            return dims[name]

        time_col = specs[0].get("time_column") or table.time_column
        period_out = None
        if grain:
            if grain not in GRAINS:
                raise ClickSenseError("bad_grain", f"Unknown grain '{grain}'.", f"Use one of {', '.join(GRAINS)}.")
            if not time_col:
                raise ClickSenseError("no_time_column", f"{table.fqn} has no time column for a grain.")
            select.append(f"{GRAINS[grain].format(c=f'{alias}.`{time_col}`')} AS period")
            group.append("period")
        rng = resolve_period(period)
        if rng:
            if not time_col:
                raise ClickSenseError("no_time_column", f"{table.fqn} has no time column to filter a period on.")
            where.append(f"{alias}.`{time_col}` >= {{p_from:Date}} AND {alias}.`{time_col}` < {{p_to:Date}}")
            params.update(p_from=rng[0].isoformat(), p_to=rng[1].isoformat())
            period_out = {"from": rng[0].isoformat(), "to_exclusive": rng[1].isoformat(), "column": time_col}
        for name in dimensions or []:
            d = dim(name)
            need_join(d.join)
            out_name = name if _IDENT.match(name) else f"dim{len(group)}"
            select.append(f"{d.sql} AS `{out_name}`")
            group.append(f"`{out_name}`")
        for i, (name, value) in enumerate((filters or {}).items()):
            d = dim(name)
            need_join(d.join)
            where.append(self._filter(d, value, f"f{i}", params, resolutions))
        for s in specs:
            select.append(f"{s['expression']} AS `{s['name']}`")
        sql = f"SELECT {', '.join(select)}\nFROM `{table.database}`.`{table.name}` AS {alias}"
        for j in joins:
            spec = table.spec["joins"][j]
            jt = self.catalog.resolve(spec["table"], self.default_db)
            sql += f"\n{spec.get('type', 'LEFT').upper()} JOIN `{jt.database}`.`{jt.name}` AS {spec.get('alias', j)} " \
                   f"ON {spec['on']}"
        if where:
            sql += "\nWHERE " + "\n  AND ".join(where)
        if group:
            sql += "\nGROUP BY " + ", ".join(group)
        if order_by:
            key = f"`{order_by}`" if order_by in metrics or order_by in (dimensions or []) else None
            if order_by == "period" and grain:
                key = "period"
            if key is None:
                raise ClickSenseError("bad_order_by", f"Cannot order by '{order_by}'.",
                                      "Order by a requested metric, dimension, or 'period'.")
            sql += f"\nORDER BY {key} {'DESC' if descending else 'ASC'}"
        elif grain:
            sql += "\nORDER BY period ASC" + (", " + f"`{specs[0]['name']}` DESC" if dimensions else "")
        elif group:
            sql += f"\nORDER BY `{specs[0]['name']}` DESC"
        sql += f"\nLIMIT {int(limit)}"
        return Compiled(sql, params, [{k: s[k] for k in ("name", "label", "expression", "unit", "description")
                                       if k in s} for s in specs],
                        list(dimensions or []), period_out, resolutions)

    def _filter(self, d: Dimension, value: Any, pname: str, params: dict[str, Any],
                resolutions: list[dict[str, Any]]) -> str:
        """One WHERE condition for a filter: resolve words to stored values for text columns (raise
        needs_clarification on ambiguity, record each resolution), then bind as {fN:Type}."""
        op = "IN"
        if isinstance(value, dict):
            if len(value) != 1:
                raise ClickSenseError("bad_filter", f"Filter on {d.name}: one operator per dimension.",
                                      'e.g. {"amount_eur": {">=": 100}} or {"country": ["FR", "BE"]}')
            raw_op, value = next(iter(value.items()))
            if raw_op not in OPS:
                raise ClickSenseError("bad_filter", f"Unknown operator '{raw_op}'.", f"Use one of {', '.join(OPS)}.")
            op = OPS[raw_op]
        values = value if isinstance(value, list) else [value]
        col = d.column
        if col is not None and col.is_text and d.table is not None:
            resolved = []
            for r in self.resolver.resolve(d.table, col, values):
                if r["status"] in ("exact", "resolved"):
                    resolved.append(r["value"])
                    if r["status"] == "resolved":
                        resolutions.append({"dimension": d.name, "from": r["term"], "to": r["value"],
                                            "match": r["candidates"][0]["match"]})
                else:
                    raise ClickSenseError(
                        "needs_clarification",
                        f"'{r['term']}' is {'ambiguous' if r['status'] == 'ambiguous' else 'not a known value'} "
                        f"for {d.name}.",
                        "Ask the user which value they mean (ask_user_to_clarify), then retry with it.",
                        dimension=d.name, term=r["term"], candidates=r["candidates"][:6])
            values = resolved
        ch_type = _param_type(col)
        if op in ("IN", "NOT IN"):
            params[pname] = values
            return f"{d.sql} {op} {{{pname}:Array({ch_type})}}"
        if len(values) != 1:
            raise ClickSenseError("bad_filter", f"Operator {op} takes a single value.")
        params[pname] = values[0]
        return f"{d.sql} {op} {{{pname}:{ch_type}}}"


def _param_type(col: Column | None) -> str:
    """The ClickHouse type of a {param:Type} placeholder for a column (Enum -> String,
    Decimal -> Float64, LowCardinality/Nullable stripped)."""
    if col is None:
        return "String"
    bt = col.base_type
    if bt.startswith("Enum"):
        return "String"
    if bt.startswith("Decimal"):
        return "Float64"
    if bt.startswith("DateTime"):
        return "DateTime"
    if bt.startswith(("UInt", "Int", "Float", "Date", "String", "Bool", "UUID")):
        return bt.split("(")[0]
    return "String"
