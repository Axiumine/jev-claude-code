#!/usr/bin/env python3
"""Build the local skill = pinned upstream typesafe-ai/skills SKILL.md (MIT) + shorter description + local addendum.

Upstream: https://github.com/typesafe-ai/skills  commit 65a39f393687675ce170e6094757de20370365b9
          skills/typesafe-ai/SKILL.md  sha256 71ea90d7906c6554c4f4c460ef7361b2d26f59116ccdae986dc6d997b9389f52
Usage:    python3 build-skill.py upstream.SKILL.md addendum.md out/SKILL.md
"""
import hashlib, re, sys, difflib

UP_SHA = "71ea90d7906c6554c4f4c460ef7361b2d26f59116ccdae986dc6d997b9389f52"
NEW_DESC = (
    "description: >\n"
    "  Jev / TypeSafe System One (reached through OpenRouter on this machine): fast typed judgments\n"
    "  (Choice, Noul, Score) with probabilities and no free text, about 200 ms and $0.00002 per call.\n"
    "  Use when writing or designing application code that needs a bounded judgment on text or JSON\n"
    "  state (routing, triage, moderation, gating, verification, ranking), or when an LLM \"return JSON\"\n"
    "  step could become a typed decision. Not for generation, math, dates or counting.\n"
)

up_path, add_path, out_path = sys.argv[1:4]
up = open(up_path, encoding="utf-8").read()
got = hashlib.sha256(up.encode("utf-8")).hexdigest()
if got != UP_SHA:
    sys.exit(f"upstream SKILL.md changed (sha256 {got}); re-read it before re-vendoring")
m = re.match(r"---\n(.*?)\n---\n", up, re.S)
fm = m.group(1)
new_fm = re.sub(r"description: >\n(?:  .*\n?)+", NEW_DESC, fm + "\n", count=1).rstrip("\n")
body = up[m.end():]
out = "---\n" + new_fm + "\n---\n" + body.rstrip("\n") + "\n" + open(add_path, encoding="utf-8").read()
open(out_path, "w", encoding="utf-8").write(out)

desc_chars = len(re.search(r"description: >\n((?:  .*\n?)+)", new_fm + "\n").group(1))
old_desc_chars = len(re.search(r"description: >\n((?:  .*\n?)+)", fm + "\n").group(1))
print(f"description chars: {old_desc_chars} -> {desc_chars}")
print(f"body chars: upstream {len(up)}, local {len(out)} (~{len(out)//4} tokens when the skill loads)")
open(out_path + ".diff", "w", encoding="utf-8").writelines(
    difflib.unified_diff(up.splitlines(True), out.splitlines(True), "upstream/SKILL.md", "local/SKILL.md"))
