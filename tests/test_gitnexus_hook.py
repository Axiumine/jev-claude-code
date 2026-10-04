"""scripts/gitnexus_hook.py: a no-op unless GitNexus is set up in the clone."""

from __future__ import annotations

import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import gitnexus_hook

ARGS = ["check", "--cycles"]


class Call:
    """subprocess.call stand-in that records each command."""

    def __init__(self, status: int = 0) -> None:
        self.status = status
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> int:
        self.calls.append((list(cmd), kwargs))
        return self.status


@pytest.fixture
def clone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Call:
    """A clone with GitNexus set up, node and gitnexus both installed, no checkout in progress."""
    (tmp_path / ".gitnexus").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRE_COMMIT_CHECKOUT_TYPE", raising=False)
    monkeypatch.setattr(shutil, "which", lambda tool: f"/bin/{tool}")
    call = Call()
    monkeypatch.setattr(subprocess, "call", call)
    return call


def test_a_clone_without_gitnexus_is_left_alone(tmp_path: Path, clone: Call) -> None:
    (tmp_path / ".gitnexus").rmdir()
    assert gitnexus_hook.main(ARGS) == 0
    assert clone.calls == []


def test_a_gitnexus_file_instead_of_a_directory_is_not_a_set_up_clone(tmp_path: Path, clone: Call) -> None:
    (tmp_path / ".gitnexus").rmdir()
    (tmp_path / ".gitnexus").write_text("", encoding="utf-8")
    assert gitnexus_hook.main(ARGS) == 0
    assert clone.calls == []


def test_file_checkouts_do_not_refresh_the_graph(monkeypatch: pytest.MonkeyPatch, clone: Call) -> None:
    monkeypatch.setenv("PRE_COMMIT_CHECKOUT_TYPE", "0")
    clone.status = 5
    assert gitnexus_hook.main(["analyze"]) == 0
    assert clone.calls == []


def test_branch_checkouts_do(monkeypatch: pytest.MonkeyPatch, clone: Call) -> None:
    monkeypatch.setenv("PRE_COMMIT_CHECKOUT_TYPE", "1")
    assert gitnexus_hook.main(["analyze"]) == 0
    assert clone.calls == [(["/bin/gitnexus", "analyze"], {})]


def test_the_runner_left_by_analyze_wins_over_a_global_install(tmp_path: Path, clone: Call) -> None:
    (tmp_path / ".gitnexus" / "run.cjs").write_text("// runner\n", encoding="utf-8")
    assert gitnexus_hook.main(ARGS) == 0
    assert clone.calls == [(["/bin/node", str(tmp_path / ".gitnexus" / "run.cjs"), *ARGS], {})]


def test_a_runner_directory_is_not_a_runner(tmp_path: Path, clone: Call) -> None:
    (tmp_path / ".gitnexus" / "run.cjs").mkdir()
    assert gitnexus_hook.main(ARGS) == 0
    assert clone.calls == [(["/bin/gitnexus", *ARGS], {})]


def test_without_a_runner_the_global_install_is_used(clone: Call) -> None:
    assert gitnexus_hook.main(ARGS) == 0
    assert clone.calls == [(["/bin/gitnexus", *ARGS], {})]


def test_the_exit_status_of_gitnexus_is_the_hooks(clone: Call) -> None:
    clone.status = 3
    assert gitnexus_hook.main(ARGS) == 3


def test_node_and_the_runner_are_enough(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clone: Call) -> None:
    (tmp_path / ".gitnexus" / "run.cjs").write_text("// runner\n", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda tool: None if tool == "gitnexus" else f"/bin/{tool}")
    assert gitnexus_hook.main(ARGS) == 0
    assert clone.calls == [(["/bin/node", str(tmp_path / ".gitnexus" / "run.cjs"), *ARGS], {})]


def test_a_runner_without_node_falls_back_to_the_global_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clone: Call
) -> None:
    (tmp_path / ".gitnexus" / "run.cjs").write_text("// runner\n", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda tool: None if tool == "node" else f"/bin/{tool}")
    assert gitnexus_hook.main(ARGS) == 0
    assert clone.calls == [(["/bin/gitnexus", *ARGS], {})]


def test_neither_runner_nor_install_skips_with_a_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clone: Call, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".gitnexus" / "run.cjs").write_text("// runner\n", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda tool: None)
    clone.status = 5
    assert gitnexus_hook.main(ARGS) == 0
    assert clone.calls == []
    assert capsys.readouterr().out == gitnexus_hook.SKIPPED + "\n"


def test_command_names_the_tools_it_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda tool: f"/opt/{tool}")
    assert gitnexus_hook.command(tmp_path) == ["/opt/gitnexus"]
    (tmp_path / ".gitnexus").mkdir()
    (tmp_path / ".gitnexus" / "run.cjs").write_text("", encoding="utf-8")
    assert gitnexus_hook.command(tmp_path) == ["/opt/node", str(tmp_path / ".gitnexus" / "run.cjs")]
    monkeypatch.setattr(shutil, "which", lambda tool: None)
    assert gitnexus_hook.command(tmp_path) is None


def test_main_reads_sys_argv(monkeypatch: pytest.MonkeyPatch, clone: Call) -> None:
    monkeypatch.setattr(sys, "argv", ["gitnexus_hook.py", "analyze", "--index-only"])
    assert gitnexus_hook.main() == 0
    assert clone.calls == [(["/bin/gitnexus", "analyze", "--index-only"], {})]


def test_script_entry_point(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = Path(gitnexus_hook.__file__)
    monkeypatch.chdir(tmp_path)  # no .gitnexus/ here: the hook does nothing and exits 0
    monkeypatch.setattr(sys, "argv", [str(script), *ARGS])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(script), run_name="__main__")
    assert exit_info.value.code == 0
