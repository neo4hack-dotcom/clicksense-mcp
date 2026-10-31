"""Static guard: decides whether a statement may run, and bounds it.

This is defence in depth. The real boundary is the ClickHouse account (readonly=2, settings
profile with CONSTRAINTS, quota, grants on the allowed databases only): see dev/clickhouse/users.xml.
What the guard adds is what the account cannot express:

* exactly one statement, and only SELECT / UNION / WITH ... SELECT;
* no SETTINGS / FORMAT / INTO OUTFILE clause: limits and output format belong to the server;
* no table function that reaches outside the database (url, s3, remote, file, mysql, ...);
* no `system` / `information_schema`: schema questions go through the catalog tools;
* database allow-list, table deny-list, PII columns not returned raw;
* a row cap, applied by wrapping (`SELECT * FROM (<sql>) LIMIT n+1`) rather than by
  regenerating the SQL, so the text that runs is the text the model wrote.

Parsing is sqlglot first (offline, gives us an AST for linting). sqlglot does not know every
ClickHouse construct (e.g. `LIMIT n BY x LIMIT m`), so when it fails and the policy allows,
ClickHouse's own parser is asked (`EXPLAIN AST`, parse-only, reads no data). If both fail the
statement is refused.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

import sqlglot
from sqlglot import exp

from .config import Policy
from .errors import Rejected

SYSTEM_DATABASES = {"system", "information_schema"}

# Functions that read from or write to the outside world, or exist only to burn time.
# Matched case-insensitively, in the AST and, as a safety net, in literal-stripped text.
FORBIDDEN_FUNCTIONS = {
    "url", "urlcluster", "s3", "s3cluster", "gcs", "oss", "cosn", "azureblobstorage", "azureblobstoragecluster",
    "hdfs", "hdfscluster", "file", "filecluster", "remote", "remotesecure", "cluster", "clusterallreplicas",
    "mysql", "postgresql", "jdbc", "odbc", "mongodb", "redis", "sqlite", "executable", "input",
    "iceberg", "icebergs3", "icebergazure", "iceberghdfs", "icebergcluster", "deltalake", "hudi",
    "merge", "loop", "generaterandom", "fuzzjson", "fuzzquery", "view", "viewifpermitted",
    "sleep", "sleepeachrow", "ytsaurus", "arrowflight", "kafka", "dictionary",
}

_TEXT_FORBIDDEN = re.compile(
    r"\b(" + "|".join(sorted(FORBIDDEN_FUNCTIONS, key=len, reverse=True)) + r")\s*\(", re.IGNORECASE
)
# Shaped like the clauses, so a `format(...)` call or a column named `settings` passes.
_CLAUSES = re.compile(r"\b(SETTINGS)\s+\w+\s*=|\b(FORMAT)\s+[A-Za-z]\w*\s*$|\b(INTO\s+OUTFILE)\b", re.IGNORECASE)
_STRINGS = re.compile(r"'(?:[^'\\]|\\.|'')*'")
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_QUALIFIED = re.compile(r"\b([A-Za-z_][\w]*)\s*\.\s*([A-Za-z_][\w]*)")


def strip_literals(sql: str) -> str:
    return _STRINGS.sub("''", _COMMENTS.sub(" ", sql))


@dataclass
class GuardResult:
    sql: str  # what will actually run
    original: str
    tables: list[tuple[str, str]]  # (database, table), real tables only
    row_cap: int | None  # the LIMIT we enforce (None: user's own LIMIT was within the cap)
    parser: str  # "sqlglot" | "clickhouse"
    tree: exp.Expression | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def fetch_limit(self) -> int | None:
        return None if self.row_cap is None else self.row_cap + 1


def _func_name(node: exp.Func) -> str:
    if isinstance(node, (exp.Anonymous, exp.AnonymousAggFunc, exp.ParameterizedAgg, exp.CombinedAggFunc,
                         exp.CombinedParameterizedAgg)):
        return str(node.name)
    return node.sql_name()


def _literal_int(node: exp.Expression | None) -> int | None:
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return int(node.this)
        except ValueError:
            return None
    return None


class Guard:
    def __init__(
        self,
        policy: Policy,
        default_database: str,
        *,
        max_sql_chars: int = 20_000,
        pii_columns: Callable[[str, str], set[str]] | None = None,
        server_parse: Callable[[str], str] | None = None,
    ):
        self.policy = policy
        self.default_database = default_database
        self.max_sql_chars = max_sql_chars
        self.pii_columns = pii_columns or (lambda db, t: set())
        self.server_parse = server_parse
        self.allowed_tf = {f.lower() for f in policy.allowed_table_functions}
        self.allowed_dbs = {d.lower() for d in policy.allowed_databases}
        self.denied = {t.lower() for t in policy.denied_tables}

    # ------------------------------------------------------------------ public
    def check(self, sql: str, row_cap: int) -> GuardResult:
        text = self._normalise(sql)
        stripped = strip_literals(text)
        self._text_checks(stripped)
        try:
            statements = [s for s in sqlglot.parse(text, read="clickhouse") if s is not None]
        except Exception as exc:  # sqlglot.errors.ParseError and friends
            if self.policy.server_parse_fallback and self.server_parse is not None:
                return self._check_via_server(text, stripped, row_cap, str(exc).splitlines()[0])
            raise Rejected("parse_error", f"Could not parse SQL: {str(exc).splitlines()[0]}",
                           "Simplify the statement or check the syntax.")
        if len(statements) != 1:
            raise Rejected("multiple_statements", "Exactly one statement per call.",
                           "Send statements one at a time, without ';' separators.")
        tree = statements[0]
        if not isinstance(tree, (exp.Select, exp.SetOperation)):
            kind = type(tree).__name__.upper()
            raise Rejected("not_a_select", f"Only SELECT queries are allowed (got {kind}).",
                           "This server is read-only. Use SELECT, WITH ... SELECT or UNION ALL.")
        for node in tree.walk():
            if isinstance(node, (exp.Insert, exp.Create, exp.Drop, exp.Delete, exp.Update, exp.Alter,
                                 exp.Command, exp.Into, exp.Set, exp.TruncateTable)):
                raise Rejected("not_a_select", "Statement contains a write or DDL clause.",
                               "This server is read-only.")
            if isinstance(node, (exp.Select, exp.SetOperation)):
                if node.args.get("settings"):
                    raise Rejected("settings_clause", "SETTINGS clauses are not allowed.",
                                   "Query limits are set by the server; remove the SETTINGS clause.")
                if node.args.get("format"):
                    raise Rejected("format_clause", "FORMAT clauses are not allowed.",
                                   "Output format is chosen by the server; use the `format` argument instead.")
            if isinstance(node, exp.Func):
                name = _func_name(node).lower()
                if name in FORBIDDEN_FUNCTIONS and not self._is_allowed_table_function(node, name):
                    raise Rejected("forbidden_function", f"Function {name}() is not allowed.",
                                   "External table functions and sleep() are disabled. Query tables directly.")

        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        tables = self._collect_tables(tree, cte_names)
        self._check_pii(tree, tables)
        user_limit = self._top_limit(tree)
        return self._bound(text, tables, row_cap, user_limit, parser="sqlglot", tree=tree)

    # ------------------------------------------------------------------ helpers
    def _normalise(self, sql: str) -> str:
        if not sql or not sql.strip():
            raise Rejected("empty", "Empty SQL.")
        if len(sql) > self.max_sql_chars:
            raise Rejected("too_long", f"SQL longer than {self.max_sql_chars} characters.",
                           "Split the question into smaller queries.")
        text = sql.strip()
        while text.endswith(";"):
            text = text[:-1].rstrip()
        return text

    def _text_checks(self, stripped: str) -> None:
        if ";" in stripped:
            raise Rejected("multiple_statements", "Exactly one statement per call.",
                           "Remove ';' separators and send statements one at a time.")
        m = _CLAUSES.search(stripped)
        if m:
            word = next(g for g in m.groups() if g).split()[0].upper()
            raise Rejected(f"{word.lower()}_clause", f"{word} clauses are not allowed.",
                           "Limits and output format are controlled by the server.")
        for m in _TEXT_FORBIDDEN.finditer(stripped):
            name = m.group(1).lower()
            if name not in self.allowed_tf:
                raise Rejected("forbidden_function", f"Function {name}() is not allowed.",
                               "External table functions and sleep() are disabled. Query tables directly.")

    def _is_allowed_table_function(self, node: exp.Func, name: str) -> bool:
        return name in self.allowed_tf and isinstance(node.parent, exp.Table)

    def _check_table(self, db: str, name: str) -> None:
        dbl = db.lower()
        if dbl in SYSTEM_DATABASES:
            raise Rejected("system_table", f"Direct access to {db}.{name} is not allowed.",
                           "Use list_tables / describe_table / check_freshness / server_status instead.")
        if self.allowed_dbs and dbl not in self.allowed_dbs:
            raise Rejected("database_not_allowed", f"Database '{db}' is not in the allow-list.",
                           f"Allowed databases: {', '.join(sorted(self.allowed_dbs))}.")
        if f"{dbl}.{name.lower()}" in self.denied:
            raise Rejected("table_denied", f"Table {db}.{name} is not available through this server.")

    def _collect_tables(self, tree: exp.Expression, cte_names: set[str]) -> list[tuple[str, str]]:
        seen: list[tuple[str, str]] = []
        for t in tree.find_all(exp.Table):
            if isinstance(t.this, exp.Func):  # table function: checked with the functions above
                name = _func_name(t.this).lower()
                if name not in self.allowed_tf:
                    raise Rejected("forbidden_function", f"Table function {name}() is not allowed.",
                                   f"Allowed table functions: {', '.join(sorted(self.allowed_tf)) or 'none'}.")
                continue
            if not t.name:
                continue
            if not t.db and t.name.lower() in cte_names:
                continue
            db = t.db or self.default_database
            self._check_table(db, t.name)
            key = (db, t.name)
            if key not in seen:
                seen.append(key)
        return seen

    def _check_pii(self, tree: exp.Expression, tables: list[tuple[str, str]]) -> None:
        if self.policy.pii == "allow" or not tables:
            return
        pii: dict[str, str] = {}
        for db, t in tables:
            for col in self.pii_columns(db, t):
                pii[col.lower()] = f"{db}.{t}.{col}"
        if not pii:
            return
        selects = [tree] if isinstance(tree, exp.Select) else list(tree.find_all(exp.Select))
        for sel in selects:
            for proj in sel.expressions:
                if isinstance(proj, exp.Star) or (isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star)):
                    star = proj if isinstance(proj, exp.Star) else proj.this
                    exc = star.args.get("except_") or star.args.get("except")  # key renamed across sqlglot versions
                    if exc:
                        excluded = {e.name.lower() for e in exc}
                        if set(pii) <= excluded:
                            continue
                    raise Rejected("pii_column", "SELECT * would return PII columns: "
                                   + ", ".join(sorted(pii.values())) + ".",
                                   "List the columns you need explicitly, or use SELECT * EXCEPT (...).")
                for col in proj.find_all(exp.Column):
                    if col.name.lower() in pii and not col.find_ancestor(exp.AggFunc):
                        raise Rejected("pii_column", f"Column {pii[col.name.lower()]} is PII and cannot be "
                                       "returned raw.",
                                       "Filter or aggregate on it (count(), uniq()), but do not select it.")

    @staticmethod
    def _top_limit(tree: exp.Expression) -> int | None:
        limit = tree.args.get("limit")
        if isinstance(limit, exp.Limit) and not limit.args.get("expressions"):  # not LIMIT n BY
            return _literal_int(limit.expression)
        return None

    def _bound(self, text: str, tables, row_cap: int, user_limit: int | None, *, parser: str,
               tree: exp.Expression | None) -> GuardResult:
        if user_limit is not None and user_limit <= row_cap:
            return GuardResult(text, text, tables, None, parser, tree)
        wrapped = f"SELECT * FROM (\n{text}\n) LIMIT {row_cap + 1}"
        return GuardResult(wrapped, text, tables, row_cap, parser, tree)

    # ------------------------------------------------------------------ fallback
    def _check_via_server(self, text: str, stripped: str, row_cap: int, sqlglot_error: str) -> GuardResult:
        try:
            ast = self.server_parse(text)  # type: ignore[misc]
        except Exception as exc:
            raise Rejected("parse_error", f"SQL does not parse: {getattr(exc, 'message', exc)}",
                           "Check the syntax against ClickHouse SQL.", sqlglot=sqlglot_error)
        lines = [ln for ln in ast.splitlines() if ln.strip()]
        if not lines or not lines[0].startswith("SelectWithUnionQuery"):
            raise Rejected("not_a_select", "Only SELECT queries are allowed.", "This server is read-only.")
        tables: list[tuple[str, str]] = []
        for ln in lines:
            s = ln.strip()
            if s.startswith("TableIdentifier "):
                ident = s.split()[1]
                db, _, name = ident.rpartition(".")
                db = db or self.default_database
                self._check_table(db, name)
                if (db, name) not in tables:
                    tables.append((db, name))
            elif s.startswith("Function "):
                name = s.split()[1].lower()
                if name in FORBIDDEN_FUNCTIONS and name not in self.allowed_tf:
                    raise Rejected("forbidden_function", f"Function {name}() is not allowed.")
            elif s == "Set" or s.startswith("Set "):
                raise Rejected("settings_clause", "SETTINGS clauses are not allowed.")
        # Unqualified CTE names show up as TableIdentifier too; they resolve in the default
        # database, which is allowed by construction, so they cannot escape the allow-list.
        for m in _QUALIFIED.finditer(stripped):
            if m.group(1).lower() in SYSTEM_DATABASES:
                raise Rejected("system_table", f"Direct access to {m.group(1)}.{m.group(2)} is not allowed.")
        result = self._bound(text, tables, row_cap, None, parser="clickhouse", tree=None)
        result.warnings.append("Parsed by ClickHouse (sqlglot could not): static lint is limited for this query.")
        return result
