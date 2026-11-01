"""Value disambiguation: map what a person says to what the column actually stores.

The single most common silent-wrong-answer in text-to-SQL is a literal that does not exist
(`country = 'France'` when the column stores 'FR'): the query succeeds and returns nothing,
or worse, a plausible partial total. The resolver closes that gap.

Sources, cheapest first:
1. dictionary labels and value synonyms (no query);
2. a cached enumeration of the column's distinct values with their frequency, built with a
   bounded query (sampled or time-windowed on large tables), refreshed hourly;
3. for high-cardinality columns, a bounded live search (accent/case-insensitive contains,
   then n-gram distance) that returns the 20 most frequent matches.

Every answer carries a status: `exact`, `resolved` (one confident mapping), `ambiguous`
(several plausible values: ask, don't guess) or `not_found` (with the closest suggestions).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz, process

from .catalog import Catalog, Column, Table
from .errors import ClickSenseError
from .executor import Executor
from .knowledge import fold, norm

# ---------------------------------------------------------------------------------------------
#  Matching scores and decision thresholds. Tune them here: the tests in test_semantic.py
#  (test_resolver_*) show the expected behaviour on concrete cases and must stay green.
#
#  A candidate gets the score of the best way it matched. Then:
#    top score == SCORE_EXACT                         -> status "exact"
#    one candidate >= STRONG (or clearly ahead)       -> status "resolved" (the value is used)
#    several >= STRONG, or the best is below STRONG   -> status "ambiguous" (ask the user)
#    no candidate                                     -> status "not_found"
# ---------------------------------------------------------------------------------------------
SCORE_EXACT = 1.0          # same string
SCORE_FOLDED = 0.97        # same after case/accent/punctuation folding ("saint etienne" = "Saint-Étienne")
SCORE_LABEL = 0.96         # matches the dictionary label of a value ("France" -> FR)
SCORE_SYNONYM = 0.96       # matches a dictionary value_synonym ("canceled" -> cancelled)
SCORE_PREFIX = 0.82        # the stored value starts with the word (3+ characters)
SCORE_CONTAINS = 0.75      # the stored value contains the word (3+ characters)
FUZZY_CUTOFF = 72          # rapidfuzz WRatio (0-100) below which a fuzzy match is ignored
FUZZY_WEIGHT = 0.88        # fuzzy score = WRatio / 100 * this: a typo match never beats a synonym
LIVE_SCORES = (0.97, 0.8, 0.65)  # live search on big columns: equal / contains / n-gram neighbour
NGRAM_MAX_DISTANCE = 0.55  # ClickHouse ngramDistance threshold for the fuzzy live search
STRONG = 0.9               # a candidate at or above this is acceptable without asking
CLEAR_LEAD = 0.02          # several strong candidates: the best wins only if ahead by this much
MAX_CANDIDATES = 8         # candidates returned per term


@dataclass
class ValueSet:
    """The distinct values of one column with their frequency, cached `value_cache_ttl_s`.

    complete=False means the column has more distinct values than value_cache_max_distinct (only
    the most frequent are kept) or they were computed on a sample/window: a word absent from the
    list may still exist, so the live search is used for it."""

    values: list[tuple[Any, int]]  # (value, frequency), most frequent first
    complete: bool  # False: high cardinality, only the top values are here
    scope: str  # how it was computed: full | sample | window
    at: float


class Resolver:
    """Maps user wording to stored values. One per server; caches value sets per column."""

    def __init__(self, executor: Executor, catalog: Catalog, *, ttl_s: float, max_distinct: int,
                 scan_max_rows: int):
        self.executor = executor
        self.catalog = catalog
        self.ttl = ttl_s
        self.max_distinct = max_distinct
        self.scan_max_rows = scan_max_rows
        self.cache: dict[str, ValueSet] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ enumeration
    def values(self, table: Table, col: Column, *, allow_query: bool = True) -> ValueSet | None:
        """The cached value set of a column, computing it if needed (unless allow_query=False,
        used by the lint so that checking a query never triggers a scan)."""
        key = f"{table.fqn}.{col.name}"
        vs = self.cache.get(key)
        if vs and time.monotonic() - vs.at < self.ttl:
            return vs
        if not allow_query:
            return vs
        with self._lock:
            vs = self.cache.get(key)
            if vs and time.monotonic() - vs.at < self.ttl:
                return vs
            vs = self._enumerate(table, col)
            self.cache[key] = vs
            return vs

    def _scope(self, table: Table) -> tuple[str, str, str]:
        """How to bound the enumeration of a big table: (SAMPLE clause, WHERE clause, label).
        Small tables are read in full; big ones are sampled (if they have a sampling key) or
        limited to the last 365 days of their time column."""
        rows = table.total_rows or 0
        if rows <= self.scan_max_rows:
            return "", "", "full"
        if table.sampling_key:
            return f"SAMPLE {self.scan_max_rows}", "", "sample"
        if table.time_column:
            return "", f"WHERE {_q(table.time_column)} >= today() - 365", "window"
        return "", "", "full"  # bounded by max_rows_to_read below; may fail -> live search only

    def _enumerate(self, table: Table, col: Column) -> ValueSet:
        """SELECT value, count() ... GROUP BY value, bounded in rows read and time. A refusal for
        load reasons gives an empty, incomplete set (the live search then takes over)."""
        sample, where, scope = self._scope(table)
        sql = (f"SELECT {_q(col.name)} AS v, count() AS n FROM {_q(table.database)}.{_q(table.name)} {sample} "
               f"{where} GROUP BY v ORDER BY n DESC LIMIT {self.max_distinct + 1}")
        try:
            out = self.executor.run(sql, tool="resolver", use_cache=False,
                                    settings={"max_rows_to_read": self.scan_max_rows * 2, "max_execution_time": 15})
        except ClickSenseError as exc:
            if exc.code in ("too_many_rows_to_read", "timeout", "memory_limit", "too_many_bytes_to_read"):
                return ValueSet([], False, "unavailable", time.monotonic())
            raise
        vals = [(r[0], int(r[1])) for r in out.rows]
        complete = len(vals) <= self.max_distinct and scope == "full"
        return ValueSet(vals[: self.max_distinct], complete, scope, time.monotonic())

    def _live_search(self, table: Table, col: Column, term: str, limit: int = 20) -> list[tuple[Any, int]]:
        """For high-cardinality columns: the most frequent stored values containing the word
        (accent/case-insensitive), else the closest by n-gram distance. Bounded like any query."""
        expr = f"toString({_q(col.name)})"
        folded = f"replaceRegexpAll(normalizeUTF8NFD(lowerUTF8({expr})), '\\\\p{{Mn}}', '')"
        base = f"FROM {_q(table.database)}.{_q(table.name)}"
        settings = {"max_rows_to_read": self.scan_max_rows * 2, "max_execution_time": 10}
        try:
            out = self.executor.run(
                f"SELECT {_q(col.name)} AS v, count() AS n {base} WHERE position({folded}, {{t:String}}) > 0 "
                f"GROUP BY v ORDER BY n DESC LIMIT {limit}", {"t": fold(term)}, tool="resolver", settings=settings)
            if out.rows:
                return [(r[0], int(r[1])) for r in out.rows]
            out = self.executor.run(
                f"SELECT {_q(col.name)} AS v, count() AS n {base} "
                f"WHERE ngramDistanceCaseInsensitiveUTF8({expr}, {{t:String}}) < {NGRAM_MAX_DISTANCE} "
                f"GROUP BY v ORDER BY n DESC LIMIT {limit}", {"t": term}, tool="resolver", settings=settings)
            return [(r[0], int(r[1])) for r in out.rows]
        except ClickSenseError as exc:
            if exc.code in ("too_many_rows_to_read", "timeout", "memory_limit", "too_many_bytes_to_read"):
                return []
            raise

    # ------------------------------------------------------------------ matching
    def resolve(self, table: Table, col: Column, terms: list[Any], *, allow_query: bool = True) -> list[dict[str, Any]]:
        """Resolve each user word against the column. Sources: stored values (cached), dictionary
        `values` labels and `value_synonyms`, then live search. One result dict per term:
        {term, status, value?, candidates[{value, score, match, label?, rows?}], coverage, hint?}."""
        spec = col.spec
        labels: dict[str, str] = {str(k): str(v) for k, v in (spec.get("values") or {}).items()}
        synonyms: dict[str, list[str]] = {str(k): [str(s) for s in v] for k, v in
                                          (spec.get("value_synonyms") or {}).items()}
        vs = self.values(table, col, allow_query=allow_query) if (allow_query or self.cache) else None
        stored = vs.values if vs else []
        freq = {str(v): n for v, n in stored}
        known = list(dict.fromkeys([str(v) for v, _ in stored] + list(labels) + list(synonyms)))
        results = []
        for term in terms:
            results.append(self._resolve_one(table, col, term, known, labels, synonyms, freq, vs, allow_query))
        return results

    def _resolve_one(self, table, col, term, known, labels, synonyms, freq, vs, allow_query) -> dict[str, Any]:
        """Score every candidate value for one word (best way it matched wins), rank by score then
        by frequency, and decide the status with the thresholds at the top of this module."""
        t = str(term)
        ft = fold(t).strip()
        nt = norm(t)
        cands: dict[str, tuple[float, str]] = {}

        def add(value: str, score: float, how: str) -> None:
            """Record a candidate, keeping its best score and how it matched."""
            if value not in cands or cands[value][0] < score:
                cands[value] = (score, how)

        for v in known:
            fv = fold(v)
            if v == t:
                add(v, SCORE_EXACT, "exact")
            elif fv == ft or norm(v) == nt:
                add(v, SCORE_FOLDED, "case/accent")
        for v, label in labels.items():
            if norm(label) == nt:
                add(v, SCORE_LABEL, "label")
        for v, syns in synonyms.items():
            if any(norm(s) == nt for s in syns):
                add(v, SCORE_SYNONYM, "synonym")
        # Weaker evidence only when nothing matched exactly, by label or by synonym.
        if not cands and ft:
            for v in known:
                fv = fold(v)
                if len(ft) >= 3 and fv.startswith(ft):
                    add(v, SCORE_PREFIX, "prefix")
                elif len(ft) >= 3 and ft in fv:
                    add(v, SCORE_CONTAINS, "contains")
            choices = {v: fold(v) for v in known}
            # Labels are fuzzy-matched too; "\x00" separates value and label in the choice key.
            choices.update({f"{v}\x00{lab}": fold(lab) for v, lab in labels.items()})
            for _, score, key in process.extract(ft, choices, scorer=fuzz.WRatio, limit=8,
                                                 score_cutoff=FUZZY_CUTOFF):
                add(key.split("\x00")[0], round(score / 100 * FUZZY_WEIGHT, 3), "fuzzy")
        # Still nothing and the cached list is not exhaustive: ask ClickHouse directly.
        if not cands and allow_query and vs is not None and not vs.complete and col.is_text:
            for v, n in self._live_search(table, col, t):
                sv = str(v)
                freq[sv] = n
                fv = fold(sv)
                equal, contains, near = LIVE_SCORES
                add(sv, equal if fv == ft else (contains if ft in fv else near), "live_search")

        ranked = sorted(cands.items(), key=lambda kv: (-kv[1][0], -freq.get(kv[0], 0)))
        candidates = [{"value": _typed(v, col), "score": s, "match": how,
                       **({"label": labels[v]} if v in labels else {}),
                       **({"rows": freq[v]} if v in freq else {})} for v, (s, how) in ranked[:MAX_CANDIDATES]]
        status = "not_found"
        if candidates:
            top = candidates[0]["score"]
            strong = [c for c in candidates if c["score"] >= STRONG]
            if top == SCORE_EXACT:
                status = "exact"
            elif len(strong) == 1 or (top >= STRONG and len(strong) > 1
                                      and candidates[1]["score"] < top - CLEAR_LEAD):
                status = "resolved"
            elif len(strong) > 1 or top < STRONG:
                status = "ambiguous"
        out: dict[str, Any] = {"term": term, "status": status, "candidates": candidates}
        if status in ("exact", "resolved"):
            out["value"] = candidates[0]["value"]
        if status == "not_found":
            out["hint"] = ("No stored value matches. " + (
                "The value list is complete, so this value does not occur in the data."
                if vs and vs.complete else "Try resolve_values with a shorter or different spelling, "
                                           "or locate_value to find which column holds it."))
        if vs is not None:
            out["coverage"] = vs.scope if vs.complete or vs.scope != "full" else "top_values"
        return out

    def known_values(self, table: Table, col: Column) -> list[str] | None:
        """Cached complete value list or dictionary labels, without querying. For lint."""
        vs = self.cache.get(f"{table.fqn}.{col.name}")
        if vs and vs.complete:
            return [str(v) for v, _ in vs.values]
        if col.spec.get("values") and col.spec.get("values_complete", True):
            return [str(k) for k in col.spec["values"]]
        return None


def _typed(v: str, col: Column) -> Any:
    """Return candidates in the column's type (42, not "42"), so they can be used as parameters."""
    bt = col.base_type
    if bt.startswith(("UInt", "Int")):
        try:
            return int(v)
        except ValueError:
            return v
    if bt.startswith(("Float", "Decimal")):
        try:
            return float(v)
        except ValueError:
            return v
    return v


def _q(ident: str) -> str:
    """Quote an identifier with backticks (names come from the catalog, never from the user)."""
    return "`" + str(ident).replace("`", "``") + "`"
