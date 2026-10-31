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

_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_TOKEN = re.compile(r"[a-z0-9_]+")
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
    for suf in ("ements", "ement", "ations", "ation", "ees", "ies", "es", "ee", "s", "e"):
        if len(tok) > len(suf) + 3 and tok.endswith(suf):
            return tok[: -len(suf)]
    return tok


def tokens(text: str) -> list[str]:
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
    return yaml.load(text, Loader=_Loader)  # noqa: S506 - SafeLoader subclass


def _read_yaml(path: Path) -> Any:
    try:
        return _load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from None


def _read_md(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
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
    kind: str  # table | column | metric | term | knowledge | skill | example
    id: str
    title: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)


class BM25:
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
        out = list(q)
        for t in q:
            if t not in self.vocab and len(t) >= 4:
                out += [v for v in self.vocab if v.startswith(t) or (len(v) >= 4 and t.startswith(v))][:5]
        return out

    def search(self, query: str, kinds: set[str] | None = None, limit: int = 10) -> list[tuple[float, Doc]]:
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
    """Loads semantic/ and keeps it fresh: files are re-read when their mtime changes."""

    def __init__(self, root: Path):
        self.root = root
        self.tables: dict[str, dict[str, Any]] = {}
        self.glossary: list[dict[str, Any]] = []
        self.metrics: dict[str, dict[str, Any]] = {}
        self.examples: list[dict[str, Any]] = []
        self.docs: dict[str, dict[str, Any]] = {}
        self.skills: dict[str, dict[str, Any]] = {}
        self.errors: list[str] = []
        self._stamp: float = -1
        self._checked = 0.0
        self.index = BM25([])
        self.reload()

    # ------------------------------------------------------------------ loading
    def _mtime(self) -> float:
        if not self.root.exists():
            return 0
        return max((p.stat().st_mtime for p in self.root.rglob("*") if p.is_file()), default=0)

    def maybe_reload(self) -> None:
        now = time.monotonic()
        if now - self._checked < 2:
            return
        self._checked = now
        if self._mtime() != self._stamp:
            self.reload()

    def reload(self) -> None:
        self.errors = []
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
        self.tables = tables

        def load_list(name: str, key: str) -> list[dict[str, Any]]:
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
            self.docs[doc_id] = {"id": doc_id, "title": meta.get("title", doc_id), "tags": meta.get("tags", []),
                                 "applies_to": meta.get("applies_to", []), "body": body}
        self.skills = {}
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
            self.skills[name] = {"name": name, "description": meta.get("description", ""),
                                 "when_to_use": meta.get("when_to_use", ""), "body": body}
        self._stamp = self._mtime()
        self.index = BM25(self._documents())

    def _documents(self) -> list[Doc]:
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
        self.maybe_reload()
        return self.index.search(query, kinds, limit)

    def table_spec(self, db: str, table: str) -> dict[str, Any]:
        return self.tables.get(f"{db}.{table}") or {}

    def column_spec(self, db: str, table: str, column: str) -> dict[str, Any]:
        return (self.table_spec(db, table).get("columns") or {}).get(column) or {}

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
