"""The semantic layer on disk, and search over it.

    semantic/
      dictionary/*.yaml   tables, columns, value labels & synonyms, joins, dimensions, caveats
      glossary.yaml       business terms, synonyms, ambiguous senses
      metrics.yaml        governed metric definitions (compiled by query_metric)
      examples.yaml       verified question -> SQL pairs
      knowledge/*.md      knowledge bank: rules, caveats, definitions (YAML front-matter)
      skills/<name>/SKILL.md  playbooks (Agent Skills format: tools, MCP prompts, MCP skills)
      inbox/              suggestions written by the agent, for a human to review

Everything is plain files, versionable and reviewable. Search is BM25 over all of it, with
accent/case folding and light English/French stemming: no embedding service, works air-gapped, and
fast (the corpus is thousands of items, not millions).

LIFE CYCLE OF AN EDIT
  You save a file under semantic/ -> within ~2 seconds the next tool call notices the newer
  modification time (`maybe_reload`) -> everything is re-read and the search index rebuilt.
  No restart. A file with a syntax error is skipped and reported in server_status and by
  `clicksense-admin kb validate`; the rest keeps working.

HOW TO TEACH THE LOADER A NEW KEY
  1. Read it where it is used (e.g. `spec.get("my_key")` in catalog.py or service.py).
  2. Add it to the matching *_KEYS set below, so `kb validate` stops flagging it as a typo.
  3. Document it in the matching template under src/clicksense/templates/.

TUNING THE SEARCH
  * `_STOP`: words ignored by the search (add frequent words of your users' language).
  * `stem()`: crude suffix stripping so "orders"/"order" and "annulées"/"annulé" match.
  * BM25 weights: titles/ids count 3x (a hit on a name beats a hit in prose); k1/b are standard.
  Use `clicksense-admin kb search "a question"` to see what a question retrieves.
"""
from __future__ import annotations

import math
import re
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------------------------
#  The schema of each kind of entry. The loader ignores unknown keys, so a typo ("descripton",
#  "synonym") would make a description or a synonym silently disappear. These sets are what
#  `clicksense-admin kb validate` checks against, and the templates in semantic/_templates/
#  document every one of them. Add a key here when you teach the code to read a new one.
# ---------------------------------------------------------------------------------------------
TABLE_KEYS = {"description", "grain", "alias", "time_column", "owner", "tags", "synonyms", "caveats", "refresh",
              "joins", "dimensions", "columns"}
COLUMN_KEYS = {"description", "unit", "pii", "synonyms", "values", "value_synonyms", "values_complete",
               "enumerable", "caveats", "examples", "format", "type"}
JOIN_KEYS = {"table", "alias", "on", "type", "relationship"}
DIMENSION_KEYS = {"sql", "join", "column", "description"}
METRIC_KEYS = {"name", "label", "description", "table", "expression", "filters", "time_column", "unit", "synonyms",
               "dimensions", "format"}
TERM_KEYS = {"term", "synonyms", "definition", "senses", "maps_to"}
SENSE_KEYS = {"meaning", "maps_to"}
EXAMPLE_KEYS = {"question", "sql", "tags", "notes", "verified_by"}
DOC_KEYS = {"id", "title", "tags", "applies_to"}
SKILL_KEYS = {"name", "description", "when_to_use", "license", "allowed-tools", "metadata"}

# Markdown front-matter: a YAML block between two `---` lines at the top of the file.
_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_TOKEN = re.compile(r"[a-z0-9_]+")
# Words ignored by the search, French and English (question words, articles, "show me"...).
_STOP = set("""
le la les un une des du de d l et ou a au aux en dans par pour sur avec sans ce cet cette ces qui que quoi
est sont etre ont avoir quel quelle quels quelles combien comment pourquoi mon ma mes notre nos votre vos
the a an of to in on for by with and or is are be what which how many much per from as at this that it
show me give list get donne moi montre liste veux
""".split())


def fold(text: str) -> str:
    """Lower-case and strip accents: 'Électroménager' -> 'electromenager'."""
    norm = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in norm if not unicodedata.combining(c)).casefold()


_NONALNUM = re.compile(r"[^0-9a-z]+")


def norm(text: str) -> str:
    """fold() plus punctuation-insensitive: 'Saint-Étienne' == 'saint etienne'."""
    return _NONALNUM.sub(" ", fold(text)).strip()


def stem(tok: str) -> str:
    """Crude FR/EN suffix stripping ("cancellations" -> "cancel", "annulées" -> "annul"). Only
    applied to tokens long enough that the stem stays meaningful."""
    for suf in ("ements", "ement", "ations", "ation", "ees", "ies", "es", "ee", "s", "e"):
        if len(tok) > len(suf) + 3 and tok.endswith(suf):
            return tok[: -len(suf)]
    return tok


def tokens(text: str) -> list[str]:
    """Search tokens: folded, split on non-alphanumerics (snake_case too), stop words removed,
    stemmed. Used both for indexing and for queries, so they always agree."""
    out = []
    for t in _TOKEN.findall(fold(text).replace("_", " ")):
        if t not in _STOP and len(t) > 1:
            out.append(stem(t))
    return out


class _Loader(yaml.SafeLoader):
    """YAML 1.1 reads `on`, `off`, `yes`, `no` as booleans, so a join's `on:` key would become
    True. Only true/false are booleans here."""


_Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:bool"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
                              list("tTfF"))


def _load(text: str) -> Any:
    """Parse YAML with the boolean-safe loader above."""
    return yaml.load(text, Loader=_Loader)  # noqa: S506 - SafeLoader subclass


def _read_yaml(path: Path) -> Any:
    """A YAML file as Python data. utf-8-sig accepts the BOM Notepad adds. Raises ValueError with
    the file name, which the loader records in `errors` instead of crashing."""
    try:
        return _load(path.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from None


def _read_md(path: Path) -> tuple[dict[str, Any], str]:
    """A Markdown file as (front-matter dict, body). No front-matter -> ({}, whole text)."""
    text = path.read_text(encoding="utf-8-sig")
    m = _FRONT.match(text)
    if not m:
        return {}, text.strip()
    try:
        meta = _load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid front-matter: {str(exc).splitlines()[0]}") from None
    return meta, m.group(2).strip()


@dataclass
class Doc:
    """One searchable item. `text` is what is indexed; `payload` is what search results return
    (the metric definition, the term with its senses, the example with its SQL...)."""

    kind: str  # table | column | metric | term | knowledge | skill | example
    id: str
    title: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)


class BM25:
    """Okapi BM25 ranking over Docs, in memory. Rebuilt on every reload (milliseconds for a few
    thousand items). Unknown query words are expanded to vocabulary words sharing a prefix, so a
    word cut by the stemmer differently still matches."""

    def __init__(self, docs: list[Doc], k1: float = 1.4, b: float = 0.7):
        self.docs, self.k1, self.b = docs, k1, b
        self.tf: list[Counter] = []
        df: Counter = Counter()
        for d in docs:
            # Titles and ids count three times: a hit on a name beats a hit in prose.
            c = Counter(tokens(d.text) + tokens(f"{d.title} {d.id}") * 3)
            self.tf.append(c)
            df.update(c.keys())
        self.n = len(docs)
        self.avg = sum(sum(c.values()) for c in self.tf) / max(self.n, 1)
        self.idf = {t: math.log(1 + (self.n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.vocab = set(df)
        self.postings: dict[str, list[int]] = defaultdict(list)
        for i, c in enumerate(self.tf):
            for t in c:
                self.postings[t].append(i)

    def _expand(self, q: list[str]) -> list[str]:
        # Prefix expansion catches "commande" vs "commandes" leftovers and truncated words.
        """Add vocabulary words sharing a 4+ character prefix with unknown query words (at most 5 each)."""
        out = list(q)
        for t in q:
            if t not in self.vocab and len(t) >= 4:
                out += [v for v in self.vocab if v.startswith(t) or (len(v) >= 4 and t.startswith(v))][:5]
        return out

    def search(self, query: str, kinds: set[str] | None = None, limit: int = 10) -> list[tuple[float, Doc]]:
        """Best `limit` docs for `query`, optionally only of some kinds, as (score, doc)."""
        q = self._expand(tokens(query))
        scores: dict[int, float] = defaultdict(float)
        for t in set(q):
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i in self.postings[t]:
                tf = self.tf[i][t]
                dl = sum(self.tf[i].values())
                scores[i] += idf * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * dl / self.avg))
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        out = []
        for i, s in ranked:
            d = self.docs[i]
            if kinds and d.kind not in kinds:
                continue
            out.append((round(s, 3), d))
            if len(out) >= limit:
                break
        return out


class Knowledge:
    """Loads semantic/ and keeps it fresh: files are re-read when their mtime changes.

    Attributes after a load (all plain dicts/lists, exactly as written in the files):
      tables    {"db.table": table spec}        from dictionary/*.yaml
      metrics   {"name": metric spec}           from metrics.yaml
      glossary  [term spec, ...]                from glossary.yaml
      examples  [example spec, ...]             from examples.yaml
      docs      {"id": {id, title, tags, applies_to, body}}   from knowledge/*.md
      skills    {"name": {name, description, when_to_use, body}} from skills/<name>/SKILL.md
      errors    ["file: problem", ...]          files that could not be loaded
      sources   {"table:db.t" | "doc:id" | "skill:name": Path}  for error messages
    """

    def __init__(self, root: Path):
        self.root = root
        self.tables: dict[str, dict[str, Any]] = {}
        self.glossary: list[dict[str, Any]] = []
        self.metrics: dict[str, dict[str, Any]] = {}
        self.examples: list[dict[str, Any]] = []
        self.docs: dict[str, dict[str, Any]] = {}
        self.skills: dict[str, dict[str, Any]] = {}
        self.errors: list[str] = []
        # Where each entry came from, for error messages (`kb validate`): table -> file, etc.
        self.sources: dict[str, Path] = {}
        self._stamp: float = -1
        self._checked = 0.0
        self.index = BM25([])
        self.reload()

    # ------------------------------------------------------------------ loading
    def _mtime(self) -> float:
        """Newest modification time under semantic/: the cheap change detector."""
        if not self.root.exists():
            return 0
        return max((p.stat().st_mtime for p in self.root.rglob("*") if p.is_file()), default=0)

    def maybe_reload(self) -> None:
        """Reload if a file changed. Checks at most every 2 seconds (a directory walk per tool
        call would be wasteful on a big semantic layer)."""
        now = time.monotonic()
        if now - self._checked < 2:
            return
        self._checked = now
        if self._mtime() != self._stamp:
            self.reload()

    def reload(self) -> None:
        """Read every file again and rebuild the search index. Broken files are skipped and listed
        in `errors`: one bad file must not take the whole semantic layer down."""
        self.errors = []
        self.sources = {}
        tables: dict[str, dict[str, Any]] = {}
        for path in sorted((self.root / "dictionary").glob("*.y*ml")):
            try:
                data = _read_yaml(path)
            except ValueError as exc:
                self.errors.append(str(exc))
                continue
            for name, spec in (data.get("tables") or {}).items():
                spec = dict(spec or {})
                spec.setdefault("columns", {})
                spec["columns"] = {k: dict(v or {}) for k, v in spec["columns"].items()}
                tables[name] = spec
                self.sources[f"table:{name}"] = path
        self.tables = tables

        def load_list(name: str, key: str) -> list[dict[str, Any]]:
            """The list under `key` in a top-level YAML file, or [] (errors recorded, never raised)."""
            path = self.root / name
            if not path.exists():
                return []
            try:
                return list(_read_yaml(path).get(key) or [])
            except ValueError as exc:
                self.errors.append(str(exc))
                return []

        self.glossary = load_list("glossary.yaml", "terms")
        self.metrics = {m["name"]: m for m in load_list("metrics.yaml", "metrics") if "name" in m}
        self.examples = load_list("examples.yaml", "examples")

        self.docs = {}
        for path in sorted((self.root / "knowledge").glob("*.md")):
            try:
                meta, body = _read_md(path)
            except ValueError as exc:
                self.errors.append(str(exc))
                continue
            doc_id = str(meta.get("id") or path.stem)
            self.sources[f"doc:{doc_id}"] = path
            self.docs[doc_id] = {"id": doc_id, "title": meta.get("title", doc_id), "tags": meta.get("tags", []),
                                 "applies_to": meta.get("applies_to", []), "body": body}
        self.skills = {}
        self.skills_meta: dict[str, dict[str, Any]] = {}  # raw front-matter, for validation
        # Agent Skills layout (skills/<name>/SKILL.md), the format Claude and FastMCP's skills
        # provider read; flat skills/*.md files are accepted too.
        skill_files = sorted((self.root / "skills").glob("*/SKILL.md")) + sorted((self.root / "skills").glob("*.md"))
        for path in skill_files:
            try:
                meta, body = _read_md(path)
            except ValueError as exc:
                self.errors.append(str(exc))
                continue
            name = str(meta.get("name") or (path.parent.name if path.name == "SKILL.md" else path.stem))
            self.sources[f"skill:{name}"] = path
            self.skills_meta[name] = meta
            self.skills[name] = {"name": name, "description": meta.get("description", ""),
                                 "when_to_use": meta.get("when_to_use", ""), "body": body}
        self._stamp = self._mtime()
        self.index = BM25(self._documents())

    def _documents(self) -> list[Doc]:
        """Everything searchable, one Doc per table, column, metric, term, document, skill and
        example. What goes into `text` decides what a question can match: descriptions,
        synonyms, value labels and value synonyms are all indexed."""
        docs: list[Doc] = []
        for tname, t in self.tables.items():
            docs.append(Doc("table", tname, tname, " ".join(
                [t.get("description", ""), t.get("grain", ""), " ".join(t.get("synonyms", [])),
                 " ".join(t.get("tags", [])), " ".join(t.get("caveats", []))]), {"table": tname}))
            for cname, c in t["columns"].items():
                vals = c.get("values") or {}
                vsyn = c.get("value_synonyms") or {}
                text = " ".join([
                    c.get("description", ""), " ".join(c.get("synonyms", [])),
                    " ".join(f"{k} {v}" for k, v in vals.items()),
                    " ".join(f"{k} {' '.join(v)}" for k, v in vsyn.items()),
                ])
                docs.append(Doc("column", f"{tname}.{cname}", cname, text, {"table": tname, "column": cname}))
        for m in self.metrics.values():
            docs.append(Doc("metric", m["name"], m.get("label", m["name"]), " ".join(
                [m.get("description", ""), " ".join(m.get("synonyms", [])), m.get("table", "")]), m))
        for i, term in enumerate(self.glossary):
            senses = " ".join(s.get("meaning", "") for s in term.get("senses", []))
            docs.append(Doc("term", term.get("term", f"term{i}"), term.get("term", ""), " ".join(
                [term.get("definition", ""), " ".join(term.get("synonyms", [])), senses]), term))
        for d in self.docs.values():
            docs.append(Doc("knowledge", d["id"], d["title"], f"{' '.join(d['tags'])} {d['body']}", d))
        for s in self.skills.values():
            docs.append(Doc("skill", s["name"], s["name"],
                            f"{s['description']} {s['when_to_use']} {s['when_to_use']} {s['body']}", s))
        for i, ex in enumerate(self.examples):
            docs.append(Doc("example", f"example-{i + 1}", ex.get("question", ""),
                            " ".join([ex.get("question", ""), " ".join(ex.get("tags", []))]), ex))
        return docs

    # ------------------------------------------------------------------ lookups
    def search(self, query: str, kinds: set[str] | None = None, limit: int = 10) -> list[tuple[float, Doc]]:
        """Search the semantic layer (reloading it first if a file changed)."""
        self.maybe_reload()
        return self.index.search(query, kinds, limit)

    def table_spec(self, db: str, table: str) -> dict[str, Any]:
        """The dictionary entry of a table, or {} if it is not documented."""
        return self.tables.get(f"{db}.{table}") or {}

    def column_spec(self, db: str, table: str, column: str) -> dict[str, Any]:
        """The dictionary entry of a column, or {}."""
        return (self.table_spec(db, table).get("columns") or {}).get(column) or {}

    def find_metrics(self, text: str) -> list[str]:
        """Metrics whose name, label or a synonym appears word-for-word in `text` (longest phrase
        first): "net sales by channel" names net_revenue even if "sales" alone is ambiguous."""
        self.maybe_reload()
        folded = f" {' '.join(_TOKEN.findall(fold(text).replace('_', ' ')))} "
        hits: list[tuple[int, str]] = []
        for m in self.metrics.values():
            names = [m["name"].replace("_", " "), m.get("label", "")] + list(m.get("synonyms", []))
            best = max((len(fn) for n in names if (fn := " ".join(_TOKEN.findall(fold(n))))
                        and f" {fn} " in folded), default=0)
            if best:
                hits.append((best, m["name"]))
        return [name for _, name in sorted(hits, key=lambda h: -h[0])]

    def find_terms(self, text: str) -> list[dict[str, Any]]:
        """Glossary terms literally present in `text` (word-boundary match on folded text)."""
        folded = f" {' '.join(_TOKEN.findall(fold(text)))} "
        hits = []
        for term in self.glossary:
            names = [term.get("term", "")] + list(term.get("synonyms", []))
            for n in names:
                fn = " ".join(_TOKEN.findall(fold(n)))
                if fn and f" {fn} " in folded:
                    hits.append({**term, "matched": n})
                    break
        return hits
