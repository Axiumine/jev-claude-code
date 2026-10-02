#!/usr/bin/env python3
"""Build the local skill = pinned upstream typesafe-ai/skills SKILL.md (MIT) + shorter description + local addendum.

Upstream: https://github.com/typesafe-ai/skills  commit 65a39f393687675ce170e6094757de20370365b9
          skills/typesafe-ai/SKILL.md  sha256 71ea90d7906c6554c4f4c460ef7361b2d26f59116ccdae986dc6d997b9389f52
Usage:    python3 build-skill.py upstream.SKILL.md addendum.md out/SKILL.md
"""

from __future__ import annotations

import difflib
import hashlib
import re
import sys
from pathlib import Path

UP_SHA = "71ea90d7906c6554c4f4c460ef7361b2d26f59116ccdae986dc6d997b9389f52"
ARGS = ("upstream.SKILL.md", "addendum.md", "out/SKILL.md")
USAGE = "usage: python3 build-skill.py " + " ".join(ARGS)
NEW_DESC = (
    "description: >\n"
    "  Jev / TypeSafe System One (reached through OpenRouter on this machine): fast typed judgments\n"
    "  (Choice, Noul, Score) with probabilities and no free text, about 200 ms and $0.00002 per call.\n"
    "  Use when writing or designing application code that needs a bounded judgment on text or JSON\n"
    '  state (routing, triage, moderation, gating, verification, ranking), or when an LLM "return JSON"\n'
    "  step could become a typed decision. Not for generation, math, dates or counting.\n"
)
FRONT_MATTER = re.compile(r"---\n(.*?)\n---\n", re.DOTALL)
DESC = re.compile(r"description: >\n((?:  .*\n?)+)")


def desc_len(front_matter: str) -> int:
    """Length of the folded `description: >` block in YAML front matter."""
    m = DESC.search(front_matter + "\n")
    if m is None:
        raise ValueError("front matter has no folded 'description: >' block")
    return len(m.group(1))


def build_skill(up: str, addendum: str) -> tuple[str, int, int]:
    """Return (local SKILL.md, old description length, new description length)."""
    m = FRONT_MATTER.match(up)
    if m is None:
        raise ValueError("upstream SKILL.md has no front matter")
    fm = m.group(1)
    old_chars = desc_len(fm)
    new_fm = DESC.sub(NEW_DESC, fm + "\n", count=1).rstrip("\n")
    out = "---\n" + new_fm + "\n---\n" + up[m.end() :].rstrip("\n") + "\n" + addendum
    return out, old_chars, desc_len(new_fm)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != len(ARGS):
        sys.exit(USAGE)
    up_path, add_path, out_path = (Path(a) for a in args)
    up = up_path.read_text(encoding="utf-8")  # pragma: no mutate  (utf-8 == locale default: equivalent mutants)
    got = hashlib.sha256(up.encode()).hexdigest()
    if got != UP_SHA:
        sys.exit(f"upstream SKILL.md changed (sha256 {got}); re-read it before re-vendoring")
    addendum = add_path.read_text(encoding="utf-8")  # pragma: no mutate
    out, old_chars, new_chars = build_skill(up, addendum)
    out_path.write_text(out, encoding="utf-8")  # pragma: no mutate
    print(f"description chars: {old_chars} -> {new_chars}")
    print(f"body chars: upstream {len(up)}, local {len(out)} (~{len(out) // 4} tokens when the skill loads)")
    diff = difflib.unified_diff(
        up.splitlines(keepends=True), out.splitlines(keepends=True), "upstream/SKILL.md", "local/SKILL.md"
    )
    diff_path, diff_text = Path(f"{out_path}.diff"), "".join(diff)
    diff_path.write_text(diff_text, encoding="utf-8")  # pragma: no mutate
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
