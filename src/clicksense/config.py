"""Settings: one YAML file, `${VAR}` / `${VAR:-default}` resolved from the environment.

Search order when no path is given: $CLICKSENSE_CONFIG, ./clicksense.yaml, then pure defaults.
A handful of connection fields can also be overridden by env (CLICKSENSE_HOST, ...), which is
what you want in a container.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(RuntimeError):
    pass


class Connection(BaseModel):
    host: str = "localhost"
    port: int = 8123
    username: str = "default"
    password: str = ""
    secure: bool = False
    database: str = "default"
    connect_timeout: float = 5
    send_receive_timeout: float = 90
    pool_size: int = 8
    compress: str | bool = "lz4"


class Limits(BaseModel):
    """Per-query ceilings sent with EVERY query. They can only tighten the account profile."""

    default_rows: int = 200
    hard_max_rows: int = 5000
    max_response_bytes: int = 200_000
    max_sql_chars: int = 20_000

    max_execution_time: int = 20
    max_memory_usage: int = 1_000_000_000
    max_threads: int = 4
    max_rows_to_read: int = 1_000_000_000
    max_bytes_to_read: int = 100_000_000_000
    max_bytes_before_external_group_by: int = 500_000_000
    max_bytes_before_external_sort: int = 500_000_000

    # Pre-flight: EXPLAIN ESTIMATE before running. Refuse when the index analysis says the
    # query would read more than this many rows. Cheap (no data read) and catches full scans.
    preflight: bool = True
    preflight_max_rows: int = 300_000_000

    # Admission control, client side: protects the DB from a burst of agent calls.
    max_concurrent: int = 4
    queue_timeout_s: float = 15
    rate_per_minute: int = 240

    # Circuit breaker: after N consecutive *infrastructure* failures (timeouts, memory,
    # too many queries, network), stop sending queries for `breaker_cooldown_s`.
    breaker_failures: int = 4
    breaker_cooldown_s: float = 30

    result_cache_ttl_s: float = 120
    result_cache_entries: int = 256
    use_query_cache: bool = True
    query_cache_ttl_s: int = 300


class Policy(BaseModel):
    allowed_databases: list[str] = Field(default_factory=list)  # empty = every non-system db
    denied_tables: list[str] = Field(default_factory=list)  # "db.table"
    allowed_table_functions: list[str] = Field(default_factory=lambda: ["numbers"])
    # "block": a PII column (dictionary `pii: true`) may be filtered/aggregated but not
    # returned raw. "allow": no restriction.
    pii: Literal["block", "allow"] = "block"
    # When sqlglot cannot parse valid ClickHouse SQL, fall back to ClickHouse's own parser
    # (EXPLAIN AST, parse only, no data read) rather than refusing the query.
    server_parse_fallback: bool = True


class LLM(BaseModel):
    """Any OpenAI-compatible chat-completions endpoint, for `clicksense-chat` (OpenAI, Azure OpenAI,
    Mistral, Groq, DeepSeek, OpenRouter, vLLM, Ollama, LM Studio...). The key is read from
    `api_key`, else $CLICKSENSE_LLM_API_KEY, else $OPENAI_API_KEY: keep it out of the YAML file."""

    base_url: str | None = None  # None: $OPENAI_BASE_URL, else https://api.openai.com/v1
    api_key: str | None = None
    model: str = "gpt-4.1-mini"
    temperature: float | None = None  # None: provider default (some reasoning models reject it)
    max_steps: int = 16  # tool-call rounds per question
    # Call get_context before the model's first turn, so ambiguities and the right tables are in
    # front of it whatever its tool-use habits. Weaker models skip "call X first" instructions.
    auto_context: bool = True
    max_tool_chars: int = 12_000  # a tool result larger than this is truncated before the model sees it
    timeout_s: float = 120
    extra_headers: dict[str, str] = Field(default_factory=dict)
    mcp_url: str | None = None  # None: run ClickSense in-process; else e.g. http://127.0.0.1:3110/mcp


class Settings(BaseModel):
    connection: Connection = Field(default_factory=Connection)
    limits: Limits = Field(default_factory=Limits)
    policy: Policy = Field(default_factory=Policy)
    llm: LLM = Field(default_factory=LLM)
    semantic_dir: Path = Path("semantic")
    audit_path: Path | None = Path("audit/clicksense.jsonl")
    catalog_ttl_s: float = 600
    value_cache_ttl_s: float = 3600
    # Value enumeration for disambiguation: up to this many distinct values are cached per
    # column; above that the column is "high cardinality" and searched live instead.
    value_cache_max_distinct: int = 2000
    value_scan_max_rows: int = 50_000_000

    @field_validator("semantic_dir", "audit_path", mode="before")
    @classmethod
    def _expand(cls, v):
        return None if v in (None, "", "none") else Path(os.path.expanduser(str(v)))


def _subst(value):
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"environment variable {name} is not set (referenced in config)")
        return _VAR.sub(repl, value)
    if isinstance(value, dict):
        return {k: _subst(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v) for v in value]
    return value


_ENV_OVERRIDES = {
    "CLICKSENSE_HOST": "host",
    "CLICKSENSE_PORT": "port",
    "CLICKSENSE_USER": "username",
    "CLICKSENSE_PASSWORD": "password",
    "CLICKSENSE_DATABASE": "database",
    "CLICKSENSE_SECURE": "secure",
}


def load_settings(path: str | Path | None = None) -> Settings:
    candidate = path or os.environ.get("CLICKSENSE_CONFIG")
    if candidate is None and Path("clicksense.yaml").exists():
        candidate = "clicksense.yaml"
    raw: dict = {}
    base = Path.cwd()
    if candidate:
        p = Path(candidate).expanduser()
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
        raw = _subst(yaml.safe_load(p.read_text()) or {})
        base = p.resolve().parent
    conn = raw.setdefault("connection", {})
    for env, key in _ENV_OVERRIDES.items():
        if env in os.environ:
            conn[key] = os.environ[env]
    settings = Settings.model_validate(raw)
    # Relative paths are relative to the config file, not to wherever the client spawned us.
    if not settings.semantic_dir.is_absolute():
        settings.semantic_dir = base / settings.semantic_dir
    if settings.audit_path and not settings.audit_path.is_absolute():
        settings.audit_path = base / settings.audit_path
    if settings.connection.database.lower() in {"system", "information_schema"}:
        raise ConfigError("connection.database must not be a system database")
    return settings
