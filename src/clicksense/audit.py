"""Append-only JSONL audit: one line per tool call that touched ClickHouse or was refused.

Parameter *values* are not logged (they are often the sensitive part: names, emails); their
keys are. Pair the `query_id` with system.query_log on the server for the full picture.

One line looks like:
  {"ts": "2026-10-31T23:07:22+0100", "tool": "run_query", "status": "ok", "sql": "SELECT ...",
   "params": ["d"], "query_id": "cs-...", "rows": 3, "read_rows": 585192, "cached": false, "ms": 13.1}

status is "ok", "refused" (the guard or the pre-flight said no) or "error" (ClickHouse failed).
The file grows forever: rotate it with your usual tooling (logrotate, a scheduled task) or set
`audit_path:` to empty in clicksense.yaml to disable it.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class Audit:
    """Thread-safe JSONL writer. path=None disables auditing (every call becomes a no-op)."""

    def __init__(self, path: Path | None, max_sql_chars: int = 4000):
        self.path = path
        self.max_sql = max_sql_chars
        self._lock = threading.Lock()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, tool: str, status: str, **fields: Any) -> None:
        """Append one record. None values are dropped; `sql` is truncated; `params` keeps only
        the parameter names (never the values)."""
        if not self.path:
            return
        rec: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": tool, "status": status}
        for k, v in fields.items():
            if v is None:
                continue
            if k == "sql":
                v = str(v)[: self.max_sql]
            if k == "params":
                v = sorted(v) if isinstance(v, dict) else v
            rec[k] = v
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
