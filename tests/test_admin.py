"""Maintenance tooling: generated configuration, `kb validate`, `kb new`, `kb bootstrap`."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from clicksense import admin
from clicksense.config import Settings, env_overrides, load_settings, render_yaml
from clicksense.errors import ClickSenseError
from clicksense.kbtools import scaffold, validate
from clicksense.knowledge import Knowledge

from .conftest import CH_UP, ROOT, make_settings

ENV_VARS = list(env_overrides())


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(Path(__file__).parent)  # no .env in tests/


@pytest.fixture
def kb(tmp_path) -> Path:
    root = tmp_path / "semantic"
    shutil.copytree(ROOT / "semantic", root, ignore=shutil.ignore_patterns("inbox"))
    return root


# ------------------------------------------------------------------------------- configuration
def test_example_file_is_in_sync_with_the_code():
    # Regenerate with: clicksense-admin config --example > clicksense.example.yaml
    assert (ROOT / "clicksense.example.yaml").read_text(encoding="utf-8") == render_yaml(example=True)


def test_every_setting_is_documented():
    def walk(model):
        for name, field in model.model_fields.items():
            assert field.description, f"{model.__name__}.{name} has no description"
            ann = field.annotation
            if isinstance(ann, type) and hasattr(ann, "model_fields"):
                walk(ann)
    walk(Settings)


def test_example_file_loads_to_the_defaults(tmp_path, clean_env):
    path = tmp_path / "clicksense.yaml"
    path.write_text(render_yaml(example=True), encoding="utf-8")
    loaded = load_settings(path)
    defaults = Settings()
    for section in ("connection", "limits", "policy", "http", "llm"):
        a, b = getattr(loaded, section).model_dump(), getattr(defaults, section).model_dump()
        assert {k: v for k, v in a.items() if v not in ("", None)} == {k: v for k, v in b.items() if v not in ("", None)}


def test_env_overrides_reach_nested_sections(tmp_path, clean_env, monkeypatch):
    path = tmp_path / "clicksense.yaml"
    path.write_text("http:\nllm:\n", encoding="utf-8")  # empty sections must not swallow overrides
    monkeypatch.setenv("CLICKSENSE_HTTP_PORT", "4000")
    monkeypatch.setenv("CLICKSENSE_LLM_MODEL", "my-model")
    monkeypatch.setenv("CLICKSENSE_HOST", "ch.internal")
    s = load_settings(path)
    assert (s.http.port, s.llm.model, s.connection.host) == (4000, "my-model", "ch.internal")


def test_invalid_value_names_the_key(tmp_path, clean_env):
    path = tmp_path / "clicksense.yaml"
    path.write_text("limits:\n  max_threads: lots\n", encoding="utf-8")
    with pytest.raises(Exception) as ei:
        load_settings(path)
    assert "max_threads" in str(ei.value)


def test_show_masks_secrets(tmp_path):
    s = make_settings(tmp_path)
    s.connection.password = "hunter2-secret"
    s.http.tokens = ["x" * 30]
    text = render_yaml(s, example=False)
    assert "hunter2" not in text and "x" * 30 not in text and "***" in text
    yaml.safe_load(text)


# ------------------------------------------------------------------------------- validate
def test_repository_semantic_layer_is_clean():
    findings = validate(Knowledge(ROOT / "semantic"))
    assert [f.line() for f in findings if f.level in ("error", "warning")] == []


def test_validate_catches_common_mistakes(kb):
    (kb / "metrics.yaml").write_text((kb / "metrics.yaml").read_text(encoding="utf-8") + """
  - name: broken
    table: shop.orders
    expresion: sum(o.amount_eur)
    dimensions: [planet]
    synonyms: [turnover]
""", encoding="utf-8")
    (kb / "glossary.yaml").write_text((kb / "glossary.yaml").read_text(encoding="utf-8") + """
  - term: margin
    definition: Gross margin.
    maps_to: "metric:gross_margin"
""", encoding="utf-8")
    d = kb / "dictionary" / "shop.yaml"
    d.write_text(d.read_text(encoding="utf-8").replace("    alias: o\n", "    alias: o\n    descripton: typo\n", 1)
                 .replace("join: products, column: shop.products.category", "join: product, column: shop.products.category"),
                 encoding="utf-8")
    (kb / "skills" / "answer-data-question" / "SKILL.md").write_text("---\nname: other-name\n---\nBody of the skill.",
                                                                    encoding="utf-8")
    lines = "\n".join(f.line() for f in validate(Knowledge(kb)))
    assert "unknown key 'expresion'" in lines and "did you mean 'expression'" in lines
    assert "missing 'expression'" in lines
    assert "dimension 'planet'" in lines
    assert "'turnover' also names metric" in lines
    assert "maps_to metric:gross_margin: no such metric" in lines
    assert "unknown key 'descripton'" in lines and "did you mean 'description'" in lines
    assert "join 'product' is not declared" in lines
    assert "differs from its folder" in lines and "no 'description'" in lines


def test_validate_reports_yaml_syntax_errors(kb):
    (kb / "metrics.yaml").write_text("metrics:\n  - name: x\n   bad: indentation\n", encoding="utf-8")
    findings = validate(Knowledge(kb))
    assert findings[0].level == "error" and "metrics.yaml" in findings[0].message


# ------------------------------------------------------------------------------- scaffold
@pytest.mark.parametrize("kind,name", [("metric", "net_revenue"), ("term", "basket: size"),
                                       ("example", "Revenue by brand, last week?"), ("knowledge", "refund-policy"),
                                       ("skill", "weekly-review"), ("table", "shop.returns")])
def test_scaffold_creates_loadable_entries_flagged_as_todo(kb, kind, name):
    before = Knowledge(kb)
    path = scaffold(kb, kind, name)
    assert path.exists()
    after = Knowledge(kb)
    assert not after.errors
    grew = {"metric": (len(before.metrics), len(after.metrics)), "term": (len(before.glossary), len(after.glossary)),
            "example": (len(before.examples), len(after.examples)), "knowledge": (len(before.docs), len(after.docs)),
            "skill": (len(before.skills), len(after.skills)), "table": (len(before.tables), len(after.tables))}[kind]
    assert grew[1] == grew[0] + 1
    assert any("TODO" in f.message for f in validate(after))


@pytest.mark.parametrize("kind,name", [("metric", "revenue"), ("metric", "Bad Name"), ("skill", "Bad Name"),
                                       ("table", "orders"), ("table", "shop.orders"), ("nope", "x")])
def test_scaffold_refuses_duplicates_and_bad_names(kb, kind, name):
    with pytest.raises(ClickSenseError):
        scaffold(kb, kind, name)


def test_admin_cli_exit_codes(kb, tmp_path, capsys, clean_env):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"semantic_dir: {kb.as_posix()}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        admin.main(["--config", str(cfg), "kb", "validate"])
    assert ei.value.code == 0
    with pytest.raises(SystemExit) as ei:
        admin.main(["--config", str(cfg), "kb", "new", "metric", "revenue"])
    assert ei.value.code == 1 and "already exists" in capsys.readouterr().err


# ------------------------------------------------------------------------------- live
@pytest.mark.skipif(not CH_UP, reason="local ClickHouse not running")
def test_live_validation_compiles_metrics_and_examples(kb, tmp_path):
    from clicksense.service import ClickSense

    s = make_settings(tmp_path)
    s.semantic_dir = kb
    cs = ClickSense(s)
    cs.catalog.refresh(force=True)
    assert [f.line() for f in validate(cs.knowledge, cs.catalog, cs) if f.level == "error"] == []
    m = kb / "metrics.yaml"
    m.write_text(m.read_text(encoding="utf-8").replace("sum(o.quantity)", "sum(o.qty)"), encoding="utf-8")
    e = kb / "examples.yaml"
    e.write_text(e.read_text(encoding="utf-8").replace("AS conversion_rate", "AS conversion_rate, nope"),
                 encoding="utf-8")
    d = kb / "dictionary" / "shop.yaml"
    d.write_text(d.read_text(encoding="utf-8").replace("      quantity:", "      quantty:"), encoding="utf-8")
    lines = "\n".join(f.line() for f in validate(cs.knowledge, cs.catalog, cs))
    assert "units_sold: the compiled SQL fails" in lines
    assert "the SQL no longer runs" in lines
    assert "shop.orders.quantty: column not found" in lines and "did you mean 'quantity'" in lines


@pytest.mark.skipif(not CH_UP, reason="local ClickHouse not running")
def test_bootstrap_documents_a_table_from_clickhouse(tmp_path, kb):
    from clicksense.kbtools import bootstrap_table
    from clicksense.service import ClickSense

    s = make_settings(tmp_path)
    s.semantic_dir = kb
    cs = ClickSense(s)
    block, path = bootstrap_table(cs, "shop.products", write=False)
    spec = yaml.safe_load(block)["shop.products"]
    assert spec["description"] == "Product catalogue"
    assert "Home Appliances" in spec["columns"]["category"]["values"]
    (kb / "dictionary" / "shop.yaml").write_text("tables:\n", encoding="utf-8")
    _, path = bootstrap_table(cs, "shop.products")
    assert "shop.products" in Knowledge(kb).tables
