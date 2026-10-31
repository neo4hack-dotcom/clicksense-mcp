"""Tool logic, independent of MCP. server.py is a thin protocol layer over this class.

Keeping the logic here makes every tool testable as a plain method call, and keeps the
protocol concerns (annotations, isError, elicitation) in one place.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process

from .audit import Audit
from .catalog import Catalog, Table
from .config import Settings
from .errors import ClickSenseError, Rejected
from .executor import Executor, QueryOutcome
from .guard import Guard, GuardResult
from .knowledge import BM25, Doc, Knowledge, fold, norm, tokens
from .lint import Advisor
from .metrics import MetricCompiler
from .resolver import Resolver, _q

_WORD = re.compile(r"[\w'’-]+", re.UNICODE)


class ClickSense:
    def __init__(self, settings: Settings, executor: Executor | None = None):
        self.settings = settings
        self.limits = settings.limits
        self.default_db = settings.connection.database
        self.knowledge = Knowledge(settings.semantic_dir)
        self.executor = executor or Executor(settings)
        self.catalog = Catalog(self.executor, self.knowledge, ttl_s=settings.catalog_ttl_s,
                               allowed=settings.policy.allowed_databases, denied=settings.policy.denied_tables)
        self.resolver = Resolver(self.executor, self.catalog, ttl_s=settings.value_cache_ttl_s,
                                 max_distinct=settings.value_cache_max_distinct,
                                 scan_max_rows=settings.value_scan_max_rows)
        self.guard = Guard(settings.policy, self.default_db, max_sql_chars=self.limits.max_sql_chars,
                           pii_columns=self._pii, server_parse=self.executor.explain_ast)
        self.advisor = Advisor(self.catalog, self.resolver)
        self.metrics = MetricCompiler(self.knowledge, self.catalog, self.resolver, self.default_db)
        self.audit = Audit(settings.audit_path)
        self.started = time.time()
        self._cat_index: tuple[float, BM25] | None = None

    def _pii(self, db: str, table: str) -> set[str]:
        self.catalog.refresh()
        return self.catalog.pii_columns(db, table)

    def _cap(self, max_rows: int | None) -> int:
        return max(1, min(max_rows or self.limits.default_rows, self.limits.hard_max_rows))

    # ================================================================== discovery
    def list_tables(self, search: str | None = None) -> dict[str, Any]:
        tables = self.catalog.all()
        if search:
            hits = {d.id for _, d in self._catalog_index().search(search, limit=50)}
            f = fold(search)
            tables = [t for t in tables if t.fqn in hits or f in fold(t.fqn)]
        out: dict[str, Any] = {"ok": True, "tables": [t.summary() for t in tables]}
        if self.catalog.error:
            out["warning"] = f"Live catalog unavailable ({self.catalog.error}); showing the dictionary only."
        missing = sorted(set(self.knowledge.tables) - set(self.catalog.tables))
        if missing and not self.catalog.error:
            out["dictionary_only"] = missing
        return out

    def describe_table(self, table: str, include_samples: bool = False) -> dict[str, Any]:
        self.catalog.all()
        t = self.catalog.resolve(table, self.default_db)
        out = {"ok": True, **t.to_dict()}
        docs = [d for d in self.knowledge.docs.values() if t.fqn in (d.get("applies_to") or [])]
        if docs:
            out["knowledge"] = [{"id": d["id"], "title": d["title"]} for d in docs]
        metrics = [m["name"] for m in self.knowledge.metrics.values() if m.get("table") == t.fqn]
        if metrics:
            out["metrics"] = metrics
        if include_samples:
            out["sample"] = self.sample_rows(t.fqn, n=5)
        return out

    def _catalog_index(self) -> BM25:
        self.catalog.refresh()
        if self._cat_index and self._cat_index[0] == self.catalog.loaded_at:
            return self._cat_index[1]
        docs = []
        for t in self.catalog.tables.values():
            cols = " ".join(f"{c.name} {c.description} {' '.join(c.spec.get('synonyms', []))}"
                            for c in t.columns.values())
            docs.append(Doc("table", t.fqn, t.name, f"{t.description} {' '.join(t.spec.get('synonyms', []))} {cols}"))
        idx = BM25(docs)
        self._cat_index = (self.catalog.loaded_at, idx)
        return idx

    def get_context(self, question: str, limit: int = 6) -> dict[str, Any]:
        """Everything relevant to a question, in one call: the agent's starting point."""
        self.catalog.all()
        hits = self.knowledge.search(question, limit=40)
        by_kind: dict[str, list[tuple[float, Doc]]] = {}
        for s, d in hits:
            by_kind.setdefault(d.kind, []).append((s, d))

        # Table ranking: lexical match on the live schema, then strong boosts for tables behind a
        # matching metric or glossary term, so "revenue on the website" lands on orders, not web_events.
        table_scores: dict[str, float] = {}
        for s, d in self._catalog_index().search(question, limit=10):
            table_scores[d.id] = table_scores.get(d.id, 0) + s
        for s, d in by_kind.get("table", []) + by_kind.get("column", []):
            table_scores[d.payload["table"]] = table_scores.get(d.payload["table"], 0) + s
        terms = self.knowledge.find_terms(question)
        # Metrics: glossary-mapped ones first, then search hits within half of the best score.
        glossary_metrics = [t.split(":", 1)[1] for term in terms
                            for t in [term.get("maps_to")] + [x.get("maps_to") for x in term.get("senses", [])]
                            if isinstance(t, str) and t.startswith("metric:")]
        mhits = by_kind.get("metric", [])
        best = mhits[0][0] if mhits else 0
        searched = [d.payload["name"] for sc, d in mhits if sc >= best * 0.5]
        names = list(dict.fromkeys(glossary_metrics + searched))
        metrics = [self.knowledge.metrics[n] for n in names if n in self.knowledge.metrics][: max(2, min(limit, 4))]
        for rank, m in enumerate(metrics):
            boost = 15 if m["name"] in glossary_metrics else (10 if rank == 0 else 3)
            table_scores[m["table"]] = table_scores.get(m["table"], 0) + boost
        # Values named in the question pull their tables up ("on the web" -> orders.channel).
        prelim = [t for t, _ in sorted(table_scores.items(), key=lambda kv: -kv[1]) if t in self.catalog.tables][:6]
        values = self._spot_values(question, [self.catalog.tables[f] for f in prelim])
        for v in values:
            for t in {m["table"] for m in v["matches"]}:
                table_scores[t] = table_scores.get(t, 0) + 4
        top = max(table_scores.values(), default=0)
        ranked_tables = [t for t, sc in sorted(table_scores.items(), key=lambda kv: -kv[1])
                         if t in self.catalog.tables and sc >= top * 0.3][: min(3, limit)]
        values = [{**v, "matches": [m for m in v["matches"] if m["table"] in ranked_tables]} for v in values]
        values = [v for v in values if v["matches"]]

        tables_out = []
        for fqn in ranked_tables:
            t = self.catalog.tables[fqn]
            entry = t.summary()
            entry["time_column"] = t.time_column
            entry["sorting_key"] = t.sorting_key or None
            entry["columns"] = [c.to_dict(brief=True) for c in list(t.columns.values())[:60]]
            if t.spec.get("joins"):
                entry["joins"] = {k: {"table": v["table"], "on": v["on"]} for k, v in t.spec["joins"].items()}
            if t.spec.get("caveats"):
                entry["caveats"] = t.spec["caveats"]
            tables_out.append({k: v for k, v in entry.items() if v not in (None, [], {})})

        ambiguities = []
        for term in terms:
            if len(term.get("senses", [])) > 1:
                ambiguities.append({"term": term["matched"], "kind": "business_term",
                                    "senses": term["senses"],
                                    "action": "Ask the user which meaning applies unless the context settles it."})
        already = {fold(a["term"]) for a in ambiguities}
        for v in values:
            if v["term"] in already:
                continue
            if len({(m["table"], m["column"]) for m in v["matches"]}) > 1:
                ambiguities.append({"term": v["term"], "kind": "value_in_several_columns", "matches": v["matches"],
                                    "action": "Pick the column from the question's intent, or ask the user."})
        examples = [{"question": d.payload.get("question"), "sql": d.payload.get("sql"),
                     **({"notes": d.payload["notes"]} if d.payload.get("notes") else {})}
                    for _, d in by_kind.get("example", [])[: min(3, limit)]]
        knowledge = [{"id": d.id, "title": d.title, "excerpt": _excerpt(d.payload.get("body", ""), question)}
                     for _, d in by_kind.get("knowledge", [])[:3]]
        skills = [{"name": d.id, "description": d.payload.get("description")} for _, d in by_kind.get("skill", [])[:2]]
        out: dict[str, Any] = {
            "ok": True,
            "question": question,
            "tables": tables_out,
            "metrics": [{k: m[k] for k in ("name", "label", "description", "expression", "table", "dimensions",
                                           "filters") if k in m} for m in metrics],
            "glossary": [{"term": t["term"], "definition": t.get("definition"),
                          **({"maps_to": t["maps_to"]} if t.get("maps_to") else {})}
                         for t in terms if len(t.get("senses", [])) <= 1],
            "values_spotted": values,
            "ambiguities": ambiguities,
            "examples": examples,
            "knowledge": knowledge,
            "skills": skills,
            "next_steps": _next_steps(bool(metrics), bool(ambiguities)),
        }
        if self.catalog.error:
            out["warning"] = f"Live catalog unavailable ({self.catalog.error})."
        return {k: v for k, v in out.items() if v not in ([], {}, None) or k in ("ok", "tables")}

    def _spot_values(self, question: str, tables: list[Table]) -> list[dict[str, Any]]:
        """Words of the question that are stored values (or their synonyms) of low-cardinality columns."""
        words = [w.strip("'’-") for w in _WORD.findall(question)]
        # Two-letter words count only when written as a code ("CA", "FR"): "de", "le", "en"
        # are prepositions far more often than Germany, Lebanon or English.
        grams = {norm(w) for w in words if len(w) >= 3 or (len(w) == 2 and w.isupper())}
        grams |= {norm(f"{a} {b}") for a, b in zip(words, words[1:])}
        found: dict[str, list[dict[str, Any]]] = {}
        for t in tables:
            for c in t.columns.values():
                if not c.is_text or c.pii:
                    continue
                low_card = c.type.startswith("LowCardinality") or c.base_type.startswith("Enum") \
                    or c.spec.get("values") or c.spec.get("value_synonyms") or c.spec.get("enumerable")
                if not low_card:
                    continue
                try:
                    vs = self.resolver.values(t, c)
                except ClickSenseError:
                    continue
                labels = {str(k): str(v) for k, v in (c.spec.get("values") or {}).items()}
                syn = {str(k): [str(s) for s in v] for k, v in (c.spec.get("value_synonyms") or {}).items()}
                stored = {str(v) for v, _ in (vs.values if vs else [])} | set(labels) | set(syn)
                for v in stored:
                    forms = {norm(v)} | {norm(s) for s in syn.get(v, [])} | ({norm(labels[v])} if v in labels else set())
                    for g in grams & forms:
                        found.setdefault(g, []).append({"table": t.fqn, "column": c.name, "value": v,
                                                        **({"label": labels[v]} if v in labels else {})})
        return [{"term": k, "matches": v} for k, v in found.items()]

    def search_knowledge(self, query: str, kinds: list[str] | None = None, limit: int = 8) -> dict[str, Any]:
        hits = self.knowledge.search(query, set(kinds) if kinds else None, limit)
        items = []
        for score, d in hits:
            item: dict[str, Any] = {"kind": d.kind, "id": d.id, "title": d.title, "score": score}
            p = d.payload
            if d.kind == "knowledge":
                item["excerpt"] = _excerpt(p.get("body", ""), query)
            elif d.kind == "metric":
                item.update({k: p[k] for k in ("description", "expression", "table") if k in p})
            elif d.kind == "term":
                item.update({k: p[k] for k in ("definition", "senses", "maps_to") if k in p})
            elif d.kind == "example":
                item.update({"sql": p.get("sql")})
            elif d.kind == "skill":
                item["description"] = p.get("description")
            elif d.kind in ("table", "column"):
                item["text"] = d.text[:300].strip()
            items.append(item)
        return {"ok": True, "results": items}

    def get_knowledge(self, doc_id: str) -> dict[str, Any]:
        self.knowledge.maybe_reload()
        d = self.knowledge.docs.get(doc_id)
        if not d:
            raise ClickSenseError("unknown_document", f"No knowledge document '{doc_id}'.",
                                  "Use search_knowledge to find document ids.")
        return {"ok": True, **d}

    # ================================================================== skills & metrics
    def list_skills(self) -> dict[str, Any]:
        self.knowledge.maybe_reload()
        return {"ok": True, "skills": [{"name": s["name"], "description": s["description"],
                                        "when_to_use": s["when_to_use"]} for s in self.knowledge.skills.values()]}

    def get_skill(self, name: str) -> dict[str, Any]:
        self.knowledge.maybe_reload()
        s = self.knowledge.skills.get(name)
        if not s:
            close = process.extractOne(name, list(self.knowledge.skills)) if self.knowledge.skills else None
            raise ClickSenseError("unknown_skill", f"No skill '{name}'.",
                                  f"Did you mean '{close[0]}'?" if close else "Use list_skills.")
        return {"ok": True, **s}

    def list_metrics(self) -> dict[str, Any]:
        self.knowledge.maybe_reload()
        return {"ok": True, "metrics": [{k: m[k] for k in ("name", "label", "description", "table", "unit",
                                                           "dimensions", "synonyms") if k in m}
                                        for m in self.knowledge.metrics.values()]}

    def query_metric(self, metrics: list[str], dimensions: list[str] | None = None,
                     filters: dict[str, Any] | None = None, period: Any = None, grain: str | None = None,
                     order_by: str | None = None, descending: bool = True, limit: int = 100,
                     fmt: str = "json") -> dict[str, Any]:
        compiled = self.metrics.compile(metrics, dimensions=dimensions, filters=filters, period=period, grain=grain,
                                        order_by=order_by, descending=descending,
                                        limit=min(limit, self.limits.hard_max_rows))
        result = self.run_query(compiled.sql, compiled.params, max_rows=limit, fmt=fmt, tool="query_metric")
        result["metric_definitions"] = compiled.metrics
        if compiled.period:
            result["period"] = compiled.period
        if compiled.resolutions:
            result["value_resolutions"] = compiled.resolutions
        result["sql"] = compiled.sql
        result["params"] = compiled.params
        return result

    # ================================================================== querying
    def _guarded(self, sql: str, max_rows: int | None) -> GuardResult:
        self.catalog.refresh()
        return self.guard.check(sql, self._cap(max_rows))

    def _preflight(self, g: GuardResult, params: dict[str, Any] | None) -> list[dict[str, Any]] | None:
        """EXPLAIN ESTIMATE when the referenced tables are big enough to matter. None if skipped."""
        if not self.limits.preflight:
            return None
        tables = [self.catalog.tables.get(f"{db}.{t}") for db, t in g.tables]
        total = sum((t.total_rows or 0) for t in tables if t is not None and t.is_merge_tree)
        if total <= self.limits.preflight_max_rows:
            return None  # cannot exceed the budget even as a full scan: skip the round trip
        est = self.executor.estimate(g.sql, params)
        rows = sum(int(e.get("rows", 0)) for e in est)
        if rows > self.limits.preflight_max_rows:
            findings = [f for f in self.advisor.review(g, check_values=False) if f["severity"] != "info"
                        or f["code"] == "primary_key_unused"]
            raise Rejected("too_expensive",
                           f"Pre-flight estimate: the query would read {rows:,} rows "
                           f"(budget {self.limits.preflight_max_rows:,}).",
                           "Filter on the partition/time column or a primary-key prefix, aggregate, or use SAMPLE "
                           "on tables that support it. explain_query shows which indexes are used.",
                           estimate=est, findings=findings)
        return est

    def run_query(self, sql: str, params: dict[str, Any] | None = None, max_rows: int | None = None,
                  fmt: str = "json", tool: str = "run_query") -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            g = self._guarded(sql, max_rows)
            self._preflight(g, params)
            out = self.executor.run(g.sql, params, tool=tool, fetch_limit=g.fetch_limit)
        except ClickSenseError as exc:
            self.audit.log(tool, "refused" if isinstance(exc, Rejected) else "error", sql=sql, params=params,
                           error=exc.code, query_id=exc.extra.get("query_id"))
            if exc.code in ("unknown_identifier", "unknown_table", "type_mismatch", "syntax_error"):
                self._attach_findings(exc, sql, max_rows)
            raise
        result = self._render(out, fmt, g)
        notes = [f for f in self._safe_review(g, live=not out.rows) if f["severity"] in ("error", "warning")]
        if notes:
            result["notes"] = notes
        if not out.rows:
            result["empty_hint"] = ("0 rows. Before answering 'none', check the notes: a filter literal that does "
                                    "not match stored values is the usual cause.") if notes else \
                "0 rows. The filters matched nothing."
        self.audit.log(tool, "ok", sql=g.sql, params=params, query_id=out.query_id, rows=len(out.rows),
                       read_rows=out.read_rows, read_bytes=out.read_bytes, cached=out.cached,
                       ms=round((time.perf_counter() - t0) * 1000, 1))
        return result

    def _safe_review(self, g: GuardResult, live: bool) -> list[dict[str, Any]]:
        try:
            return self.advisor.review(g, check_values=True, live_values=live)
        except ClickSenseError:
            return []

    def _attach_findings(self, exc: ClickSenseError, sql: str, max_rows: int | None) -> None:
        try:
            g = self._guarded(sql, max_rows)
            f = [x for x in self.advisor.review(g, check_values=True) if x["severity"] == "error"]
            if f:
                exc.extra["findings"] = f
        except ClickSenseError:
            pass

    def _render(self, out: QueryOutcome, fmt: str, g: GuardResult) -> dict[str, Any]:
        rows, truncated, reason = out.rows, out.truncated, out.truncated_reason
        budget = self.limits.max_response_bytes
        size = len(json.dumps(out.columns))
        kept = []
        for r in rows:
            size += len(json.dumps(r, ensure_ascii=False, default=str)) + 1
            if size > budget:
                truncated, reason = True, "response_bytes"
                break
            kept.append(r)
        result: dict[str, Any] = {"ok": True, "columns": out.columns, "types": out.types, "row_count": len(kept)}
        if fmt == "markdown":
            result["table"] = _markdown(out.columns, kept)
        elif fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(out.columns)
            w.writerows(kept)
            result["csv"] = buf.getvalue()
        else:
            result["rows"] = kept
        result["truncated"] = truncated
        if truncated:
            result["truncated_reason"] = reason
            result["truncation_hint"] = ("The result is incomplete: it is NOT the full answer. Aggregate in SQL "
                                         "(GROUP BY, count(), sum()) or add ORDER BY ... LIMIT for a top-N.")
        if g.row_cap is not None:
            result["row_cap"] = g.row_cap
        result["stats"] = out.stats()
        return result

    def validate_query(self, sql: str, params: dict[str, Any] | None = None, check_values: bool = True,
                       max_rows: int | None = None) -> dict[str, Any]:
        try:
            g = self._guarded(sql, max_rows)
        except Rejected as exc:
            return {"ok": True, "valid": False, "verdict": "rejected", **{k: v for k, v in exc.to_dict().items()
                                                                        if k != "ok"}}
        findings = self.advisor.review(g, check_values=check_values, live_values=check_values)
        estimate = None
        try:
            if not any(f["code"] == "unknown_column" for f in findings) and any((self.catalog.tables.get(f"{d}.{t}") or Table(d, t)).is_merge_tree for d, t in g.tables):
                estimate = self.executor.estimate(g.sql, params)
        except ClickSenseError as exc:
            findings.append({"severity": "error", "code": exc.code, "message": exc.message, "fix": exc.hint})
        rows = sum(int(e.get("rows", 0)) for e in estimate or [])
        if rows > self.limits.preflight_max_rows:
            findings.append({"severity": "error", "code": "too_expensive",
                             "message": f"Estimated {rows:,} rows to read (budget {self.limits.preflight_max_rows:,}).",
                             "fix": "Filter on the partition/time column or primary-key prefix."})
        errors = [f for f in findings if f["severity"] == "error"]
        return {"ok": True, "valid": not errors,
                "verdict": "fix_before_running" if errors else ("review_warnings" if any(
                    f["severity"] == "warning" for f in findings) else "ok"),
                "findings": findings, "estimate": estimate, "estimated_rows": rows if estimate is not None else None,
                "sql_to_run": g.sql, "parser": g.parser}

    def explain_query(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        g = self._guarded(sql, None)
        plan = self.executor.explain(g.original, params, kind="indexes = 1")
        estimate = self.executor.estimate(g.original, params)
        findings = [f for f in self.advisor.review(g, check_values=False) if f["code"] != "caveat"]
        return {"ok": True, "estimate": estimate,
                "estimated_rows": sum(int(e.get("rows", 0)) for e in estimate),
                "plan": plan[:8000] + ("\n... (truncated)" if len(plan) > 8000 else ""), "findings": findings}

    def sample_rows(self, table: str, n: int = 10, columns: list[str] | None = None,
                    where: str | None = None) -> dict[str, Any]:
        t = self.catalog.resolve(table, self.default_db)
        cols = [self.catalog.column(t, c) for c in columns] if columns else list(t.columns.values())
        if self.settings.policy.pii == "block":
            cols = [c for c in cols if not c.pii]
        if not cols:
            raise ClickSenseError("no_columns", "No selectable columns (all requested columns are PII).")
        sql = f"SELECT {', '.join(_q(c.name) for c in cols)} FROM {_q(t.database)}.{_q(t.name)}"
        if where:
            sql += f" WHERE {where}"
        sql += f" LIMIT {max(1, min(n, 100))}"
        res = self.run_query(sql, max_rows=n, tool="sample_rows")
        hidden = [c.name for c in t.columns.values() if c.pii and self.settings.policy.pii == "block"]
        if hidden:
            res["hidden_pii_columns"] = hidden
        return res

    # ================================================================== control & disambiguation
    def resolve_values(self, table: str, column: str, terms: list[str]) -> dict[str, Any]:
        t = self.catalog.resolve(table, self.default_db)
        c = self.catalog.column(t, column)
        if c.pii and self.settings.policy.pii == "block":
            raise Rejected("pii_column", f"{t.fqn}.{c.name} is PII: its values are not enumerated.")
        results = self.resolver.resolve(t, c, terms)
        all_ok = all(r["status"] in ("exact", "resolved") for r in results)
        return {"ok": True, "table": t.fqn, "column": c.name, "all_resolved": all_ok, "results": results,
                **({} if all_ok else {"action": "For ambiguous or not_found terms, ask the user (ask_user_to_clarify) "
                                                "with the candidates rather than picking one."})}

    def locate_value(self, term: str, tables: list[str] | None = None, limit: int = 10) -> dict[str, Any]:
        targets = [self.catalog.resolve(x, self.default_db) for x in tables] if tables else self.catalog.all()
        matches = []
        scanned = 0
        for t in targets:
            for c in t.columns.values():
                if not c.is_text or c.pii:
                    continue
                if not (c.type.startswith("LowCardinality") or c.base_type.startswith("Enum") or
                        c.spec.get("values") or c.spec.get("value_synonyms") or c.spec.get("enumerable")):
                    continue
                scanned += 1
                if scanned > 60:
                    break
                try:
                    r = self.resolver.resolve(t, c, [term])[0]
                except ClickSenseError:
                    continue
                for cand in r["candidates"][:3]:
                    if cand["score"] >= 0.8:
                        matches.append({"table": t.fqn, "column": c.name, **cand})
        matches.sort(key=lambda m: (-m["score"], -m.get("rows", 0)))
        out: dict[str, Any] = {"ok": True, "term": term, "matches": matches[:limit], "columns_scanned": scanned}
        if len({(m["table"], m["column"]) for m in matches if m["score"] >= 0.95}) > 1:
            out["ambiguous"] = True
            out["action"] = "The value exists in several columns: choose from the question's intent or ask the user."
        if not matches:
            out["hint"] = (f"'{term}' is not a value of any low-cardinality column. If it is a name or free text, "
                           "use resolve_values on the specific column (it searches high-cardinality columns live).")
        return out

    def find_columns(self, term: str, limit: int = 10) -> dict[str, Any]:
        self.catalog.all()
        scored: dict[tuple[str, str], float] = {}
        for s, d in self.knowledge.search(term, {"column"}, limit=20):
            scored[(d.payload["table"], d.payload["column"])] = s * 2
        ft = fold(term)
        for t in self.catalog.tables.values():
            for c in t.columns.values():
                names = [c.name] + list(c.spec.get("synonyms", []))
                best = max(fuzz.WRatio(ft, fold(n)) for n in names)
                if best >= 80 or ft in fold(c.description):
                    key = (t.fqn, c.name)
                    scored[key] = scored.get(key, 0) + best / 20
        ranked = sorted(scored.items(), key=lambda kv: -kv[1])[:limit]
        results = []
        for (tf, cn), score in ranked:
            t = self.catalog.tables.get(tf)
            c = t.columns.get(cn) if t else None
            if c is None:
                continue
            results.append({"table": tf, "column": cn, "type": c.type, "description": c.description,
                            "score": round(score, 2)})
        out: dict[str, Any] = {"ok": True, "term": term, "candidates": results}
        if len(results) > 1 and results[1]["score"] >= results[0]["score"] * 0.8:
            out["ambiguous"] = True
            out["action"] = "Several columns fit this term: confirm with the user or the glossary which one is meant."
        return out

    def profile_column(self, table: str, column: str) -> dict[str, Any]:
        t = self.catalog.resolve(table, self.default_db)
        c = self.catalog.column(t, column)
        src, scope = f"{_q(t.database)}.{_q(t.name)}", "full"
        if (t.total_rows or 0) > self.settings.value_scan_max_rows:
            if t.sampling_key:
                src, scope = f"{src} SAMPLE {self.settings.value_scan_max_rows}", "sample"
            elif t.time_column:
                src, scope = f"{src} WHERE {_q(t.time_column)} >= today() - 90", "last_90_days"
        col = _q(c.name)
        parts = ["count() AS rows", f"countIf(isNull({col})) AS nulls", f"uniq({col}) AS distinct_approx"]
        bt = c.base_type
        numeric = bt.startswith(("UInt", "Int", "Float", "Decimal"))
        temporal = bt.startswith(("Date", "DateTime"))
        if numeric or temporal:
            parts += [f"min({col}) AS min", f"max({col}) AS max"]
        if numeric:
            parts += [f"avg({col}) AS avg", f"quantiles(0.5, 0.9, 0.99)({col}) AS p50_p90_p99"]
        if c.is_text:
            parts += [f"countIf(empty({col})) AS empty", f"avg(lengthUTF8({col})) AS avg_length"]
        if not (c.pii and self.settings.policy.pii == "block"):
            parts.append(f"topK(10)({col}) AS top_values")
        out = self.executor.run(f"SELECT {', '.join(parts)} FROM {src}", tool="profile_column",
                                settings={"max_execution_time": 15})
        stats = dict(zip(out.columns, out.rows[0])) if out.rows else {}
        return {"ok": True, "table": t.fqn, "column": c.name, "type": c.type, "scope": scope, "stats": stats,
                **({"description": c.description} if c.description else {}), "query": out.stats()}

    def check_freshness(self, table: str) -> dict[str, Any]:
        t = self.catalog.resolve(table, self.default_db)
        tc = t.time_column
        out: dict[str, Any] = {"ok": True, "table": t.fqn, "rows": t.total_rows, "time_column": tc}
        if tc:
            r = self.executor.run(f"SELECT max({_q(tc)}) AS latest, now() AS server_now, "
                                  f"dateDiff('hour', toDateTime(max({_q(tc)})), now()) AS lag_hours "
                                  f"FROM {_q(t.database)}.{_q(t.name)}", tool="check_freshness",
                                  settings={"max_execution_time": 10})
            out.update(dict(zip(r.columns, r.rows[0])))
        expected = t.spec.get("refresh")
        if expected:
            out["expected_refresh"] = expected
        if not tc:
            out["hint"] = "No time column known for this table; declare `time_column` in the dictionary."
        return out

    # ================================================================== ops
    def status(self) -> dict[str, Any]:
        reachable = self.executor.ping()
        ex = self.executor
        out = {
            "ok": True,
            "clickhouse": {"reachable": reachable, "version": ex.server_version, "readonly": ex.readonly_level,
                           "host": f"{self.settings.connection.host}:{self.settings.connection.port}",
                           "user": self.settings.connection.username},
            "protection": {"circuit": ex.breaker.state, "consecutive_failures": ex.breaker.failures,
                           "slots": self.limits.max_concurrent, "running": len(ex.running),
                           "rate_per_minute": self.limits.rate_per_minute,
                           "per_query": {k: getattr(self.limits, k) for k in (
                               "max_execution_time", "max_memory_usage", "max_threads", "max_rows_to_read",
                               "preflight_max_rows", "hard_max_rows")}},
            "running_queries": [{"query_id": q.query_id, "tool": q.tool, "seconds": round(time.time() - q.started, 1),
                                 "sql": q.sql[:200]} for q in ex.running.values()],
            "cache": {"entries": len(ex.cache), "hits": ex.cache.hits, "misses": ex.cache.misses,
                      "value_sets": len(self.resolver.cache)},
            "totals": ex.totals,
            "catalog": {"tables": len(self.catalog.tables), "error": self.catalog.error},
            "knowledge": {"tables": len(self.knowledge.tables), "metrics": len(self.knowledge.metrics),
                          "terms": len(self.knowledge.glossary), "documents": len(self.knowledge.docs),
                          "skills": len(self.knowledge.skills), "examples": len(self.knowledge.examples),
                          "errors": self.knowledge.errors},
            "uptime_s": round(time.time() - self.started),
        }
        if reachable and ex.readonly_level == 1:
            out["warning"] = ("The account has readonly=1: per-query limits cannot be sent and only the profile "
                              "applies. Use readonly=2 with CONSTRAINTS (see README).")
        return out

    def refresh(self) -> dict[str, Any]:
        self.knowledge.reload()
        self.catalog.refresh(force=True)
        self.resolver.cache.clear()
        self.executor.cache.clear()
        return {"ok": True, "tables": len(self.catalog.tables), "catalog_error": self.catalog.error,
                "knowledge_errors": self.knowledge.errors}

    def suggest_knowledge(self, kind: str, title: str, content: str, sql: str | None = None) -> dict[str, Any]:
        inbox: Path = self.settings.semantic_dir / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", fold(title))[:60].strip("-") or "note"
        path = inbox / f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{slug}.md"
        body = f"---\nkind: {kind}\ntitle: {json.dumps(title, ensure_ascii=False)}\nstatus: proposed\n---\n\n{content}\n"
        if sql:
            body += f"\n```sql\n{sql}\n```\n"
        path.write_text(body, encoding="utf-8")
        self.audit.log("suggest_knowledge", "ok", file=path.name)
        return {"ok": True, "saved": str(path.relative_to(self.settings.semantic_dir)),
                "note": "Saved for human review. It is not used until someone moves it into the semantic layer."}


def _excerpt(body: str, query: str, size: int = 600) -> str:
    if len(body) <= size:
        return body
    q = set(tokens(query))
    paras = [p for p in body.split("\n\n") if p.strip()]
    best = max(paras, key=lambda p: len(q & set(tokens(p))), default=body[:size])
    return best[:size] + ("…" if len(best) > size else "")


def _markdown(cols: list[str], rows: list[list[Any]]) -> str:
    def cell(v: Any) -> str:
        return "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)


def _next_steps(has_metric: bool, ambiguous: bool) -> list[str]:
    steps = []
    if ambiguous:
        steps.append("Resolve the ambiguities first (ask_user_to_clarify), do not pick a meaning silently.")
    if has_metric:
        steps.append("A governed metric matches: prefer query_metric over hand-written SQL.")
    steps += ["Check filter values with resolve_values before using them as literals.",
              "validate_query before run_query for any non-trivial SQL."]
    return steps
