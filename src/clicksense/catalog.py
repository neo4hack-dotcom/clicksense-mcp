"""Live schema from ClickHouse system tables, merged with the curated dictionary.

ClickHouse is the source of truth for what exists (tables, columns, types, keys, sizes);
the dictionary adds what only people know (meaning, units, PII, value labels, joins).
A table described in the dictionary but missing from ClickHouse is reported, not invented.
When ClickHouse is unreachable the catalog serves the dictionary alone and says so.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .errors import ClickSenseError
from .executor import Executor
from .knowledge import Knowledge


@dataclass
class Column:
    name: str
    type: str
    comment: str = ""
    in_primary_key: bool = False
    in_sorting_key: bool = False
    in_partition_key: bool = False
    default_kind: str = ""
    spec: dict[str, Any] = field(default_factory=dict)

    @property
    def description(self) -> str:
        return self.spec.get("description") or self.comment

    @property
    def pii(self) -> bool:
        return bool(self.spec.get("pii"))

    @property
    def base_type(self) -> str:
        """LowCardinality(Nullable(String)) -> String."""
        t = self.type
        for wrapper in ("LowCardinality(", "Nullable("):
            while t.startswith(wrapper):
                t = t[len(wrapper):-1]
        return t

    @property
    def is_text(self) -> bool:
        return self.base_type in ("String",) or self.base_type.startswith(("FixedString", "Enum"))

    def to_dict(self, brief: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "type": self.type}
        if self.description:
            out["description"] = self.description
        keys = [k for k, v in (("primary_key", self.in_primary_key), ("sorting_key", self.in_sorting_key),
                               ("partition_key", self.in_partition_key)) if v]
        if keys:
            out["keys"] = keys
        if self.pii:
            out["pii"] = True
        if brief:
            return out
        for k in ("unit", "synonyms", "values", "value_synonyms", "caveats", "examples", "format"):
            if self.spec.get(k):
                out[k] = self.spec[k]
        if self.default_kind:
            out["default_kind"] = self.default_kind
        return out


@dataclass
class Table:
    database: str
    name: str
    engine: str = ""
    total_rows: int | None = None
    total_bytes: int | None = None
    sorting_key: str = ""
    primary_key: str = ""
    partition_key: str = ""
    sampling_key: str = ""
    comment: str = ""
    columns: dict[str, Column] = field(default_factory=dict)
    skip_indices: list[dict[str, Any]] = field(default_factory=list)
    spec: dict[str, Any] = field(default_factory=dict)
    live: bool = True

    @property
    def fqn(self) -> str:
        return f"{self.database}.{self.name}"

    @property
    def description(self) -> str:
        return self.spec.get("description") or self.comment

    @property
    def time_column(self) -> str | None:
        if self.spec.get("time_column"):
            return self.spec["time_column"]
        for c in self.columns.values():
            if c.in_partition_key and c.base_type.startswith(("Date", "DateTime")):
                return c.name
        first = self.sorting_key.split(",")[0].strip() if self.sorting_key else ""
        col = self.columns.get(first)
        if col and col.base_type.startswith(("Date", "DateTime")):
            return col.name
        return None

    @property
    def is_merge_tree(self) -> bool:
        return "MergeTree" in self.engine

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"table": self.fqn}
        if self.description:
            out["description"] = self.description
        if self.total_rows is not None:
            out["rows"] = self.total_rows
        if self.spec.get("grain"):
            out["grain"] = self.spec["grain"]
        if not self.live:
            out["live"] = False
        return out

    def to_dict(self) -> dict[str, Any]:
        out = self.summary()
        out.update({
            "engine": self.engine,
            "bytes": self.total_bytes,
            "sorting_key": self.sorting_key or None,
            "partition_key": self.partition_key or None,
            "sampling_key": self.sampling_key or None,
            "time_column": self.time_column,
            "columns": [c.to_dict() for c in self.columns.values()],
        })
        if self.skip_indices:
            out["skip_indices"] = self.skip_indices
        for k in ("owner", "tags", "caveats", "joins", "dimensions", "refresh", "synonyms"):
            if self.spec.get(k):
                out[k] = self.spec[k]
        hints = []
        if self.sorting_key:
            hints.append(f"Filter on a prefix of ORDER BY ({self.sorting_key}) to use the primary index.")
        if self.partition_key:
            hints.append(f"Filter on {self.time_column or self.partition_key} to prune partitions "
                         f"(PARTITION BY {self.partition_key}).")
        if self.sampling_key:
            hints.append("Supports SAMPLE (e.g. SAMPLE 0.1) for fast approximate exploration.")
        if hints:
            out["performance_hints"] = hints
        return {k: v for k, v in out.items() if v not in (None, [], {})}


_SYS = "('system', 'INFORMATION_SCHEMA', 'information_schema')"


class Catalog:
    def __init__(self, executor: Executor, knowledge: Knowledge, *, ttl_s: float, allowed: list[str],
                 denied: list[str]):
        self.executor = executor
        self.knowledge = knowledge
        self.ttl = ttl_s
        self.allowed = allowed
        self.denied = {d.lower() for d in denied}
        self.tables: dict[str, Table] = {}
        self.loaded_at = 0.0
        self.error: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ loading
    def _filter(self) -> tuple[str, dict[str, Any]]:
        if self.allowed:
            return "database IN {dbs:Array(String)}", {"dbs": self.allowed}
        return f"database NOT IN {_SYS}", {}

    def refresh(self, force: bool = False) -> None:
        if not force and self.tables and time.monotonic() - self.loaded_at < self.ttl:
            return
        with self._lock:
            if not force and self.tables and time.monotonic() - self.loaded_at < self.ttl:
                return
            try:
                self.tables = self._load_live()
                self.error = None
            except ClickSenseError as exc:
                self.error = f"{exc.code}: {exc.message}"
                if not self.tables:
                    self.tables = self._load_dictionary_only()
            self.loaded_at = time.monotonic()

    def _q(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        out = self.executor.run(sql, params, tool="catalog", use_cache=False, admit=False,
                                settings={"max_result_rows": 0, "result_overflow_mode": "throw"})
        return [dict(zip(out.columns, r)) for r in out.rows]

    def _load_live(self) -> dict[str, Table]:
        where, params = self._filter()
        tables: dict[str, Table] = {}
        for r in self._q(
            "SELECT database, name, engine, total_rows, total_bytes, sorting_key, primary_key, partition_key, "
            f"sampling_key, comment FROM system.tables WHERE {where} AND NOT is_temporary "
            "AND engine NOT IN ('MaterializedView') ORDER BY database, name", params):
            fqn = f"{r['database']}.{r['name']}"
            if fqn.lower() in self.denied:
                continue
            tables[fqn] = Table(r["database"], r["name"], r["engine"], r["total_rows"], r["total_bytes"],
                                r["sorting_key"], r["primary_key"], r["partition_key"], r["sampling_key"],
                                r["comment"], spec=self.knowledge.tables.get(fqn, {}))
        for r in self._q(
            "SELECT database, table, name, type, comment, is_in_primary_key, is_in_sorting_key, "
            f"is_in_partition_key, default_kind FROM system.columns WHERE {where} ORDER BY database, table, position",
                params):
            t = tables.get(f"{r['database']}.{r['table']}")
            if t:
                spec = (t.spec.get("columns") or {}).get(r["name"], {})
                t.columns[r["name"]] = Column(r["name"], r["type"], r["comment"], bool(r["is_in_primary_key"]),
                                              bool(r["is_in_sorting_key"]), bool(r["is_in_partition_key"]),
                                              r["default_kind"], spec)
        try:
            for r in self._q(f"SELECT database, table, name, type, expr, granularity FROM system.data_skipping_indices "
                             f"WHERE {where}", params):
                t = tables.get(f"{r['database']}.{r['table']}")
                if t:
                    t.skip_indices.append({"name": r["name"], "type": r["type"], "expr": r["expr"]})
        except ClickSenseError:
            pass  # older servers / restricted grants: indices are a nice-to-have
        return tables

    def _load_dictionary_only(self) -> dict[str, Table]:
        out = {}
        for fqn, spec in self.knowledge.tables.items():
            db, _, name = fqn.partition(".")
            t = Table(db, name, spec=spec, live=False)
            for cname, cspec in (spec.get("columns") or {}).items():
                t.columns[cname] = Column(cname, cspec.get("type", "?"), spec=cspec)
            out[fqn] = t
        return out

    def sync_specs(self) -> None:
        """Re-attach dictionary specs after the knowledge files changed (no DB round trip)."""
        for fqn, t in self.tables.items():
            t.spec = self.knowledge.tables.get(fqn, {})
            for cname, c in t.columns.items():
                c.spec = (t.spec.get("columns") or {}).get(cname, {})

    # ------------------------------------------------------------------ lookups
    def all(self) -> list[Table]:
        self.refresh()
        self.knowledge.maybe_reload()
        self.sync_specs()
        return list(self.tables.values())

    def resolve(self, name: str, default_db: str) -> Table:
        self.refresh()
        name = name.strip().strip("`\"")
        fqn = name if "." in name else f"{default_db}.{name}"
        if fqn in self.tables:
            return self.tables[fqn]
        lowered = {k.lower(): v for k, v in self.tables.items()}
        if fqn.lower() in lowered:
            return lowered[fqn.lower()]
        matches = [t for t in self.tables.values() if t.name.lower() == name.lower().split(".")[-1]]
        if len(matches) == 1:
            return matches[0]
        from rapidfuzz import process

        close = process.extract(name, list(self.tables), limit=3, score_cutoff=60)
        raise ClickSenseError("unknown_table", f"Unknown table '{name}'.",
                              "Did you mean: " + ", ".join(c[0] for c in close) if close
                              else "Use list_tables to see what exists.",
                              candidates=[c[0] for c in close] or [m.fqn for m in matches])

    def column(self, table: Table, name: str) -> Column:
        if name in table.columns:
            return table.columns[name]
        for c in table.columns.values():
            if c.name.lower() == name.lower():
                return c
        from rapidfuzz import process

        close = process.extract(name, list(table.columns), limit=3, score_cutoff=55)
        syn = [c.name for c in table.columns.values()
               if name.lower() in [s.lower() for s in c.spec.get("synonyms", [])]]
        cands = syn + [c[0] for c in close if c[0] not in syn]
        raise ClickSenseError("unknown_column", f"Column '{name}' does not exist in {table.fqn}.",
                              ("Did you mean: " + ", ".join(cands)) if cands else "Use describe_table.",
                              candidates=cands)

    def pii_columns(self, db: str, table: str) -> set[str]:
        t = self.tables.get(f"{db}.{table}")
        if not t:
            spec = self.knowledge.table_spec(db, table)
            return {c for c, s in (spec.get("columns") or {}).items() if s.get("pii")}
        return {c.name for c in t.columns.values() if c.pii}
