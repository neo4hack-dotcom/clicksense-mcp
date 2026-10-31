from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from clicksense.config import Connection, Limits, Policy, Settings

ROOT = Path(__file__).resolve().parent.parent


def _clickhouse_up() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:18123/ping", timeout=1) as r:
            return r.read().strip() == b"Ok."
    except OSError:
        return False


CH_UP = _clickhouse_up()


def make_settings(tmp_path: Path, **limits) -> Settings:
    return Settings(
        connection=Connection(host="127.0.0.1", port=18123, username="clicksense", password="clicksense",
                              database="shop"),
        limits=Limits(**limits),
        policy=Policy(allowed_databases=["shop"]),
        semantic_dir=ROOT / "semantic",
        audit_path=tmp_path / "audit.jsonl",
    )


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def live(tmp_path):
    if not CH_UP:
        pytest.skip("local ClickHouse not running (scripts/dev-clickhouse.sh start && seed)")
    from clicksense.service import ClickSense

    cs = ClickSense(make_settings(tmp_path))
    yield cs
    cs.executor.close()
