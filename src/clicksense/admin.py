"""`clicksense-admin`: maintain the configuration and the semantic layer from the command line.

    clicksense-admin config --example        print a fully commented clicksense.yaml (all defaults)
    clicksense-admin config --show           the effective configuration (secrets masked)

    clicksense-admin kb validate [--live]    lint semantic/ (--live: also check against ClickHouse)
    clicksense-admin kb new KIND NAME        add a metric/term/example/knowledge/skill/table from a template
    clicksense-admin kb bootstrap DB.TABLE   document a table from ClickHouse (columns, comments, values)
    clicksense-admin kb search "question"    what get_context finds for a question (test your synonyms)
    clicksense-admin kb resolve TABLE COLUMN WORD...   how words map to stored values
    clicksense-admin kb inbox                suggestions the agent left for review

Every command accepts --config (default: $CLICKSENSE_CONFIG, else ./clicksense.yaml). Nothing here
needs a restart: the server reloads semantic/ files within seconds of a change. Configuration
changes (clicksense.yaml, .env) do need a restart.
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings

from .config import ConfigError, Settings, load_settings, render_yaml
from .errors import ClickSenseError


def _settings(args) -> Settings:
    """Load the configuration or exit with the error message (status 2)."""
    try:
        return load_settings(args.config)
    except ConfigError as exc:
        print(f"clicksense-admin: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _service(settings: Settings):
    """The full ClickSense service (needed for live checks, bootstrap, search, resolve)."""
    from .service import ClickSense

    return ClickSense(settings)


# ------------------------------------------------------------------------------------- config
def cmd_config(args) -> int:
    """config --example (default) or --show."""
    if args.show:
        print(render_yaml(_settings(args), example=False), end="")
    else:
        print(render_yaml(example=True), end="")
    return 0


# ------------------------------------------------------------------------------------- kb
def cmd_validate(args) -> int:
    """kb validate [--live]: print every finding; exit status 1 if there is any error (usable in CI)."""
    from .kbtools import validate

    settings = _settings(args)
    service = catalog = None
    if args.live:
        service = _service(settings)
        service.catalog.refresh(force=True)
        if service.catalog.error:
            print(f"ClickHouse not reachable ({service.catalog.error}): offline checks only.\n")
            service = None
        else:
            catalog = service.catalog
    from .knowledge import Knowledge

    findings = validate(Knowledge(settings.semantic_dir), catalog, service)
    for f in findings:
        print(f.line())
    errors = sum(f.level == "error" for f in findings)
    warns = sum(f.level == "warning" for f in findings)
    print(f"\n{errors} error(s), {warns} warning(s) in {settings.semantic_dir}"
          + ("" if args.live else "  (add --live to also check tables, columns and SQL against ClickHouse)"))
    return 1 if errors else 0


def cmd_new(args) -> int:
    """kb new KIND NAME: scaffold from a template (a table is bootstrapped from ClickHouse unless --empty)."""
    from .kbtools import scaffold

    settings = _settings(args)
    if args.kind == "table" and not args.empty:
        args.table, args.print_only = args.name, False
        return cmd_bootstrap(args)
    path = scaffold(settings.semantic_dir, args.kind, args.name)
    print(f"Created a {args.kind} entry in {path}\nNext: fill the TODOs, then run `clicksense-admin kb validate --live`.")
    return 0


def cmd_bootstrap(args) -> int:
    """kb bootstrap DB.TABLE [--print-only]: document a table from ClickHouse."""
    from .kbtools import bootstrap_table

    service = _service(_settings(args))
    block, path = bootstrap_table(service, args.table, write=not args.print_only)
    if path is None:
        print(block, end="")
    else:
        print(f"Documented {args.table} in {path} from ClickHouse.\n"
              "Next: replace the TODOs and placeholder labels with business meanings, then run "
              "`clicksense-admin kb validate --live`.")
    return 0


def cmd_search(args) -> int:
    """kb search QUESTION [--json]: what get_context returns, as a readable summary."""
    ctx = _service(_settings(args)).get_context(args.question)
    if args.json:
        print(json.dumps(ctx, ensure_ascii=False, indent=2, default=str))
        return 0

    def section(title, items):
        """Print a titled bullet list if it has items."""
        if items:
            print(f"\n{title}")
            for it in items:
                print(f"  - {it}")

    print(f"Question: {args.question}")
    section("Tables (ranked)", [t["table"] + (f" — {t['description']}" if t.get("description") else "")
                                for t in ctx.get("tables", [])])
    section("Metrics", [f"{m['name']}: {m.get('description', '')}" for m in ctx.get("metrics", [])])
    section("Glossary", [f"{g['term']}: {g.get('definition', '')}" for g in ctx.get("glossary", [])])
    section("Values recognised", [f"'{v['term']}' -> " + ", ".join(f"{m['table']}.{m['column']} = {m['value']!r}"
                                                                    for m in v["matches"])
                                  for v in ctx.get("values_spotted", [])])
    section("Ambiguities (the agent will ask)", [f"{a['term']} ({a['kind']})" for a in ctx.get("ambiguities", [])])
    section("Examples", [e["question"] for e in ctx.get("examples", [])])
    section("Knowledge", [f"{d['id']}: {d['title']}" for d in ctx.get("knowledge", [])])
    section("Skills", [s["name"] for s in ctx.get("skills", [])])
    print("\nNot what you expected? Add synonyms (dictionary, metrics, glossary) and run this again: "
          "no restart needed.")
    return 0


def cmd_resolve(args) -> int:
    """kb resolve TABLE COLUMN WORD...: one line per word with status, value and candidates."""
    res = _service(_settings(args)).resolve_values(args.table, args.column, args.words)
    for r in res["results"]:
        cands = ", ".join(f"{c['value']!r} ({c['match']}, {c['score']})" for c in r["candidates"][:4])
        print(f"{r['term']!r:24} {r['status']:10} {('-> ' + repr(r['value'])) if 'value' in r else ''}  {cands}")
    return 0


def cmd_inbox(args) -> int:
    """kb inbox: print the agent's pending suggestions."""
    inbox = _settings(args).semantic_dir / "inbox"
    files = sorted(inbox.glob("*.md"))
    if not files:
        print("Inbox is empty.")
        return 0
    for f in files:
        print(f"\n=== {f.name}\n{f.read_text(encoding='utf-8').strip()}")
    print(f"\n{len(files)} suggestion(s). To accept one: copy its content into the right file "
          "(clicksense-admin kb new ... helps), run kb validate, then delete it from inbox/.")
    return 0


# ------------------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: config, kb {validate,new,bootstrap,search,resolve,inbox}."""
    ap = argparse.ArgumentParser(prog="clicksense-admin", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--config", help="clicksense.yaml (default: $CLICKSENSE_CONFIG or ./clicksense.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("config", help="print the commented example configuration, or the effective one")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--example", action="store_true", help="all keys with defaults and comments (default)")
    g.add_argument("--show", action="store_true", help="effective values after .env and environment, secrets masked")
    c.set_defaults(func=cmd_config)

    kb = sub.add_parser("kb", help="maintain the semantic layer (knowledge bank, metrics, skills...)")
    ks = kb.add_subparsers(dest="kbcmd", required=True)
    v = ks.add_parser("validate", help="lint every file of semantic/")
    v.add_argument("--live", action="store_true", help="also check tables, columns and SQL against ClickHouse")
    v.set_defaults(func=cmd_validate)
    n = ks.add_parser("new", help="add an entry from a commented template")
    n.add_argument("kind", choices=["table", "metric", "term", "example", "knowledge", "skill"])
    n.add_argument("name", help="metric: snake_case; knowledge/skill: file name; term: the word; "
                                "example: the question; table: db.table")
    n.add_argument("--empty", action="store_true", help="table: empty template instead of reading ClickHouse")
    n.set_defaults(func=cmd_new)
    b = ks.add_parser("bootstrap", help="document a table from ClickHouse")
    b.add_argument("table", help="database.table")
    b.add_argument("--print-only", action="store_true", help="print the YAML instead of writing it")
    b.set_defaults(func=cmd_bootstrap)
    s = ks.add_parser("search", help="what get_context returns for a question")
    s.add_argument("question")
    s.add_argument("--json", action="store_true", help="the raw get_context payload")
    s.set_defaults(func=cmd_search)
    r = ks.add_parser("resolve", help="how words map to stored values of a column")
    r.add_argument("table")
    r.add_argument("column")
    r.add_argument("words", nargs="+")
    r.set_defaults(func=cmd_resolve)
    i = ks.add_parser("inbox", help="suggestions the agent left for review")
    i.set_defaults(func=cmd_inbox)
    return ap


def main(argv: list[str] | None = None) -> None:
    """Entry point of `clicksense-admin`. ClickSenseError -> message + hint on stderr, exit status 1."""
    warnings.filterwarnings("ignore")
    import logging

    logging.getLogger("clickhouse_connect").setLevel(logging.CRITICAL)  # errors are reported as findings
    args = build_parser().parse_args(argv)
    try:
        code = args.func(args)
    except ClickSenseError as exc:
        print(f"clicksense-admin: {exc.message}" + (f"\n  -> {exc.hint}" if exc.hint else ""), file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
