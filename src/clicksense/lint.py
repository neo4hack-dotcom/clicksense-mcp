"""Query advisor: findings a ClickHouse expert would raise before running a query.

Three families, each finding has a `severity` (error | warning | info) and a fix:

* correctness — unknown columns (with "did you mean"), filter literals that do not exist in
  the column (`country = 'France'` vs stored 'FR'), PII;
* performance — no partition/time filter on a big partitioned table, primary-key prefix not
  used, SELECT *, FINAL, count(DISTINCT), big right side of a JOIN, leading-wildcard LIKE;
* semantics — known caveats attached to the tables in the dictionary.

Value checks use only cached/dictionary value lists unless `check_values` asks for a live
lookup, so linting stays cheap.
"""
from __future__ import annotations

from typing import Any

from sqlglot import exp

from .catalog import Catalog, Table
from .errors import ClickSenseError
from .guard import GuardResult
from .resolver import Resolver

BIG_TABLE_ROWS = 10_000_000


def _f(severity: str, code: str, message: str, fix: str | None = None, **extra: Any) -> dict[str, Any]:
    out = {"severity": severity, "code": code, "message": message}
    if fix:
        out["fix"] = fix
    out.update(extra)
    return out


class Advisor:
    def __init__(self, catalog: Catalog, resolver: Resolver):
        self.catalog = catalog
        self.resolver = resolver

    def review(self, g: GuardResult, *, check_values: bool = True, live_values: bool = False) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = [_f("info", "parser", w) for w in g.warnings]
        tables: dict[str, Table] = {}
        for db, name in g.tables:
            t = self.catalog.tables.get(f"{db}.{name}")
            if t is None:
                try:
                    t = self.catalog.resolve(f"{db}.{name}", db)
                except ClickSenseError as exc:
                    findings.append(_f("error", "unknown_table", exc.message, exc.hint))
                    continue
            tables[name.lower()] = t
        for t in tables.values():
            for c in t.spec.get("caveats", []) or []:
                findings.append(_f("info", "caveat", f"{t.fqn}: {c}"))
        if g.tree is None or not tables:
            return findings
        tree = g.tree
        alias_map = self._aliases(tree, tables)
        findings += self._columns(tree, tables, alias_map)
        findings += self._performance(tree, tables, alias_map)
        if check_values:
            findings += self._values(tree, tables, alias_map, live_values)
        return findings

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _aliases(tree: exp.Expression, tables: dict[str, Table]) -> dict[str, Table]:
        amap: dict[str, Table] = {}
        for t in tree.find_all(exp.Table):
            tbl = tables.get(t.name.lower()) if t.name else None
            if tbl:
                amap[t.name.lower()] = tbl
                if t.alias:
                    amap[t.alias.lower()] = tbl
        return amap

    @staticmethod
    def _owner(col: exp.Column, amap: dict[str, Table], tables: dict[str, Table]) -> Table | None:
        if col.table:
            return amap.get(col.table.lower())
        owners = [t for t in tables.values() if col.name in t.columns]
        return owners[0] if len(owners) == 1 else None

    def _columns(self, tree, tables, amap) -> list[dict[str, Any]]:
        # Only when every source is a real table: columns of subqueries/CTEs are not knowable here.
        sources = list(tree.find_all(exp.Table))
        if any(not t.name or t.name.lower() not in tables for t in sources) or tree.find(exp.Subquery):
            return []
        aliases = {a.alias.lower() for a in tree.find_all(exp.Alias)}
        lambda_args = {i.name.lower() for lam in tree.find_all(exp.Lambda) for i in lam.expressions}
        arrays = {a.alias.lower() for j in tree.find_all(exp.Unnest) for a in j.find_all(exp.TableAlias)}
        out, seen = [], set()
        for col in tree.find_all(exp.Column):
            name = col.name
            if not name or isinstance(col.this, exp.Star):
                continue
            low = name.lower()
            if low in aliases or low in lambda_args or low in arrays or (col.table and col.table.lower() not in amap):
                continue
            pool = [amap[col.table.lower()]] if col.table else list(tables.values())
            if any(name in t.columns for t in pool) or low in seen:
                continue
            seen.add(low)
            cands: list[str] = []
            for t in pool:
                try:
                    self.catalog.column(t, name)
                except ClickSenseError as exc:
                    cands += exc.extra.get("candidates", [])
            msg = f"Column '{name}' does not exist in {', '.join(t.fqn for t in pool)}."
            exact_case = [c.name for t in pool for c in t.columns.values() if c.name.lower() == low]
            if exact_case:
                msg += " Identifiers are case-sensitive."
                cands = exact_case + cands
            out.append(_f("error", "unknown_column", msg,
                          f"Did you mean: {', '.join(dict.fromkeys(cands))}" if cands else "Use describe_table.",
                          column=name))
        if len(tables) > 1:
            for col in tree.find_all(exp.Column):
                if col.table or not col.name or col.name.lower() in aliases:
                    continue
                owners = [t.fqn for t in tables.values() if col.name in t.columns]
                if len(owners) > 1 and col.name.lower() not in seen:
                    seen.add(col.name.lower())
                    out.append(_f("warning", "ambiguous_column",
                                  f"Column '{col.name}' exists in {', '.join(owners)}.",
                                  "Qualify it with the table alias (e.g. o.customer_id)."))
        return out

    def _filters(self, tree: exp.Expression) -> list[exp.Expression]:
        conds = []
        for sel in tree.find_all(exp.Select):
            for key in ("where", "prewhere"):
                node = sel.args.get(key)
                if node is not None:
                    conds.append(node)
            for j in sel.args.get("joins") or []:
                if j.args.get("on") is not None:
                    conds.append(j.args["on"])
        return conds

    def _performance(self, tree, tables, amap) -> list[dict[str, Any]]:
        out = []
        filtered: set[tuple[str, str]] = set()
        for cond in self._filters(tree):
            for col in cond.find_all(exp.Column):
                t = self._owner(col, amap, tables)
                if t:
                    filtered.add((t.fqn, col.name))
        for t in tables.values():
            big = (t.total_rows or 0) >= BIG_TABLE_ROWS
            tc = t.time_column
            if t.partition_key and tc and (t.fqn, tc) not in filtered and big:
                out.append(_f("warning", "no_partition_filter",
                              f"{t.fqn} ({t.total_rows:,} rows) is partitioned by {t.partition_key} but the query "
                              f"does not filter on {tc}: every partition will be read.",
                              f"Add a range on {tc}, e.g. WHERE {tc} >= today() - 30."))
            first_key = t.sorting_key.split(",")[0].strip() if t.sorting_key else ""
            if big and first_key and first_key in t.columns and (t.fqn, first_key) not in filtered \
                    and first_key != tc:
                out.append(_f("info", "primary_key_unused",
                              f"No filter on {first_key}, the first ORDER BY key of {t.fqn}: the primary index "
                              "cannot skip granules.", f"Filter on {first_key} when the question allows it."))
        for sel in tree.find_all(exp.Select):
            if any(isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))
                   for p in sel.expressions):
                out.append(_f("warning", "select_star", "SELECT * reads every column of a column store.",
                              "Select only the columns you need."))
                break
        if any(t.args.get("final") for t in tree.find_all(exp.Table)) or " FINAL" in tree.sql(dialect="clickhouse").upper():
            out.append(_f("info", "final", "FINAL merges parts at read time and is expensive on large tables.",
                          "Use it only when deduplication is required (ReplacingMergeTree)."))
        for cnt in tree.find_all(exp.Count):
            if isinstance(cnt.this, exp.Distinct):
                out.append(_f("info", "count_distinct", "count(DISTINCT x) is exact and memory-hungry.",
                              "Use uniq(x) (approximate, fast) or uniqExact(x) when exactness is required."))
                break
        for like in tree.find_all(exp.Like, exp.ILike):
            pat = like.expression
            if isinstance(pat, exp.Literal) and pat.is_string and str(pat.this).startswith("%"):
                out.append(_f("info", "leading_wildcard", f"LIKE '{pat.this}' cannot use any index.",
                              "Combine it with a selective filter on the primary/partition key."))
                break
        for sel in tree.find_all(exp.Select):
            joins = sel.args.get("joins") or []
            frm = sel.args.get("from")
            left = frm.this if frm is not None else None
            for j in joins:
                rt = j.this
                if isinstance(left, exp.Table) and isinstance(rt, exp.Table):
                    lt_, rt_ = tables.get(left.name.lower()), tables.get(rt.name.lower())
                    if lt_ and rt_ and (rt_.total_rows or 0) > max(5 * (lt_.total_rows or 0), 1_000_000):
                        out.append(_f("warning", "join_order",
                                      f"The right side of the JOIN ({rt_.fqn}, {rt_.total_rows:,} rows) is larger "
                                      f"than the left ({lt_.fqn}). ClickHouse builds the hash table from the right "
                                      "side, in memory.",
                                      "Put the smaller table on the right, or pre-filter/aggregate it in a subquery."))
        return out

    def _values(self, tree, tables, amap, live: bool) -> list[dict[str, Any]]:
        out = []
        checks: list[tuple[exp.Column, list[str]]] = []
        for cond in self._filters(tree):
            for node in cond.find_all(exp.EQ, exp.NEQ, exp.In):
                if isinstance(node, exp.In):
                    col, lits = node.this, node.expressions
                else:
                    col, other = node.this, node.expression
                    if isinstance(other, exp.Column) and isinstance(col, exp.Literal):
                        col, other = other, col
                    lits = [other]
                if not isinstance(col, exp.Column):
                    continue
                vals = [str(lit.this) for lit in lits if isinstance(lit, exp.Literal) and lit.is_string]
                if vals:
                    checks.append((col, vals))
        for col, vals in checks:
            t = self._owner(col, amap, tables)
            c = t.columns.get(col.name) if t else None
            if not t or not c or not c.is_text:
                continue
            known = self.resolver.known_values(t, c)
            if known is None and not live:
                continue
            missing = [v for v in vals if known is None or v not in known]
            if not missing:
                continue
            for r in self.resolver.resolve(t, c, missing, allow_query=live):
                if r["status"] == "exact":
                    continue
                sugg = [cand["value"] for cand in r["candidates"][:4]]
                out.append(_f("error" if r["status"] in ("resolved", "not_found") else "warning", "unknown_value",
                              f"'{r['term']}' does not occur in {t.fqn}.{c.name}.",
                              (f"Use '{r['value']}'." if r.get("value") is not None else
                               (f"Did you mean one of: {', '.join(map(repr, sugg))}? Ask the user if unsure."
                                if sugg else "Check the value with resolve_values.")),
                              column=f"{t.fqn}.{c.name}", status=r["status"], candidates=r["candidates"][:4]))
        return out
