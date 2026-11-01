"""Tools for the people who maintain the semantic layer (`clicksense-admin kb ...`).

Three jobs, all safe to run at any time:

* `validate()` — lint every file of semantic/ and report problems with the file, the entry and the
  fix. Offline it checks structure (missing keys, typos, broken references between files);
  with a live catalog it also checks that tables, columns and metric SQL exist in ClickHouse.
  Run it after every edit: the server would otherwise load a broken entry silently or skip it.
* `scaffold()` — add a new entry (metric, term, example, knowledge doc, skill, table) from the
  commented templates in semantic/_templates/, so nobody starts from a blank page.
* `bootstrap_table()` — write the dictionary entry of a table from ClickHouse itself: columns,
  types, COMMENTs, keys, and the values of low-cardinality columns. You then only fill meanings.

Nothing here talks to an LLM, and the only database access is read-only metadata and bounded
value enumeration, through the same executor (limits, breaker) as the MCP tools.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from rapidfuzz import process

from .errors import ClickSenseError
from .knowledge import (COLUMN_KEYS, DIMENSION_KEYS, DOC_KEYS, EXAMPLE_KEYS, JOIN_KEYS, METRIC_KEYS, SENSE_KEYS,
                        SKILL_KEYS, TABLE_KEYS, TERM_KEYS, Knowledge, _read_md)

TEMPLATES = Path(__file__).resolve().parent / "templates"
KINDS = ("table", "metric", "term", "example", "knowledge", "skill")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass
class Finding:
    """One problem found by validate(): its level, where it is (file > entry), what is wrong and how to fix it."""

    level: str  # error: the entry is broken or ignored | warning: probably wrong | info: suggestion
    where: str  # file and entry, e.g. "metrics.yaml > revenue"
    message: str
    fix: str = ""

    def line(self) -> str:
        """The finding as printed by `clicksense-admin kb validate`."""
        tag = {"error": "ERROR  ", "warning": "WARNING", "info": "INFO   "}[self.level]
        return f"{tag} {self.where}: {self.message}" + (f"\n        -> {self.fix}" if self.fix else "")


# =============================================================================================
#  Validation
# =============================================================================================

def _unknown_keys(entry: dict, allowed: set[str], where: str, out: list[Finding]) -> None:
    """Flag keys the loader does not read, with the closest valid key: typos lose data silently."""
    for key in entry:
        if key not in allowed:
            close = process.extractOne(str(key), sorted(allowed), score_cutoff=70)
            out.append(Finding("warning", where, f"unknown key '{key}' is ignored",
                               f"did you mean '{close[0]}'?" if close else f"valid keys: {', '.join(sorted(allowed))}"))


def _has_todo(obj: Any) -> bool:
    """True when a scaffolded entry still contains a TODO placeholder somewhere."""
    if isinstance(obj, str):
        return "TODO" in obj
    if isinstance(obj, dict):
        return any(_has_todo(k) or _has_todo(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_has_todo(v) for v in obj)
    return False


def _todo(obj: Any, where: str, out: list[Finding]) -> None:
    """Add a warning if the entry still has TODO placeholders."""
    if _has_todo(obj):
        out.append(Finding("warning", where, "still contains TODO placeholders",
                           "fill them in: the agent reads this text as-is"))


def _rel(k: Knowledge, path: Path | None, fallback: str) -> str:
    """A path relative to semantic/ for messages."""
    if path is None:
        return fallback
    try:
        return path.relative_to(k.root).as_posix()
    except ValueError:
        return str(path)


def validate(k: Knowledge, catalog: Any = None, service: Any = None) -> list[Finding]:
    """All findings for the semantic layer. `catalog` (live schema) and `service` (to compile and
    EXPLAIN metrics and examples) are optional: without them only offline checks run."""
    out: list[Finding] = []
    k.reload()
    for err in k.errors:
        out.append(Finding("error", "load", err, "fix the YAML / front-matter syntax; the file is skipped"))
    live = dict(getattr(catalog, "tables", {}) or {}) if catalog is not None else {}

    # ---------------------------------------------------------------- dictionary
    for tname, spec in k.tables.items():
        where = f"{_rel(k, k.sources.get(f'table:{tname}'), 'dictionary')} > {tname}"
        if not re.match(r"^[^.\s]+\.[^.\s]+$", tname):
            out.append(Finding("error", where, "table key must be 'database.table'"))
        _unknown_keys({kk: v for kk, v in spec.items()}, TABLE_KEYS, where, out)
        _todo(spec, where, out)
        if not spec.get("description"):
            out.append(Finding("info", where, "no description", "one sentence: what one row is and where it comes from"))
        t = live.get(tname)
        if catalog is not None and t is None:
            out.append(Finding("error", where, "table not found in ClickHouse (or not allowed by policy)",
                               "fix the name, or remove the entry"))
        for cname, cspec in (spec.get("columns") or {}).items():
            cw = f"{where}.{cname}"
            _unknown_keys(cspec, COLUMN_KEYS, cw, out)
            if t is not None and cname not in t.columns:
                close = process.extractOne(cname, list(t.columns), score_cutoff=60)
                out.append(Finding("error", cw, "column not found in ClickHouse",
                                   f"did you mean '{close[0]}'?" if close else "remove it"))
            vals = cspec.get("values")
            if vals is not None and not isinstance(vals, dict):
                out.append(Finding("error", cw, "'values' must map stored value -> label", "e.g. values: {FR: France}"))
            syn = cspec.get("value_synonyms")
            if syn is not None and not isinstance(syn, dict):
                out.append(Finding("error", cw, "'value_synonyms' must map stored value -> [words]"))
            elif isinstance(syn, dict) and isinstance(vals, dict):
                for v in syn:
                    if str(v) not in {str(x) for x in vals}:
                        out.append(Finding("warning", cw, f"value_synonyms has '{v}', which is not in values",
                                           "a synonym must point to a stored value"))
            if "pii" in cspec and not isinstance(cspec["pii"], bool):
                out.append(Finding("error", cw, "'pii' must be true or false"))
        tc = spec.get("time_column")
        if tc and t is not None and tc not in t.columns:  # the dictionary may document only some columns
            out.append(Finding("error", where, f"time_column '{tc}' is not a column of the table"))
        joins = spec.get("joins") or {}
        for jname, j in joins.items():
            jw = f"{where} > joins.{jname}"
            if not isinstance(j, dict):
                out.append(Finding("error", jw, "a join must be a mapping {table, alias, on}"))
                continue
            _unknown_keys(j, JOIN_KEYS, jw, out)
            for req in ("table", "on"):
                if not j.get(req):
                    out.append(Finding("error", jw, f"missing '{req}'"))
            if j.get("table") and j["table"] not in k.tables and j["table"] not in live:
                out.append(Finding("warning", jw, f"joined table {j['table']} is not documented"))
        for dname, d in (spec.get("dimensions") or {}).items():
            dw = f"{where} > dimensions.{dname}"
            if not isinstance(d, dict) or not d.get("sql"):
                out.append(Finding("error", dw, "a dimension needs 'sql' (e.g. sql: p.category)"))
                continue
            _unknown_keys(d, DIMENSION_KEYS, dw, out)
            if d.get("join") and d["join"] not in joins:
                out.append(Finding("error", dw, f"join '{d['join']}' is not declared in joins",
                                   f"declared: {', '.join(joins) or 'none'}"))

    # ---------------------------------------------------------------- metrics
    seen_words: dict[str, str] = {}
    for name, m in k.metrics.items():
        where = f"metrics.yaml > {name}"
        _unknown_keys(m, METRIC_KEYS, where, out)
        _todo(m, where, out)
        for req in ("table", "expression", "description"):
            if not m.get(req):
                out.append(Finding("error" if req != "description" else "warning", where, f"missing '{req}'"))
        table = m.get("table")
        if table and table not in k.tables and table not in live:
            out.append(Finding("error", where, f"table {table} is neither documented nor in ClickHouse"))
        tspec = k.tables.get(table or "", {})
        known_dims = set(tspec.get("columns") or {}) | set(tspec.get("dimensions") or {})
        if table in live:
            known_dims |= set(live[table].columns)
        for dim in m.get("dimensions") or []:
            if known_dims and dim not in known_dims:
                out.append(Finding("error", where, f"dimension '{dim}' is neither a column nor a declared dimension "
                                                   f"of {table}", "declare it under dimensions: in the dictionary"))
        for word in [name] + list(m.get("synonyms") or []):
            key = str(word).casefold()
            if key in seen_words and seen_words[key] != name:
                out.append(Finding("warning", where, f"'{word}' also names metric {seen_words[key]}",
                                   "a word must point to one metric; use the glossary for ambiguous words"))
            seen_words[key] = name
        if service is not None and table:
            try:
                compiled = service.metrics.compile([name], limit=1)
                service.executor.explain(compiled.sql, compiled.params, kind="PLAN")
            except ClickSenseError as exc:
                out.append(Finding("error", where, f"the compiled SQL fails: {exc.message}", exc.hint or ""))

    # ---------------------------------------------------------------- glossary
    terms: set[str] = set()
    for i, term in enumerate(k.glossary):
        where = f"glossary.yaml > {term.get('term', f'#{i + 1}')}"
        _unknown_keys(term, TERM_KEYS, where, out)
        _todo(term, where, out)
        if not term.get("term"):
            out.append(Finding("error", where, "missing 'term'"))
            continue
        if term["term"].casefold() in terms:
            out.append(Finding("warning", where, "duplicate term", "merge the two entries"))
        terms.add(term["term"].casefold())
        if not term.get("definition"):
            out.append(Finding("warning", where, "missing 'definition'"))
        targets = [term.get("maps_to")]
        for s in term.get("senses") or []:
            _unknown_keys(s, SENSE_KEYS, f"{where} > sense", out)
            if not s.get("meaning"):
                out.append(Finding("error", f"{where} > sense", "a sense needs 'meaning'"))
            targets.append(s.get("maps_to"))
        for t in targets:
            if isinstance(t, str) and t.startswith("metric:") and t.split(":", 1)[1] not in k.metrics:
                out.append(Finding("error", where, f"maps_to {t}: no such metric",
                                   f"metrics: {', '.join(sorted(k.metrics))}"))
        if term.get("senses") and len(term["senses"]) == 1:
            out.append(Finding("info", where, "one sense only: not ambiguous", "use maps_to instead of senses"))

    # ---------------------------------------------------------------- examples
    for i, ex in enumerate(k.examples):
        where = f"examples.yaml > #{i + 1} {str(ex.get('question', ''))[:50]}"
        _unknown_keys(ex, EXAMPLE_KEYS, where, out)
        _todo(ex, where, out)
        if not ex.get("question") or not ex.get("sql"):
            out.append(Finding("error", where, "an example needs 'question' and 'sql'"))
            continue
        if service is not None:
            try:
                g = service.guard.check(ex["sql"], 10)
                service.executor.explain(g.original, kind="PLAN")
            except ClickSenseError as exc:
                out.append(Finding("error", where, f"the SQL no longer runs: {exc.message}",
                                   exc.hint or "update the example: agents copy it"))

    # ---------------------------------------------------------------- knowledge documents
    for doc_id, d in k.docs.items():
        path = k.sources.get(f"doc:{doc_id}")
        where = _rel(k, path, f"knowledge/{doc_id}.md")
        meta = _read_md(path)[0] if path else {}
        _unknown_keys(meta, DOC_KEYS, where, out)
        _todo([meta, d["body"]], where, out)
        if not meta.get("title"):
            out.append(Finding("warning", where, "no 'title' in the front-matter"))
        if len(d["body"]) < 40:
            out.append(Finding("warning", where, "almost empty body"))
        for t in d.get("applies_to") or []:
            if t not in k.tables and t not in live:
                out.append(Finding("warning", where, f"applies_to {t}: unknown table"))

    # ---------------------------------------------------------------- skills
    for name, meta in getattr(k, "skills_meta", {}).items():
        path = k.sources.get(f"skill:{name}")
        where = _rel(k, path, f"skills/{name}")
        _unknown_keys(meta, SKILL_KEYS, where, out)
        _todo([meta, k.skills.get(name, {}).get("body", "")], where, out)
        if not meta.get("description"):
            out.append(Finding("error", where, "no 'description': clients use it to decide when to load the skill"))
        if path is not None and path.name == "SKILL.md" and meta.get("name") and meta["name"] != path.parent.name:
            out.append(Finding("warning", where, f"name '{meta['name']}' differs from its folder '{path.parent.name}'",
                               "MCP skill clients use the folder name; keep them identical"))
        if name and not _SLUG.match(name):
            out.append(Finding("warning", where, "skill names should be lowercase-with-hyphens"))

    # ---------------------------------------------------------------- inbox
    inbox = sorted((k.root / "inbox").glob("*.md"))
    if inbox:
        out.append(Finding("info", "inbox", f"{len(inbox)} suggestion(s) from the agent waiting for review",
                           "clicksense-admin kb inbox"))
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda f: order[f.level])


# =============================================================================================
#  Scaffolding from templates
# =============================================================================================

def _template(file: str, **values: str) -> str:
    """A template from src/clicksense/templates/ with its {{placeholders}} filled."""
    text = (TEMPLATES / file).read_text(encoding="utf-8")
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", val)
    return text


def _append_to_list_file(path: Path, top_key: str, block: str) -> None:
    """Append a YAML list item under `top_key:` (the file's only top-level key), then re-parse the
    file and roll back if the result is not valid YAML or the list did not grow."""
    before = path.read_text(encoding="utf-8") if path.exists() else f"{top_key}:\n"
    count = len((yaml.safe_load(before) or {}).get(top_key) or [])
    after = before.rstrip("\n") + "\n\n" + block.rstrip("\n") + "\n"
    try:
        data = yaml.safe_load(after) or {}
        ok = len(data.get(top_key) or []) == count + 1
    except yaml.YAMLError:
        ok = False
    if not ok:
        raise ClickSenseError("scaffold_failed", f"could not append to {path.name} safely",
                              f"add the entry by hand; template: {block[:200]}")
    path.write_text(after, encoding="utf-8")


def scaffold(root: Path, kind: str, name: str) -> Path:
    """Create a new entry from its template and return the file to edit next."""
    if kind not in KINDS:
        raise ClickSenseError("bad_kind", f"unknown kind '{kind}'", f"one of: {', '.join(KINDS)}")
    if kind in ("knowledge", "skill") and not _SLUG.match(name):
        raise ClickSenseError("bad_name", f"'{name}' is not a valid file name",
                              "lowercase letters, digits, '-' and '_' only, e.g. refund-policy")
    if kind == "metric":
        if not re.match(r"^[a-z][a-z0-9_]*$", name):
            raise ClickSenseError("bad_name", "metric names are snake_case, e.g. net_revenue")
        k = Knowledge(root)
        if name in k.metrics:
            raise ClickSenseError("exists", f"metric '{name}' already exists in metrics.yaml")
        path = root / "metrics.yaml"
        _append_to_list_file(path, "metrics", _template("metric.yaml", name=name))
        return path
    if kind == "term":
        path = root / "glossary.yaml"
        _append_to_list_file(path, "terms", _template("term.yaml", name=_q(name)))
        return path
    if kind == "example":
        path = root / "examples.yaml"
        _append_to_list_file(path, "examples", _template("example.yaml", name=_q(name)))
        return path
    if kind == "knowledge":
        path = root / "knowledge" / f"{name}.md"
        if path.exists():
            raise ClickSenseError("exists", f"{path} already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_template("knowledge.md", name=name), encoding="utf-8")
        return path
    if kind == "skill":
        path = root / "skills" / name / "SKILL.md"
        if path.exists():
            raise ClickSenseError("exists", f"{path} already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_template("skill.md", name=name), encoding="utf-8")
        return path
    # table: an empty, commented entry (bootstrap_table fills it from ClickHouse instead)
    if not re.match(r"^[^.\s]+\.[^.\s]+$", name):
        raise ClickSenseError("bad_name", "tables are named 'database.table'")
    db = name.split(".")[0]
    path = root / "dictionary" / f"{db}.yaml"
    _append_table(path, name, _template("table.yaml", name=name))
    return path


def _append_table(path: Path, table: str, block: str) -> None:
    """Add one table under `tables:` of a dictionary file (created if missing)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    before = path.read_text(encoding="utf-8") if path.exists() else "tables:\n"
    data = yaml.safe_load(before) or {}
    if table in (data.get("tables") or {}):
        raise ClickSenseError("exists", f"{table} is already documented in {path.name}",
                              "edit the existing entry instead")
    indented = "\n".join(("  " + ln) if ln.strip() else "" for ln in block.rstrip("\n").splitlines())
    after = before.rstrip("\n") + "\n\n" + indented + "\n"
    try:
        ok = table in ((yaml.safe_load(after) or {}).get("tables") or {})
    except yaml.YAMLError:
        ok = False
    if not ok:
        raise ClickSenseError("scaffold_failed", f"could not append {table} to {path.name} safely",
                              "the file must have a single top-level 'tables:' key")
    path.write_text(after, encoding="utf-8")


# =============================================================================================
#  Bootstrap a dictionary entry from ClickHouse
# =============================================================================================

def _q(text: str) -> str:
    """A YAML-safe scalar for generated files (quotes only when needed)."""
    return yaml.safe_dump(text, default_flow_style=True, allow_unicode=True).strip().removesuffix("\n...").strip()


def bootstrap_table(service: Any, table: str, *, max_values: int = 30, write: bool = True) -> tuple[str, Path | None]:
    """Generate the dictionary entry of `table` from the live schema. Returns (yaml_block, path).

    Filled from ClickHouse: description (table COMMENT), time_column (partition/ORDER BY),
    each column with its COMMENT, and `values` for low-cardinality text columns (their most
    frequent stored values, with the value itself as a placeholder label). Everything a person must
    still decide is marked TODO."""
    service.catalog.refresh(force=True)
    t = service.catalog.resolve(table, service.default_db)
    alias = re.sub(r"[^a-z]", "", t.name.lower())[:1] or "t"
    lines = [f"{t.fqn}:",
             f"  description: {_q(t.comment) if t.comment else 'TODO what one row is, where it comes from'}",
             "  grain: TODO one row per ...",
             f"  alias: {alias}"]
    if t.time_column:
        lines.append(f"  time_column: {t.time_column}")
    lines += ["  # owner: TODO team", "  # refresh: TODO e.g. daily at 06:00", "  caveats: []", "  columns:"]
    for c in t.columns.values():
        lines.append(f"    {c.name}:")
        lines.append(f"      description: {_q(c.comment) if c.comment else 'TODO'}")
        if re.search(r"email|phone|name$|address|iban|ssn", c.name, re.I) and c.is_text:
            lines.append("      pii: true  # TODO confirm (guessed from the name)")
        low_card = c.type.startswith("LowCardinality") or c.base_type.startswith("Enum")
        if low_card and c.is_text:
            try:
                vs = service.resolver.values(t, c)
            except ClickSenseError:
                vs = None
            if vs and vs.values:
                lines.append("      values:  # stored value: label (TODO replace labels with business meanings)")
                for v, _ in vs.values[:max_values]:
                    lines.append(f"        {_q(str(v))}: {_q(str(v))}")
                if len(vs.values) > max_values or not vs.complete:
                    lines.append("      values_complete: false  # more values exist than listed")
                lines.append("      # value_synonyms: {stored_value: [words users say]}")
    block = "\n".join(lines) + "\n"
    if not write:
        return block, None
    path = service.settings.semantic_dir / "dictionary" / f"{t.database}.yaml"
    _append_table(path, t.fqn, block)
    return block, path
