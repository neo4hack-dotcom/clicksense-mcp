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

The order of checks in `Guard.check` (each one raises `Rejected` with a hint):
  1. normalise: non-empty, length limit, trailing ';' removed;
  2. text checks on the SQL with string literals and comments blanked out (so a ';' or the word
     SETTINGS inside a string does not count): stacked statements, SETTINGS/FORMAT/INTO OUTFILE,
     forbidden function names - a safety net that works even if the parser misses something;
  3. parse (sqlglot, else ClickHouse) and walk the tree: statement type, clauses, functions;
  4. tables: system databases, allow-list, deny-list (CTE names are not tables);
  5. PII columns in the SELECT list;
  6. the row cap.

HOW TO CHANGE WHAT IS ALLOWED
  * Allow/deny databases or tables, allow a table function: `policy:` in clicksense.yaml, no code.
  * Forbid a new function everywhere: add its lower-case name to FORBIDDEN_FUNCTIONS.
  * A new refusal: raise Rejected("stable_code", "what is wrong", "what to do instead") and add
    an attack case to tests/test_guard.py.
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
# Names listed in policy.allowed_table_functions (default: numbers) are let through, and only
# when used as a table in FROM.
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
# Pieces of SQL blanked out before the text checks: string literals ('it''s', 'a\'b') and comments.
_STRINGS = re.compile(r"'(?:[^'\\]|\\.|'')*'")
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
# "db.table" references, used by the ClickHouse-parser fallback to catch system.* access.
_QUALIFIED = re.compile(r"\b([A-Za-z_][\w]*)\s*\.\s*([A-Za-z_][\w]*)")


def strip_literals(sql: str) -> str:
    """The SQL with comments removed and every string literal replaced by '' (keeps the structure)."""
    return _STRINGS.sub("''", _COMMENTS.sub(" ", sql))


@dataclass
class GuardResult:
    """The verdict for an accepted statement.

    `sql` is what the executor runs; `original` is the caller's text (used for EXPLAIN, which must
    not see our LIMIT wrapper). `tree` is the sqlglot AST, None when ClickHouse parsed it (then the
    advisor in lint.py cannot run its static checks)."""

    sql: str  # what will actually run
    original: str
    tables: list[tuple[str, str]]  # (database, table), real tables only
    row_cap: int | None  # the LIMIT we enforce (None: user's own LIMIT was within the cap)
    parser: str  # "sqlglot" | "clickhouse"
    tree: exp.Expression | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def fetch_limit(self) -> int | None:
        """Rows to fetch: one more than the cap, so the executor can tell 'exactly n' from 'more than n'."""
        return None if self.row_cap is None else self.row_cap + 1


def _func_name(node: exp.Func) -> str:
    """The function name as written. sqlglot keeps unknown functions (most ClickHouse ones, and
    all the dangerous ones) as Anonymous nodes whose name is the text; known ones have a class."""
    if isinstance(node, (exp.Anonymous, exp.AnonymousAggFunc, exp.ParameterizedAgg, exp.CombinedAggFunc,
                         exp.CombinedParameterizedAgg)):
        return str(node.name)
    return node.sql_name()


def _literal_int(node: exp.Expression | None) -> int | None:
    """The value of an integer literal (LIMIT 10 -> 10), else None (LIMIT {n:UInt32} -> None)."""
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return int(node.this)
        except ValueError:
            return None
    return None


class Guard:
    """Stateless checker, built once per server from the policy.

    pii_columns(db, table) -> names of PII columns (from the catalog/dictionary).
    server_parse(sql) -> ClickHouse's `EXPLAIN AST` text, used when sqlglot cannot parse.
    Both are injected so the guard stays testable without a database."""

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
        """Accept (and bound to `row_cap` rows) or raise Rejected. See the module docstring."""
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
        # Walk every node: a write can hide in a subquery or a CTE, a function anywhere.
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

        # `WITH recent AS (...) SELECT ... FROM recent`: `recent` is a CTE, not a table to check.
        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        tables = self._collect_tables(tree, cte_names)
        self._check_pii(tree, tables)
        user_limit = self._top_limit(tree)
        return self._bound(text, tables, row_cap, user_limit, parser="sqlglot", tree=tree)

    # ------------------------------------------------------------------ helpers
    def _normalise(self, sql: str) -> str:
        """Trim, enforce the size limit, drop trailing semicolons (harmless and very common)."""
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
        """Checks on the literal-stripped text: they hold even when no parser understands the SQL."""
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
        """`numbers(10)` is fine in FROM (if allowed by policy), never as a scalar function."""
        return name in self.allowed_tf and isinstance(node.parent, exp.Table)

    def _check_table(self, db: str, name: str) -> None:
        """System databases, then the allow-list, then the deny-list."""
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
        """Every real table the query reads, as (database, table), each checked. Unqualified
        names belong to connection.database."""
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
        """Refuse PII columns in the SELECT list, unless inside an aggregate (count(email) is fine,
        email is not). Filtering on them (WHERE email = ...) is allowed. A `SELECT *` over a table
        with PII is refused unless `* EXCEPT (...)` removes every PII column. Matching is by
        column name across the tables of the query: conservative on purpose."""
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
        """The literal LIMIT of the outermost query, if any (LIMIT n BY x is a per-group limit,
        not a row cap, so it does not count)."""
        limit = tree.args.get("limit")
        if isinstance(limit, exp.Limit) and not limit.args.get("expressions"):  # not LIMIT n BY
            return _literal_int(limit.expression)
        return None

    def _bound(self, text: str, tables, row_cap: int, user_limit: int | None, *, parser: str,
               tree: exp.Expression | None) -> GuardResult:
        # The caller's own LIMIT is already within the cap: run the text untouched.
        """Apply the row cap: keep the caller's LIMIT when it is within the cap, else wrap the query."""
        if user_limit is not None and user_limit <= row_cap:
            return GuardResult(text, text, tables, None, parser, tree)
        # Otherwise wrap rather than rewrite: regenerating SQL from the AST could change its
        # meaning or its {param:Type} placeholders. ClickHouse pushes the outer LIMIT down.
        wrapped = f"SELECT * FROM (\n{text}\n) LIMIT {row_cap + 1}"
        return GuardResult(wrapped, text, tables, row_cap, parser, tree)

    # ------------------------------------------------------------------ fallback
    def _check_via_server(self, text: str, stripped: str, row_cap: int, sqlglot_error: str) -> GuardResult:
        """Fallback when sqlglot cannot parse: ask ClickHouse for the AST (`EXPLAIN AST`, which
        parses without reading data) and apply the same table/function/SETTINGS rules to its
        indented text form:
            SelectWithUnionQuery (children 1)
              ... TableIdentifier shop.orders
              ... Function url (children 1)"""
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
