"""Tests for research/reference-build/skill/build-skill.py."""

from __future__ import annotations

import hashlib
import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest

NEW_DESC_LEN = 469
OLD_DESC = "  old line one\n  old two\n"


def skill_dir(root: Path) -> Path:
    return root / "research" / "reference-build" / "skill"


def small_upstream(*, desc_last: bool = False) -> str:
    tail = "" if desc_last else "license: MIT\n"
    return f"---\nname: x\ndescription: >\n{OLD_DESC}{tail}---\n# Body\n\ntext\n\n\n"


def test_desc_len_counts_folded_block(build_skill: ModuleType) -> None:
    fm = "name: x\ndescription: >\n  abc\n  de\nlicense: MIT"
    assert build_skill.desc_len(fm) == len("  abc\n  de\n")


def test_desc_len_block_at_end_without_newline(build_skill: ModuleType) -> None:
    assert build_skill.desc_len("name: x\ndescription: >\n  abc\n  de") == len("  abc\n  de\n")


def test_desc_len_ignores_unindented_lines(build_skill: ModuleType) -> None:
    assert build_skill.desc_len("description: >\n  abc\nnext: y\n  not part\n") == len("  abc\n")


def test_desc_len_without_description_raises(build_skill: ModuleType) -> None:
    with pytest.raises(ValueError, match=r"^front matter has no folded 'description: >' block$"):
        build_skill.desc_len("name: x\ndescription: plain")


def test_build_skill_replaces_description(build_skill: ModuleType) -> None:
    out, old, new = build_skill.build_skill(small_upstream(), "ADD\n")
    assert out == "---\nname: x\n" + build_skill.NEW_DESC + "license: MIT\n---\n# Body\n\ntext\nADD\n"
    assert old == len(OLD_DESC)
    assert new == NEW_DESC_LEN


def test_build_skill_description_last_in_front_matter(build_skill: ModuleType) -> None:
    out, old, new = build_skill.build_skill(small_upstream(desc_last=True), "ADD\n")
    assert out == "---\nname: x\n" + build_skill.NEW_DESC + "---\n# Body\n\ntext\nADD\n"
    assert (old, new) == (len(OLD_DESC), NEW_DESC_LEN)


def test_build_skill_replaces_only_first_description(build_skill: ModuleType) -> None:
    up = "---\ndescription: >\n  one\ndescription: >\n  two\n---\nbody\n"
    out, old, _ = build_skill.build_skill(up, "")
    assert out == "---\n" + build_skill.NEW_DESC + "description: >\n  two\n---\nbody\n"
    assert old == len("  one\n")


def test_build_skill_stops_at_first_closing_rule(build_skill: ModuleType) -> None:
    up = "---\ndescription: >\n  one\n---\nbody\n---\nmore\n"
    out, _, _ = build_skill.build_skill(up, "ADD")
    assert out == "---\n" + build_skill.NEW_DESC.rstrip("\n") + "\n---\nbody\n---\nmore\nADD"


@pytest.mark.parametrize("tail", ["X", "  "])
def test_build_skill_strips_only_newlines(build_skill: ModuleType, tail: str) -> None:
    up = f"---\ndescription: >\n  d\nname: v{tail}\n---\nbody{tail}\n\n"
    out, _, _ = build_skill.build_skill(up, "ADD")
    assert out == "---\n" + build_skill.NEW_DESC + f"name: v{tail}\n---\nbody{tail}\nADD"


def test_build_skill_addendum_is_appended_verbatim(build_skill: ModuleType) -> None:
    out, _, _ = build_skill.build_skill(small_upstream(), "\n\nADD")
    assert out.endswith("text\n\n\nADD")


def test_build_skill_requires_front_matter(build_skill: ModuleType) -> None:
    with pytest.raises(ValueError, match=r"^upstream SKILL\.md has no front matter$"):
        build_skill.build_skill("\n---\ndescription: >\n  a\n---\nbody\n", "")


def test_build_skill_requires_description(build_skill: ModuleType) -> None:
    with pytest.raises(ValueError, match=r"^front matter has no folded 'description: >' block$"):
        build_skill.build_skill("---\nname: x\n---\nbody\n", "")


def test_main_reproduces_vendored_skill(build_skill: ModuleType, root: Path, tmp_path: Path) -> None:
    src = skill_dir(root)
    out = tmp_path / "SKILL.md"
    argv = [str(src / "upstream" / "SKILL.md"), str(src / "addendum.md"), str(out)]
    assert build_skill.main(argv) == 0
    assert out.read_bytes() == (src / "typesafe-ai" / "SKILL.md").read_bytes()
    assert (tmp_path / "SKILL.md.diff").read_bytes() == (src / "upstream-to-local.diff").read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["SKILL.md", "SKILL.md.diff"]


def test_main_prints_sizes(
    build_skill: ModuleType, root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = skill_dir(root)
    argv = [str(src / "upstream" / "SKILL.md"), str(src / "addendum.md"), str(tmp_path / "o.md")]
    build_skill.main(argv)
    assert capsys.readouterr().out == (
        "description chars: 679 -> 469\nbody chars: upstream 10036, local 14298 (~3574 tokens when the skill loads)\n"
    )


def test_main_reads_sys_argv_by_default(
    build_skill: ModuleType, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = skill_dir(root)
    out = tmp_path / "o.md"
    monkeypatch.setattr(
        sys, "argv", ["build-skill.py", str(src / "upstream" / "SKILL.md"), str(src / "addendum.md"), str(out)]
    )
    assert build_skill.main() == 0
    assert out.read_bytes() == (src / "typesafe-ai" / "SKILL.md").read_bytes()


@pytest.mark.parametrize("args", [[], ["a"], ["a", "b"], ["a", "b", "c", "d"]])
def test_main_wrong_arg_count_exits_with_usage(build_skill: ModuleType, args: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_skill.main(args)
    assert exc.value.code == "usage: python3 build-skill.py upstream.SKILL.md addendum.md out/SKILL.md"


def test_main_rejects_changed_upstream(build_skill: ModuleType, tmp_path: Path) -> None:
    up = tmp_path / "up.md"
    up.write_text(small_upstream(), encoding="utf-8")
    add = tmp_path / "add.md"
    add.write_text("ADD\n", encoding="utf-8")
    out = tmp_path / "out.md"
    sha = hashlib.sha256(small_upstream().encode()).hexdigest()
    with pytest.raises(SystemExit) as exc:
        build_skill.main([str(up), str(add), str(out)])
    assert exc.value.code == f"upstream SKILL.md changed (sha256 {sha}); re-read it before re-vendoring"
    assert not out.exists()


def test_main_dunder_main_exits_zero(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    src = skill_dir(root)
    out = tmp_path / "o.md"
    monkeypatch.setattr(
        sys, "argv", ["build-skill.py", str(src / "upstream" / "SKILL.md"), str(src / "addendum.md"), str(out)]
    )
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(src / "build-skill.py"), run_name="__main__")
    assert exc.value.code == 0
    assert out.read_bytes() == (src / "typesafe-ai" / "SKILL.md").read_bytes()
    assert capsys.readouterr().out.startswith("description chars: 679 -> 469\n")
