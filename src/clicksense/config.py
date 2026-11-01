"""Settings: every tunable parameter of ClickSense, in one place.

HOW SETTINGS ARE RESOLVED (later wins)
--------------------------------------
1. The defaults declared in the models below.
2. `clicksense.yaml` (path from --config, else $CLICKSENSE_CONFIG, else ./clicksense.yaml).
   `${VAR}` and `${VAR:-default}` inside the YAML are replaced from the environment.
3. Environment variables named in a field's `env=` (e.g. CLICKSENSE_HOST): they override the YAML,
   which is what you want in a container or a Windows service.
4. `.env` files are read first (next to the config, then the working directory) and only fill
   variables that are NOT already set: a real environment variable always beats the file.

HOW TO ADD OR CHANGE A SETTING
------------------------------
* Add a field to the right model with `_f(default, "description", env="OPTIONAL_ENV_VAR")`.
  That is all: the env override, `clicksense-admin config --example` (the commented example file) and
  `clicksense-admin config --show` (the effective values) all read the field's metadata.
* Then regenerate the example file so it stays in sync (a test checks it):
      clicksense-admin config --example > clicksense.example.yaml
* Read the setting where you need it as `settings.<section>.<name>`.

Secrets (passwords, API keys, HTTP tokens) belong in `.env` or the environment, never in the YAML
file that is copied around; `config --show` masks them.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

# `${NAME}` or `${NAME:-default}` inside YAML values.
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# Field names whose values are masked by `config --show`.
SECRET_FIELDS = {"password", "api_key", "tokens", "mcp_token"}


class ConfigError(RuntimeError):
    """Invalid or incomplete configuration. The message says what to fix; the server exits."""


def _f(default: Any = ..., description: str = "", *, env: str | None = None, factory=None) -> Any:
    """A documented setting.

    `description` is shown in the generated example file and in `config --show`; `env` names the
    environment variable that overrides it. Keep descriptions short, concrete and in the
    imperative ("Lower it if...") — they are the first documentation an operator reads.
    """
    extra = {"env": env} if env else None
    if factory is not None:
        return Field(default_factory=factory, description=description, json_schema_extra=extra)
    return Field(default, description=description, json_schema_extra=extra)


# =============================================================================================
#  Sections. Each class is one top-level key of clicksense.yaml.
# =============================================================================================

class Connection(BaseModel):
    """How to reach ClickHouse. Use the read-only account from dev/production-account.sql."""

    host: str = _f("localhost", "ClickHouse host name or IP.", env="CLICKSENSE_HOST")
    port: int = _f(8123, "ClickHouse HTTP port (8123, or 8443 with secure: true). Not the native port 9000.",
                   env="CLICKSENSE_PORT")
    username: str = _f("default", "ClickHouse user. Must be a read-only account (readonly = 2).",
                       env="CLICKSENSE_USER")
    password: str = _f("", "Password of that user. Put it in .env, not in this file.", env="CLICKSENSE_PASSWORD")
    secure: bool = _f(False, "Use HTTPS to talk to ClickHouse.", env="CLICKSENSE_SECURE")
    database: str = _f("default", "Database used for unqualified table names (e.g. `orders` -> `shop.orders`).",
                       env="CLICKSENSE_DATABASE")
    connect_timeout: float = _f(5, "Seconds to wait for a TCP connection before giving up.")
    send_receive_timeout: float = _f(90, "Seconds to wait for a response. Keep it above limits.max_execution_time.")
    pool_size: int = _f(8, "HTTP connections kept open to ClickHouse. Keep it >= limits.max_concurrent.")
    compress: str | bool = _f("lz4", "Transport compression: lz4 (fast), zstd, gzip, or false.")


class Limits(BaseModel):
    """Guard-rails sent with EVERY query. They can only tighten the ClickHouse account profile:
    ClickHouse rejects a value above a profile CONSTRAINT (error setting_constraint)."""

    # --- rows returned to the model ---
    default_rows: int = _f(200, "Rows returned when the caller does not ask for a number.")
    hard_max_rows: int = _f(5000, "Maximum rows a caller may request. Larger answers must be aggregated.")
    max_response_bytes: int = _f(200_000, "Size budget of a result payload; beyond it the result is truncated "
                                          "and says so.")
    max_sql_chars: int = _f(20_000, "Longest SQL statement accepted.")

    # --- per-query resource limits (sent as ClickHouse settings) ---
    max_execution_time: int = _f(20, "Seconds before ClickHouse aborts a query (error timeout).")
    max_memory_usage: int = _f(1_000_000_000, "Bytes of memory one query may use (error memory_limit).")
    max_threads: int = _f(4, "CPU threads one query may use. Lower it on a busy cluster.")
    max_rows_to_read: int = _f(1_000_000_000, "Rows one query may read from disk.")
    max_bytes_to_read: int = _f(100_000_000_000, "Bytes one query may read from disk.")
    max_bytes_before_external_group_by: int = _f(500_000_000, "Above this, GROUP BY spills to disk instead of "
                                                              "failing on memory.")
    max_bytes_before_external_sort: int = _f(500_000_000, "Above this, ORDER BY spills to disk.")

    # --- pre-flight cost check ---
    preflight: bool = _f(True, "Run EXPLAIN ESTIMATE (index analysis, no data read) before queries on big tables.")
    preflight_max_rows: int = _f(300_000_000, "Refuse a query whose estimate exceeds this many rows "
                                              "(error too_expensive). Raise it if legitimate queries are refused.")

    # --- admission control: protects ClickHouse from a burst of agent calls ---
    max_concurrent: int = _f(4, "Queries in flight at the same time from this server.")
    queue_timeout_s: float = _f(15, "Seconds a query waits for a free slot before failing with `busy`.")
    rate_per_minute: int = _f(240, "Queries per minute (token bucket). Beyond it: rate_limited.")

    # --- circuit breaker ---
    breaker_failures: int = _f(4, "Consecutive load failures (timeout, memory, network...) that pause all queries. "
                                  "Bad SQL does not count.")
    breaker_cooldown_s: float = _f(30, "Seconds the pause lasts.")

    # --- caches ---
    result_cache_ttl_s: float = _f(120, "Seconds an identical query is answered from memory. 0 disables.")
    result_cache_entries: int = _f(256, "Results kept in the in-memory cache.")
    use_query_cache: bool = _f(True, "Use ClickHouse's own query cache (ClickHouse 23.1+).")
    query_cache_ttl_s: int = _f(300, "Seconds a result stays in ClickHouse's query cache.")


class Policy(BaseModel):
    """What the guard accepts. Defence in depth: the real boundary is the ClickHouse account."""

    allowed_databases: list[str] = _f(description="Databases the agent may query. Empty = every non-system "
                                                  "database the account can see.", factory=list)
    denied_tables: list[str] = _f(description="Tables hidden and refused, as \"db.table\".", factory=list)
    allowed_table_functions: list[str] = _f(description="Table functions allowed in FROM. External ones (url, s3, "
                                                        "remote, file, mysql...) are always refused.",
                                            factory=lambda: ["numbers"])
    pii: Literal["block", "allow"] = _f("block", "block: columns marked `pii: true` in the dictionary can be filtered "
                                                 "or aggregated but never returned raw. allow: no restriction.")
    server_parse_fallback: bool = _f(True, "When sqlglot cannot parse valid ClickHouse SQL, ask ClickHouse's own "
                                           "parser (EXPLAIN AST, reads no data) instead of refusing.")


class Http(BaseModel):
    """The streamable-HTTP transport (`clicksense --transport http`, serve-windows.bat).

    Listening on anything but loopback requires at least one bearer token: an MCP endpoint on the
    network without authentication would hand the warehouse to anyone who can reach the port."""

    host: str = _f("127.0.0.1", "Bind address. 127.0.0.1 = this machine only; 0.0.0.0 = the network "
                                "(tokens then required).", env="CLICKSENSE_HTTP_HOST")
    port: int = _f(3110, "HTTP port. The MCP endpoint is http://<host>:<port>/mcp.", env="CLICKSENSE_HTTP_PORT")
    tokens: list[str] = _f(description="Accepted bearer tokens, at least 24 characters each. In the environment: "
                                       "comma-separated. Generate one with `clicksense --gen-token`.",
                           env="CLICKSENSE_HTTP_TOKENS", factory=list)
    allow_unauthenticated_remote: bool = _f(False, "Allow listening on the network WITHOUT a token. Leave false.")

    @field_validator("tokens", mode="before")
    @classmethod
    def _split(cls, v):
        # From the environment the value is one string "tok1, tok2"; from YAML it can be a list.
        if isinstance(v, str):
            v = v.split(",")
        return [t.strip() for t in (v or []) if t and t.strip()]


class LLM(BaseModel):
    """The OpenAI-compatible model used by `clicksense-chat` (OpenAI, Azure OpenAI, Mistral, Groq,
    DeepSeek, OpenRouter, vLLM, Ollama, LM Studio...). Not used by the MCP server itself."""

    base_url: str | None = _f(None, "API base URL. Empty = $OPENAI_BASE_URL, else https://api.openai.com/v1.")
    api_key: str | None = _f(None, "API key. Prefer OPENAI_API_KEY or CLICKSENSE_LLM_API_KEY in .env.")
    model: str = _f("gpt-4.1-mini", "Model name. It must support tool (function) calling.",
                    env="CLICKSENSE_LLM_MODEL")
    temperature: float | None = _f(None, "Sampling temperature. Empty = provider default (some reasoning models "
                                         "reject the parameter).")
    max_steps: int = _f(16, "Tool-call rounds allowed per question before giving up.")
    auto_context: bool = _f(True, "Call get_context before the model's first turn, so ambiguities reach it even "
                                  "if it would skip that step.")
    max_tool_chars: int = _f(12_000, "A tool result longer than this is truncated before the model sees it.")
    timeout_s: float = _f(120, "Seconds to wait for the LLM API.")
    extra_headers: dict[str, str] = _f(description="Extra HTTP headers for the LLM API (gateways, Azure...).",
                                       factory=dict)
    mcp_url: str | None = _f(None, "Empty = run ClickSense in-process. Else the URL of a running server, "
                                   "e.g. http://winserver:3110/mcp.")
    mcp_token: str | None = _f(None, "Bearer token sent to mcp_url.", env="CLICKSENSE_MCP_TOKEN")


class Settings(BaseModel):
    """The whole configuration. Top-level keys = the sections above + a few paths and cache sizes."""

    connection: Connection = _f(description="ClickHouse connection.", factory=Connection)
    limits: Limits = _f(description="Guard-rails sent with every query.", factory=Limits)
    policy: Policy = _f(description="What the guard accepts.", factory=Policy)
    http: Http = _f(description="HTTP transport for remote MCP clients.", factory=Http)
    llm: LLM = _f(description="OpenAI-compatible model for clicksense-chat.", factory=LLM)
    semantic_dir: Path = _f(Path("semantic"), "Folder of the semantic layer (dictionary, glossary, metrics, "
                                              "knowledge, skills). Relative to this file.")
    audit_path: Path | None = _f(Path("audit/clicksense.jsonl"), "JSONL audit log of every query. Empty disables it.")
    catalog_ttl_s: float = _f(600, "Seconds the live schema is cached. refresh_catalog reloads it at once.")
    value_cache_ttl_s: float = _f(3600, "Seconds the distinct values of a column are cached (used to resolve "
                                        "'France' -> 'FR').")
    value_cache_max_distinct: int = _f(2000, "Above this many distinct values a column is searched live instead "
                                             "of cached.")
    value_scan_max_rows: int = _f(50_000_000, "Tables larger than this are sampled or time-windowed when their "
                                              "values are enumerated.")

    @field_validator("semantic_dir", "audit_path", mode="before")
    @classmethod
    def _expand(cls, v):
        # "~" works in paths; an empty value disables audit_path.
        return None if v in (None, "", "none") else Path(os.path.expanduser(str(v)))


# =============================================================================================
#  Loading
# =============================================================================================

def _subst(value):
    """Replace ${VAR} / ${VAR:-default} recursively. A missing variable without default is an
    error on purpose: a silently empty password is worse than a clear message at start-up."""
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


def env_overrides(model: type[BaseModel] = Settings, prefix: tuple[str, ...] = ()) -> dict[str, tuple[str, ...]]:
    """{ENV_VAR: ("section", "field")} for every field declared with `env=`. Single source of truth
    for the overrides, the generated example file and the documentation."""
    out: dict[str, tuple[str, ...]] = {}
    for name, field in model.model_fields.items():
        extra = field.json_schema_extra or {}
        if isinstance(extra, dict) and extra.get("env"):
            out[extra["env"]] = prefix + (name,)
        ann = field.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            out.update(env_overrides(ann, prefix + (name,)))
    return out


def load_dotenv(*dirs: Path) -> None:
    """KEY=value lines from `.env` in these directories; variables already set win. No dependency,
    and the same file works on Windows and Unix (a .env is easier to edit than system variables)."""
    for d in dirs:
        f = d / ".env"
        if not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8-sig").splitlines():  # -sig: Notepad's UTF-8 BOM
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.removeprefix("export ").partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def find_config(path: str | Path | None = None) -> Path | None:
    """The config file that load_settings would use, or None for pure defaults."""
    candidate = path or os.environ.get("CLICKSENSE_CONFIG")
    if candidate is None and Path("clicksense.yaml").exists():
        candidate = "clicksense.yaml"
    return Path(candidate).expanduser() if candidate else None


def load_settings(path: str | Path | None = None) -> Settings:
    """Defaults <- clicksense.yaml (with ${VAR}) <- environment variables. See the module docstring."""
    cfg = find_config(path)
    # .env next to the config, then in the working directory, before ${VAR} substitution.
    load_dotenv(*([cfg.resolve().parent] if cfg else []), Path.cwd())
    raw: dict = {}
    base = Path.cwd()
    if cfg:
        if not cfg.exists():
            raise ConfigError(f"config file not found: {cfg}")
        # Explicit encoding: Windows would read the file as cp1252 and break accented values.
        raw = _subst(yaml.safe_load(cfg.read_text(encoding="utf-8-sig")) or {})
        base = cfg.resolve().parent
    for env, keys in env_overrides().items():
        if os.environ.get(env):
            section = raw
            for k in keys[:-1]:
                if not isinstance(section.get(k), dict):  # absent, or `http:` left empty in the YAML
                    section[k] = {}
                section = section[k]
            section[keys[-1]] = os.environ[env]
    try:
        settings = Settings.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError: say which key is wrong, in one block
        raise ConfigError(f"invalid configuration{f' in {cfg}' if cfg else ''}:\n{exc}") from None
    # Relative paths are relative to the config file, not to wherever the client spawned us.
    if not settings.semantic_dir.is_absolute():
        settings.semantic_dir = base / settings.semantic_dir
    if settings.audit_path and not settings.audit_path.is_absolute():
        settings.audit_path = base / settings.audit_path
    if settings.connection.database.lower() in {"system", "information_schema"}:
        raise ConfigError("connection.database must not be a system database")
    return settings


# =============================================================================================
#  Rendering: the commented example file and the effective configuration
# =============================================================================================

def _yaml_scalar(value: Any) -> str:
    """One value as inline YAML (paths with forward slashes, None as empty)."""
    if isinstance(value, Path):
        value = value.as_posix()
    if value is None:
        return ""
    text = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True).strip()
    return text.removesuffix("\n...").strip()


def _wrap(text: str, indent: str, width: int = 96) -> list[str]:
    """A description as wrapped YAML comment lines."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(indent) + 2 + len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return [f"{indent}# {ln}" for ln in lines]


def render_yaml(settings: Settings | None = None, *, example: bool = True, mask: bool = True) -> str:
    """A fully commented clicksense.yaml.

    example=True: defaults, with `${ENV:-default}` for fields that have an environment variable
    (this is clicksense.example.yaml). example=False: the effective values of `settings`, secrets
    masked (this is `clicksense-admin config --show`)."""
    settings = settings or Settings()
    if example:
        out = ["# ClickSense configuration. Generated by `clicksense-admin config --example` from the code",
               "# (src/clicksense/config.py): every key, its default and its meaning. Do not edit this file:",
               "# copy it to clicksense.yaml and change your copy. Secrets (passwords, keys, tokens) go in .env.",
               "# Resolution order: defaults < this file (${VAR} / ${VAR:-default} from the environment)",
               "# < the environment variables named below. Restart the server after a change.",
               ""]
    else:
        out = ["# Effective ClickSense configuration (clicksense-admin config --show): defaults + clicksense.yaml",
               "# + .env + environment. Secrets are masked.", ""]

    def emit(model: BaseModel, indent: str) -> None:
        """Write the fields of one section, recursing into sub-sections."""
        for name, field in type(model).model_fields.items():
            value = getattr(model, name)
            env = (field.json_schema_extra or {}).get("env") if isinstance(field.json_schema_extra, dict) else None
            if isinstance(value, BaseModel):
                out.extend(_wrap(field.description or "", indent))
                out.append(f"{indent}{name}:")
                emit(value, indent + "  ")
                out.append("")
                continue
            desc = field.description or ""
            if env:
                desc += f" [env: {env}]"
            out.extend(_wrap(desc, indent))
            if example:
                shown = _yaml_scalar(field.get_default(call_default_factory=True))
                if env and not isinstance(value, list):
                    shown = f"${{{env}:-{shown}}}" if shown not in ("", "''") else f"${{{env}:-}}"
                if name in SECRET_FIELDS and not env:
                    out.append(f"{indent}# {name}:")
                    continue
                out.append(f"{indent}{name}: {shown}".rstrip())
            else:
                if mask and name in SECRET_FIELDS and value:
                    shown = "'***'" if not isinstance(value, list) else _yaml_scalar(["***"] * len(value))
                else:
                    shown = _yaml_scalar(value)
                out.append(f"{indent}{name}: {shown}".rstrip())

    emit(settings, "")
    return "\n".join(out).rstrip() + "\n"
