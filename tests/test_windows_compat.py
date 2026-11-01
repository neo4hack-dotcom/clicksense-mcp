"""Windows guarantees that can be checked from any OS. Each rule here only bites on a Windows
server, so it is pinned in a test instead of being rediscovered on a user's machine."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from clicksense.config import load_dotenv, load_settings
from clicksense.knowledge import Knowledge

ROOT = Path(__file__).resolve().parents[1]
BATCH = sorted(p.name for p in ROOT.glob("*.bat"))
SCRIPTS = [ROOT / n for n in BATCH] + [ROOT / "scripts" / "windows" / "service.ps1"]


def test_expected_scripts_exist():
    assert {"install-windows.bat", "serve-windows.bat", "chat-windows.bat", "check-windows.bat"} <= set(BATCH)


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_scripts_use_crlf_and_ascii(path: Path):
    # cmd.exe misreads labels and blocks with bare LF; PowerShell 5.1 reads BOM-less files as ANSI,
    # so any non-ASCII character would be mangled.
    raw = path.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert all(b < 128 for b in raw), "non-ASCII character"


def test_gitattributes_pins_crlf():
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.bat text eol=crlf" in text and "*.ps1 text eol=crlf" in text


@pytest.mark.parametrize("name", [n for n in BATCH if n != "install-windows.bat"])
def test_run_scripts_use_the_venv_utf8_and_no_reload(name: str):
    text = (ROOT / name).read_text(encoding="ascii")
    assert "set PYTHONUTF8=1" in text  # rich output and accented data on a cp1252 console
    assert ".venv\\Scripts\\python.exe" in text
    assert "--reload" not in text.replace("No --reload", "")


def test_install_script_steps():
    text = (ROOT / "install-windows.bat").read_text(encoding="ascii")
    for needle in ('"py -3"', "-m venv .venv", "-r requirements.txt", "--no-deps -e .",
                   'copy /y "clicksense.example.yaml"', 'copy /y ".env.example"', "--gen-token", "--check"):
        assert needle in text, needle


def test_files_referenced_by_scripts_exist():
    text = "\n".join(p.read_text(encoding="ascii") for p in SCRIPTS)
    for rel in ("clicksense.example.yaml", ".env.example", "requirements.txt", "serve-windows.bat",
                "scripts\\windows\\service.ps1"):
        if rel in text:
            assert (ROOT / rel.replace("\\", "/")).exists(), rel


def test_service_script_is_balanced():
    # No PowerShell on the CI box: at least catch unbalanced braces/parentheses outside strings.
    text = (ROOT / "scripts" / "windows" / "service.ps1").read_text(encoding="ascii")
    code = re.sub(r"#.*", "", re.sub(r'"(?:`.|[^"`])*"|\'[^\']*\'', '""', text))
    assert code.count("{") == code.count("}") and code.count("(") == code.count(")")
    for action in ("install", "uninstall", "start", "stop", "restart", "status"):
        assert f'"{action}"' in text


def test_requirements_cover_runtime_dependencies():
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    deps = re.search(r"dependencies = \[(.*?)\]", pyproject, re.S).group(1)
    for name in re.findall(r'"([A-Za-z0-9_.-]+)', deps):
        assert re.search(rf"^{re.escape(name)}\b", reqs, re.M | re.I), name
    # Windows has no IANA zones: without tzdata, DateTime('Europe/Paris') columns fail to decode.
    assert re.search(r'^tzdata\S*;\s*sys_platform\s*==\s*"win32"', reqs, re.M)


def test_env_example_only_names_variables_the_code_reads():
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src" / "clicksense").glob("*.py"))
    src += (ROOT / "clicksense.example.yaml").read_text(encoding="utf-8")
    names = re.findall(r"^#?\s*((?:CLICKSENSE|OPENAI)_[A-Z_]+)=", (ROOT / ".env.example").read_text(encoding="utf-8"),
                       re.M)
    assert names
    for name in names:
        assert name in src, f"{name} is documented in .env.example but never read"


def test_notepad_bom_and_accents(tmp_path: Path, monkeypatch):
    # Notepad saves UTF-8 with a BOM; Python on Windows reads files as cp1252 unless told otherwise.
    (tmp_path / "clicksense.yaml").write_bytes("﻿connection:\n  database: montréal\n".encode("utf-8"))
    (tmp_path / ".env").write_bytes("﻿CLICKSENSE_TEST_BOM=ok\n".encode("utf-8"))
    monkeypatch.delenv("CLICKSENSE_TEST_BOM", raising=False)
    monkeypatch.delenv("CLICKSENSE_DATABASE", raising=False)
    settings = load_settings(tmp_path / "clicksense.yaml")
    assert settings.connection.database == "montréal"
    import os

    assert os.environ.pop("CLICKSENSE_TEST_BOM") == "ok"

    skill = tmp_path / "semantic" / "skills" / "bom-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes("﻿---\nname: bom-skill\ndescription: Été\n---\nBody".encode("utf-8"))
    k = Knowledge(tmp_path / "semantic")
    assert not k.errors and k.skills["bom-skill"]["description"] == "Été"


def test_dotenv_does_not_override_real_environment(tmp_path: Path, monkeypatch):
    (tmp_path / ".env").write_text('CLICKSENSE_TEST_A="from-file"\nexport CLICKSENSE_TEST_B=b\n# x=y\n',
                                   encoding="utf-8")
    monkeypatch.setenv("CLICKSENSE_TEST_A", "from-env")
    monkeypatch.delenv("CLICKSENSE_TEST_B", raising=False)
    load_dotenv(tmp_path)
    import os

    assert os.environ["CLICKSENSE_TEST_A"] == "from-env" and os.environ.pop("CLICKSENSE_TEST_B") == "b"
