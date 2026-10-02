#!/usr/bin/env python3
"""Keep private links, env files and project keys out of commits and pushes (git hooks).

Usage: git_guard.py files PATH...      names and whole contents of the given files (pre-commit,
                                        staged or --all-files); a symlink's target
       git_guard.py staged              what the index adds
       git_guard.py message FILE        the commit message (commit-msg)
       git_guard.py pushed [BASE TIP]   commits, their messages and the messages of their annotated
                                        tags (pre-push): BASE..TIP, or with a zero/empty BASE what no
                                        remote has; without arguments every commit of the local
                                        branches and tags (and PRE_COMMIT_TO_REF and
                                        PRE_COMMIT_LOCAL_BRANCH) that the remote PRE_COMMIT_REMOTE_NAME
                                        lacks, because pre-commit tells the hooks about the first
                                        pushed ref only. A push of nothing but a tag whose commit the
                                        remote has runs no pre-commit hook: its message goes unchecked.
       git_guard.py checked-out [TIP]   TIP (default PRE_COMMIT_TO_REF) is HEAD and nothing is staged
                                        or untracked, so the push-stage checks test what is pushed
Generic secrets are gitleaks' job. A line that contains "git-guard: allow" is not checked.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ALLOW = "git-guard: allow"
RULES = (
    (
        "private claude.ai link",
        re.compile(
            r"(?i)(?<![\w.@-])(?:https?://(?:[^/@\s]+@)?)?(?:www\.)?claude\.ai(?::\d+)?/(?:code/)?"
            r"(?:(?:artifact|chat|project|share)/|session_)[\w-]+"
        ),
    ),
    (
        "Qodana Cloud report link",
        re.compile(
            r"(?i)(?<![\w.@-])(?:https?://(?:[^/@\s]+@)?)?(?:www\.)?qodana\.cloud(?::\d+)?/projects/[\w-]+/reports/[\w-]+"
        ),
    ),
    ("OpenRouter API key", re.compile(r"sk-or-v1-[0-9a-f]{64}")),
    ("QODANA_TOKEN value", re.compile(r"QODANA_TOKEN[\"']?\s*[=:]\s*[\"']?[\w.-]{20,}")),
)
ENV_FILE = re.compile(r"(?i)(?:^|/)(?:\.env(?:[._-][^/]*)?|\.envrc|[^/]+\.env)$")
ENV_TEMPLATE = re.compile(r"(?i)\.(?:example|sample|template)$")
# A hunk header; a merge's combined diff has one @ more per parent and one column per parent.
HUNK = re.compile(r"(@@+) (?:-\S+ )+\+(\d+)")
HEADER = -1  # scan_patch: in a file header, before its first hunk
START = ("?", 0, HEADER)  # scan_patch: path, line number, columns
# The scissors line of `commit -v` for any core.commentChar or core.commentString.
SCISSORS = re.compile(r"^\S+ -{24} >8 -{24}$", re.MULTILINE)
DIFF_START = re.compile(r"^diff --(?:git|cc|combined) ", re.MULTILINE)
# Every kind of added content: binary-looking files and type changes (a symlink's target) too.
DIFF = ("--diff-filter=ACMRT", "--text", "--no-textconv", "--no-ext-diff", "--no-color")
# Git settings that would change the diff's paths, reset for every call.
GIT_CONFIG = (
    "core.quotePath=false",
    "diff.noprefix=false",
    "diff.mnemonicPrefix=false",
    "diff.srcPrefix=a/",
    "diff.dstPrefix=b/",
)
TAG_MESSAGE = "%(if:equals=tag)%(objecttype)%(then)%(contents)%(end)"  # annotated tags only
MASK_LEN = 12
USAGE = "usage: git_guard.py files PATH... | staged | message FILE | pushed [BASE TIP] | checked-out [TIP]"


@dataclass(frozen=True)
class Finding:
    where: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.where}: {self.rule}: {self.detail}"


def git(*args: str) -> str:
    """Output of a git command; a git error raises CalledProcessError."""
    cmd = ["git", "--no-replace-objects", *(arg for c in GIT_CONFIG for arg in ("-c", c)), *args]
    return subprocess.run(cmd, check=True, capture_output=True).stdout.decode(errors="replace")  # noqa: S603


def mask(text: str) -> str:
    """Enough of a match to recognise it, never the whole secret."""
    return text if len(text) <= MASK_LEN else text[:MASK_LEN] + "…"


def scan_line(text: str, where: str) -> list[Finding]:
    text = text.replace("\0", "")  # UTF-16 text shows up with a NUL beside every character
    if ALLOW in text:
        return []
    return [Finding(where, rule, mask(m.group())) for rule, rx in RULES for m in rx.finditer(text)]


def scan_text(text: str, where: str) -> list[Finding]:
    return [f for n, line in enumerate(text.split("\n"), start=1) for f in scan_line(line, f"{where}:{n}")]


def scan_message(text: str, where: str) -> list[Finding]:
    """A commit message up to the last scissors line that a diff follows (`commit -v`). Comment
    lines are checked too, since `commit -m` keeps them, and so is a scissors line typed in."""
    cuts = [m.start() for m in SCISSORS.finditer(text) if DIFF_START.search(text, m.end())]
    return scan_text(text[: cuts[-1]] if cuts else text, where)


def scan_patch(patch: str, prefix: str = "") -> list[Finding]:
    """The lines a patch adds: '+' lines, or in a merge's combined diff the lines no parent has."""
    findings: list[Finding] = []
    path, line_no, cols = START
    for line in patch.split("\n"):  # not splitlines(): a form feed or U+2028 is not a diff line break
        if line.startswith("diff "):
            cols = HEADER
        elif cols == HEADER and line.startswith("+++ "):
            path = line[4:].rstrip("\t").strip('"').removeprefix("b/")  # git adds a tab after a name with a space
        elif hunk := HUNK.match(line):
            cols, line_no = len(hunk.group(1)) - 1, int(hunk.group(2))
        elif cols != HEADER and "-" not in line[:cols] and not line.startswith("\\"):  # a new-version line
            if line[:cols] == "+" * cols:
                findings += scan_line(line[cols:], f"{prefix}{path}:{line_no}")
            line_no += 1
    return findings


def env_files(paths: Iterable[str], prefix: str = "") -> list[Finding]:
    return [
        Finding(f"{prefix}{p}", "env file", "keep it untracked (see .gitignore)")
        for p in paths
        if ENV_FILE.search(p) and not ENV_TEMPLATE.search(p)
    ]


def files(paths: list[str]) -> list[Finding]:
    findings = env_files(paths)
    for p in paths:
        path = Path(p)
        # os.readlink, not Path.readlink: a Path would turn the target's '//' into '/'
        text = os.readlink(path) if path.is_symlink() else path.read_bytes().decode(errors="replace")  # noqa: PTH115
        findings += scan_text(text, p)
    return findings


def staged() -> list[Finding]:
    names = git("diff", "--cached", "--name-only", "-z", *DIFF).split("\0")
    patch = git("diff", "--cached", "-U0", *DIFF)
    return env_files(n for n in names if n) + scan_patch(patch)


def commits(base: str, tip: str) -> list[str]:
    """BASE..TIP; without a base (empty or all zeros: a new branch), what no remote has yet."""
    if not base.strip("0"):
        return git("rev-list", tip, "--not", "--remotes").split()
    return git("rev-list", f"{base}..{tip}").split()


def unpushed(remote: str | None, tips: list[str]) -> list[str]:
    """Every commit of the local branches and tags, and of TIPS, that REMOTE lacks; every remote when
    REMOTE is unnamed or not a configured remote (a URL)."""
    remotes = f"--remotes={remote}" if remote in git("remote").split() else "--remotes"
    return git("rev-list", "--branches", "--tags", *tips, "--not", remotes).split()


def pushed(shas: list[str]) -> list[Finding]:
    """What the commits add (a merge: what none of its parents has), their messages and their tags' messages."""
    findings: list[Finding] = []
    for sha in shas:
        prefix = f"{sha[:12]} "
        show = ("show", "--format=", *DIFF, sha)
        names = git(*show, "--name-only", "-z").split("\0")
        findings += env_files((n for n in names if n), prefix) + scan_patch(git(*show, "-U0"), prefix)
        findings += scan_text(git("log", "-1", "--format=%B", sha), f"{prefix}message")
        findings += scan_text(git("tag", "--points-at", sha, f"--format={TAG_MESSAGE}"), f"{prefix}tag message")
    return findings


def checked_out(tip: str | None) -> list[Finding]:
    findings = []
    if tip and git("rev-parse", "HEAD").strip() != git("rev-parse", f"{tip}^{{commit}}").strip():  # tags too
        findings.append(Finding("HEAD", "not the pushed commit", f"check out {tip[:12]} or push HEAD"))
    if git("status", "--porcelain", "--untracked-files=normal").strip():
        findings.append(Finding("working tree", "staged or untracked changes", "commit or stash them first"))
    return findings


def run(args: list[str]) -> list[Finding] | None:
    """The findings of one command, or None when the arguments are not a command."""
    env = os.environ
    findings = None
    match args:
        case ["files", *paths]:
            findings = files(paths)
        case ["staged"]:
            findings = staged()
        case ["message", path]:
            findings = scan_message(Path(path).read_bytes().decode(errors="replace"), "commit message")
        case ["pushed"]:
            tips = [t for t in (env.get("PRE_COMMIT_TO_REF"), env.get("PRE_COMMIT_LOCAL_BRANCH")) if t]
            findings = pushed(unpushed(env.get("PRE_COMMIT_REMOTE_NAME"), tips))
        case ["pushed", base, tip]:
            findings = pushed(commits(base, tip))
        case ["checked-out"]:
            findings = checked_out(env.get("PRE_COMMIT_TO_REF") or env.get("PRE_COMMIT_LOCAL_BRANCH"))
        case ["checked-out", tip]:
            findings = checked_out(tip)
    return findings


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        findings = run(args)
    except subprocess.CalledProcessError as e:
        print(f"git-guard: {' '.join(e.cmd)} failed: {e.stderr.decode(errors='replace').strip()}", file=sys.stderr)
        return 2
    if findings is None:
        print(USAGE, file=sys.stderr)
        return 2
    for f in findings:
        print(f, file=sys.stderr)
    if findings:
        hint = f"fix them, or mark a deliberate line with '{ALLOW}'"
        print(f"git-guard: {len(findings)} problem(s); {hint}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
