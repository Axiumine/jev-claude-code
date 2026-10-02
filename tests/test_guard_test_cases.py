"""Tests for research/reference-build/install/guard-test-cases.py."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest

ANCHOR_LINE = "  ['Bash process env code', { tool_name: 'Bash' }, 'deny'],"
SOURCE = f"const cases = [\n  ['first', {{}}, 'allow'],\n{ANCHOR_LINE}\n  ['last', {{}}, 'allow'],\n];\n"


def test_constants(guard_cases: ModuleType) -> None:
    assert guard_cases.TEST_FILE == "no-secret-leak.test.cjs"
    assert guard_cases.MARKER == "Jev hooks: OpenRouter key"
    assert guard_cases.ANCHOR == "  ['Bash process env code',"
    assert guard_cases.MARKER in guard_cases.NEW_CASES


def test_new_cases_shape(guard_cases: ModuleType) -> None:
    lines = guard_cases.NEW_CASES.split("\n")
    assert lines[0] == ""
    assert lines[1].startswith("  // Jev hooks: OpenRouter key variable")
    rows = lines[2:]
    assert len(rows) == 40
    assert all(row.startswith("  ['") for row in rows)
    assert sum(row.endswith("'deny'],") for row in rows) == 27
    assert sum(row.endswith("'allow'],") for row in rows) == 13
    assert rows[0].startswith("  ['Bash expand OpenRouter key'")
    assert rows[-1].startswith("  ['Monitor tail log'")
    assert not guard_cases.NEW_CASES.endswith("\n")


def test_add_cases_inserts_after_anchor_line(guard_cases: ModuleType) -> None:
    updated = guard_cases.add_cases(SOURCE)
    head, tail = SOURCE.split(ANCHOR_LINE + "\n")
    assert updated == head + ANCHOR_LINE + guard_cases.NEW_CASES + "\n" + tail
    assert updated.index(ANCHOR_LINE) < updated.index("Jev hooks") < updated.index("['last'")


def test_add_cases_anchor_on_last_line_without_newline(guard_cases: ModuleType) -> None:
    source = "x\n  ['Bash process env code',"
    assert guard_cases.add_cases(source) == source + guard_cases.NEW_CASES


def test_add_cases_is_idempotent(guard_cases: ModuleType) -> None:
    once = guard_cases.add_cases(SOURCE)
    assert guard_cases.add_cases(once) is None


def test_add_cases_marker_alone_means_present(guard_cases: ModuleType) -> None:
    assert guard_cases.add_cases("// Jev hooks: OpenRouter key") is None


def test_add_cases_missing_anchor_raises(guard_cases: ModuleType) -> None:
    with pytest.raises(ValueError, match="substring not found"):
        guard_cases.add_cases("no anchor here\n")


def test_add_cases_uses_first_anchor(guard_cases: ModuleType) -> None:
    source = f"{ANCHOR_LINE}\n{ANCHOR_LINE}\n"
    updated = guard_cases.add_cases(source)
    assert updated == ANCHOR_LINE + guard_cases.NEW_CASES + f"\n{ANCHOR_LINE}\n"


def test_main_golden(
    guard_cases: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "no-secret-leak.test.cjs"
    target.write_text(SOURCE, encoding="utf-8")

    assert guard_cases.main() == 0
    assert capsys.readouterr().out == "cases added\n"
    after = target.read_text(encoding="utf-8")
    assert after == guard_cases.add_cases(SOURCE)
    assert after.startswith(f"const cases = [\n  ['first', {{}}, 'allow'],\n{ANCHOR_LINE}\n  // Jev hooks")
    assert after.endswith("'allow'],\n  ['last', {}, 'allow'],\n];\n")

    assert guard_cases.main() == 0
    assert capsys.readouterr().out == "cases already present\n"
    assert target.read_text(encoding="utf-8") == after


def test_main_reads_and_writes_utf8(
    guard_cases: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "no-secret-leak.test.cjs"
    target.write_bytes(SOURCE.replace("first", "primà").encode("utf-8"))
    assert guard_cases.main() == 0
    capsys.readouterr()
    assert "primà" in target.read_bytes().decode("utf-8")


def test_main_missing_file_raises(guard_cases: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        guard_cases.main()
    assert list(tmp_path.iterdir()) == []


def test_script_entry_point(
    guard_cases: ModuleType,
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = root / "research/reference-build/install/guard-test-cases.py"
    target = tmp_path / "no-secret-leak.test.cjs"
    target.write_text(SOURCE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [str(script)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(script), run_name="__main__")
    assert exc.value.code == 0
    assert capsys.readouterr().out == "cases added\n"
    assert target.read_text(encoding="utf-8") == guard_cases.add_cases(SOURCE)
