"""Tests for scripts/git_guard.py, run against real throwaway git repositories."""

from __future__ import annotations

import os
import re
import runpy
import shlex
import subprocess
import sys
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import Any

import pytest

from scripts import git_guard
from scripts.git_guard import Finding

# Fake secrets and private links are assembled from pieces, so this file never contains a literal match.
CLAUDE_LINK = "https://claude" + ".ai/artifact/abc-123"
CLAUDE_LINK_2 = "https://claude" + ".ai/chat/xyz-789"
QODANA_LINK = "https://qodana" + ".cloud/projects/p1/reports/r-2"
KEY = "sk-or-" + "v1-" + "a" * 64
TOKEN_NAME = "QODANA" + "_TOKEN"
TOKEN = TOKEN_NAME + "=" + "t" * 20

ELL = "…"
LINK_DETAIL = "https://clau" + ELL
QODANA_DETAIL = "https://qoda" + ELL
KEY_DETAIL = "sk-or-v1-aaa" + ELL
TOKEN_DETAIL = "QODANA_TOKEN" + ELL

BARE_LINK = "claude" + ".ai/chat/xyz-789"  # no scheme, and no '//' for pathlib to collapse in a symlink target
BARE_DETAIL = "claude.ai/ch" + ELL

LINK_RULE = "private claude.ai link"
QODANA_RULE = "Qodana Cloud report link"
KEY_RULE = "OpenRouter API key"
TOKEN_RULE = "QODANA_TOKEN value"  # noqa: S105

GIT_CONFIG = [
    "core.quotePath=false",
    "diff.noprefix=false",
    "diff.mnemonicPrefix=false",
    "diff.srcPrefix=a/",
    "diff.dstPrefix=b/",
]
GIT = ["git", "--no-replace-objects", *(arg for setting in GIT_CONFIG for arg in ("-c", setting))]
GIT_TEXT = " ".join(GIT)
ENV_DETAIL = "keep it untracked (see .gitignore)"
SUMMARY = "git-guard: {n} problem(s); fix them, or mark a deliberate line with 'git-guard: allow'"
USAGE = "usage: git_guard.py files PATH... | staged | message FILE | pushed [BASE TIP] | checked-out [TIP]"


LINE_BREAKS = ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}", "\r"]
DIRTY = Finding("working tree", "staged or untracked changes", "commit or stash them first")


def utf16(text: str) -> str:
    """TEXT as git shows UTF-16 without a decoder: a NUL beside every character."""
    return "\0".join(text) + "\0"


def link_finding(where: str) -> Finding:
    return Finding(where, LINK_RULE, LINK_DETAIL)


def key_finding(where: str) -> Finding:
    return Finding(where, KEY_RULE, KEY_DETAIL)


# ---- environment and repositories ------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_git_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No git variable or configuration of the surrounding hook or user leaks into the throwaway repositories."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    for name in list(os.environ):
        if name.startswith(("GIT_", "PRE_COMMIT_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("LC_ALL", "C")


@dataclass
class Repo:
    path: Path

    def git(self, *args: str, check: bool = True) -> str:
        done = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=self.path,
            check=check,
            capture_output=True,
            encoding="utf-8",
        )
        return done.stdout

    def write(self, name: str, content: str | bytes) -> None:
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")

    def stage(self, files: dict[str, str | bytes]) -> None:
        for name, content in files.items():
            self.write(name, content)
        self.git("add", "-A")

    def commit(self, files: dict[str, str | bytes], *messages: str) -> str:
        """Commit the given files (all changes staged) and return the new commit's full hash."""
        self.stage(files)
        args = [arg for message in messages or ("c",) for arg in ("-m", message)]
        self.git("commit", "-q", "--cleanup=verbatim", *args)
        return self.head()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def checkout(self, *args: str) -> None:
        self.git("checkout", "-q", *args)


def init_repo(path: Path) -> Repo:
    path.mkdir()
    repo = Repo(path)
    repo.git("init", "-q", "-b", "main", "--template=")
    with (path / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write("[user]\n\tname = Test\n\temail = t@example.com\n[commit]\n\tgpgsign = false\n")
    return repo


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    repo = init_repo(tmp_path / "work")
    monkeypatch.chdir(repo.path)
    return repo


@pytest.fixture
def remote(repo: Repo, tmp_path: Path) -> Repo:
    """A local bare remote called origin; the returned Repo is the bare repository."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    bare_repo = Repo(bare)
    bare_repo.git("init", "-q", "--bare", "-b", "main", "--template=")
    repo.git("remote", "add", "origin", str(bare))
    return bare_repo


@pytest.fixture
def history(repo: Repo) -> list[str]:
    """Three clean commits, oldest first."""
    return [repo.commit({"a.txt": f"{n}\n"}, f"commit {n}") for n in range(1, 4)]


def sha12(sha: str) -> str:
    return sha[:12]


def pushed_range(base: str, tip: str) -> list[Finding]:
    """What `pushed BASE TIP` reports."""
    return git_guard.pushed(git_guard.commits(base, tip))


# ---- Finding, mask, git ---------------------------------------------------------------------------------------------


def test_finding_str_joins_where_rule_detail() -> None:
    assert str(Finding("f.txt:3", "some rule", "some detail")) == "f.txt:3: some rule: some detail"


def test_finding_is_frozen_and_comparable() -> None:
    finding = Finding("a", "b", "c")
    assert finding == Finding("a", "b", "c")
    assert finding != Finding("a", "b", "d")
    with pytest.raises(FrozenInstanceError):
        finding.where = "x"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ""),
        ("short", "short"),
        ("a" * 11, "a" * 11),
        ("a" * 12, "a" * 12),
        ("a" * 13, "a" * 12 + ELL),
        ("0123456789abcdefghij", "0123456789ab" + ELL),
    ],
)
def test_mask(text: str, expected: str) -> None:
    assert git_guard.mask(text) == expected


def test_constants() -> None:
    assert git_guard.ALLOW == "git-guard: allow"
    assert git_guard.MASK_LEN == 12
    assert git_guard.USAGE == USAGE
    assert git_guard.SCISSORS.pattern == r"^\S+ -{24} >8 -{24}$"
    assert git_guard.SCISSORS.flags & re.MULTILINE
    assert git_guard.DIFF_START.pattern == r"^diff --(?:git|cc|combined) "
    assert git_guard.DIFF_START.flags & re.MULTILINE
    assert git_guard.DIFF == ("--diff-filter=ACMRT", "--text", "--no-textconv", "--no-ext-diff", "--no-color")
    assert list(git_guard.GIT_CONFIG) == GIT_CONFIG
    assert git_guard.HEADER == -1
    assert git_guard.START == ("?", 0, -1)
    assert git_guard.TAG_MESSAGE == "%(if:equals=tag)%(objecttype)%(then)%(contents)%(end)"


def test_git_returns_stdout(repo: Repo) -> None:
    assert git_guard.git("rev-parse", "--is-inside-work-tree") == "true\n"
    repo.commit({"x.txt": "1\n"})
    assert git_guard.git("rev-parse", "HEAD") == repo.head() + "\n"


def test_git_does_not_quote_non_ascii_paths(repo: Repo) -> None:
    repo.stage({"café.txt": "1\n"})
    assert git_guard.git("ls-files") == "café.txt\n"


def test_git_failure_raises_with_stderr(repo: Repo) -> None:
    with pytest.raises(subprocess.CalledProcessError) as exc:
        git_guard.git("rev-parse", "--verify", "nosuchref")
    assert exc.value.returncode == 128
    assert exc.value.cmd == [*GIT, "rev-parse", "--verify", "nosuchref"]
    assert exc.value.stderr == b"fatal: Needed a single revision\n"


def test_git_decodes_invalid_utf8_with_replacement(repo: Repo) -> None:
    repo.commit({"f.txt": b"\xff caf\xe9\n"})
    assert git_guard.git("show", "HEAD:f.txt") == "� caf�\n"


# ---- rules ----------------------------------------------------------------------------------------------------------


def test_rule_names_and_order() -> None:
    assert [name for name, _ in git_guard.RULES] == [LINK_RULE, QODANA_RULE, KEY_RULE, TOKEN_RULE]


def matched(rule_name: str, text: str) -> str | None:
    rx = next(rx for name, rx in git_guard.RULES if name == rule_name)
    m = rx.search(text)
    return m.group() if m else None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (CLAUDE_LINK, CLAUDE_LINK),
        (CLAUDE_LINK.replace("https", "http"), CLAUDE_LINK.replace("https", "http")),
        ("https://claude" + ".ai/code/artifact/0a1-b)", "https://claude" + ".ai/code/artifact/0a1-b"),
        ("https://claude" + ".ai/chat/x_1?utm=1", "https://claude" + ".ai/chat/x_1"),
        ("https://claude" + ".ai/code/chat/x", "https://claude" + ".ai/code/chat/x"),
        ("https://claude" + ".ai/project/p-1/", "https://claude" + ".ai/project/p-1"),
        ("https://claude" + ".ai/share/s-1 and more", "https://claude" + ".ai/share/s-1"),
        ("see <https://claude" + ".ai/chat/c1>", "https://claude" + ".ai/chat/c1"),
        ("xhttps://claude" + ".ai/chat/c1", "claude" + ".ai/chat/c1"),
        # without a scheme, with a port, with www, in any case
        ("claude" + ".ai/artifact/x", "claude" + ".ai/artifact/x"),
        ("www.claude" + ".ai/chat/c1?x", "www.claude" + ".ai/chat/c1"),
        ("CLAUDE" + ".AI/ARTIFACT/X", "CLAUDE" + ".AI/ARTIFACT/X"),
        ("WWW.Claude" + ".Ai/Code/Session_X", "WWW.Claude" + ".Ai/Code/Session_X"),
        ("claude" + ".ai:8080/project/p1", "claude" + ".ai:8080/project/p1"),
        ("https://claude" + ".ai:443/share/s1", "https://claude" + ".ai:443/share/s1"),
        ("http://www.claude" + ".ai:80/code/artifact/a", "http://www.claude" + ".ai:80/code/artifact/a"),
        ("www.claude" + ".ai:1/chat/c", "www.claude" + ".ai:1/chat/c"),
        ("ftp://claude" + ".ai/artifact/x", "claude" + ".ai/artifact/x"),
        ("httpss://claude" + ".ai/artifact/x", "claude" + ".ai/artifact/x"),
        ("https://example.com/claude" + ".ai/artifact/x", "claude" + ".ai/artifact/x"),
        ("(claude" + ".ai/chat/c1)", "claude" + ".ai/chat/c1"),
        ('"claude' + '.ai/chat/c1"', "claude" + ".ai/chat/c1"),
        ("see claude" + ".ai/chat/c1", "claude" + ".ai/chat/c1"),
        ("<claude" + ".ai/chat/c1>", "claude" + ".ai/chat/c1"),
        ("a:claude" + ".ai/chat/c1", "claude" + ".ai/chat/c1"),
        ("https://www.claude" + ".ai/artifact/a-1", "https://www.claude" + ".ai/artifact/a-1"),
        ("http://www.claude" + ".ai/chat/c1?x", "http://www.claude" + ".ai/chat/c1"),
        ("HTTPS://claude" + ".ai/artifact/x", "HTTPS://claude" + ".ai/artifact/x"),
        ("https://CLAUDE" + ".AI/artifact/x", "https://CLAUDE" + ".AI/artifact/x"),
        ("Https://Www.Claude" + ".Ai/Chat/AbC-1", "Https://Www.Claude" + ".Ai/Chat/AbC-1"),
        ("https://claude" + ".ai/ARTIFACT/x", "https://claude" + ".ai/ARTIFACT/x"),
        ("https://claude" + ".ai/code/session_01AbC", "https://claude" + ".ai/code/session_01AbC"),
        ("https://claude" + ".ai/session_01-x/more", "https://claude" + ".ai/session_01-x"),
        ("https://www.claude" + ".ai/code/session_x_1)", "https://www.claude" + ".ai/code/session_x_1"),
        ("https://claude" + ".ai/CODE/SESSION_ABC", "https://claude" + ".ai/CODE/SESSION_ABC"),
        # near misses
        ("https://claude" + ".ai/", None),
        ("https://claude" + ".ai/artifact/", None),
        ("https://claude" + ".ai/artifact/?x=1", None),
        ("https://claude" + ".ai/new", None),
        ("https://claude" + ".ai/docs/artifact/x", None),
        ("https://claude" + ".ai/code/code/x", None),
        ("https://claude" + ".ai/code/new/x", None),
        ("https://claude" + "Xai/artifact/x", None),
        ("https://docs.claude" + ".ai/artifact/x", None),
        # a name that merely ends in claude.ai: a word character, '.', '@' or '-' comes right before it
        ("x.claude" + ".ai/chat/c1", None),
        ("a.b.claude" + ".ai/chat/c1", None),
        ("user@claude" + ".ai/chat/c1", None),
        ("foo-claude" + ".ai/chat/c1", None),
        ("xclaude" + ".ai/chat/c1", None),
        ("_claude" + ".ai/chat/c1", None),
        ("9claude" + ".ai/chat/c1", None),
        ("-claude" + ".ai/chat/c1", None),
        (".claude" + ".ai/chat/c1", None),
        ("@claude" + ".ai/chat/c1", None),
        ("x.www.claude" + ".ai/chat/c1", None),
        ("xwww.claude" + ".ai/chat/c1", None),
        ("-www.claude" + ".ai/chat/c1", None),
        ("user@www.claude" + ".ai/chat/c1", None),
        ("https://x.claude" + ".ai/chat/c1", None),
        ("https://foo-claude" + ".ai/chat/c1", None),
        ("https://claude" + ".ai/chat", None),
        ("claude" + ".ai/chat/", None),
        ("claude" + ".ai:/chat/c1", None),
        ("claude" + ".ai:80a/chat/c1", None),
        ("claude" + ".ai:x/chat/c1", None),
        ("claude" + ".aix/chat/c1", None),
        ("claude" + ".ai/session_", None),
        ("https://claude" + ".ai/session_", None),
        ("https://claude" + ".ai/session/x", None),
        ("https://claude" + ".ai/code/session_", None),
        ("https://claude" + ".ai/code/code/session_x", None),
        ("https://claude" + ".ai/sessions_x", None),
        ("https://www.www.claude" + ".ai/artifact/x", None),
        ("https://wwwclaude" + ".ai/artifact/x", None),
        ("https://www" + ".claude" + ".ai/x/artifact/x", None),
    ],
)
def test_rule_claude_link(text: str, expected: str | None) -> None:
    assert matched(LINK_RULE, text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (QODANA_LINK, QODANA_LINK),
        (QODANA_LINK.replace("https", "http"), QODANA_LINK.replace("https", "http")),
        (QODANA_LINK + "/problems?x=1", QODANA_LINK),
        ("[r](" + QODANA_LINK + ")", QODANA_LINK),
        ("https://www.qodana" + ".cloud/projects/p1/reports/r", "https://www.qodana" + ".cloud/projects/p1/reports/r"),
        ("HTTPS://QODANA" + ".CLOUD/PROJECTS/P1/REPORTS/R-2", "HTTPS://QODANA" + ".CLOUD/PROJECTS/P1/REPORTS/R-2"),
        ("Http://Www.Qodana" + ".Cloud/Projects/p/Reports/r", "Http://Www.Qodana" + ".Cloud/Projects/p/Reports/r"),
        # without a scheme, with a port, in any case
        ("qodana" + ".cloud/projects/p1/reports/r-2", "qodana" + ".cloud/projects/p1/reports/r-2"),
        ("www.qodana" + ".cloud/projects/p/reports/r?x", "www.qodana" + ".cloud/projects/p/reports/r"),
        ("QODANA" + ".CLOUD/PROJECTS/P/REPORTS/R", "QODANA" + ".CLOUD/PROJECTS/P/REPORTS/R"),
        ("qodana" + ".cloud:8443/projects/p/reports/r", "qodana" + ".cloud:8443/projects/p/reports/r"),
        ("https://qodana" + ".cloud:443/projects/p/reports/r", "https://qodana" + ".cloud:443/projects/p/reports/r"),
        (
            "http://www.qodana" + ".cloud:80/projects/p/reports/r",
            "http://www.qodana" + ".cloud:80/projects/p/reports/r",
        ),
        ("ftp://qodana" + ".cloud/projects/p1/reports/r", "qodana" + ".cloud/projects/p1/reports/r"),
        ("(qodana" + ".cloud/projects/p1/reports/r)", "qodana" + ".cloud/projects/p1/reports/r"),
        # near misses
        ("x.qodana" + ".cloud/projects/p1/reports/r", None),
        ("user@qodana" + ".cloud/projects/p1/reports/r", None),
        ("foo-qodana" + ".cloud/projects/p1/reports/r", None),
        ("xqodana" + ".cloud/projects/p1/reports/r", None),
        ("_qodana" + ".cloud/projects/p1/reports/r", None),
        ("x.www.qodana" + ".cloud/projects/p1/reports/r", None),
        ("https://x.qodana" + ".cloud/projects/p1/reports/r", None),
        ("qodana" + ".cloud:/projects/p1/reports/r", None),
        ("qodana" + ".cloud:80a/projects/p1/reports/r", None),
        ("https://qodana" + ".cloud/projects/p1", None),
        ("https://qodana" + ".cloud/projects/p1/reports/", None),
        ("https://qodana" + ".cloud/projects//reports/r", None),
        ("https://qodana" + ".cloud/projects/p1/other/r", None),
        ("https://qodana" + ".cloud/project/p1/reports/r", None),
        ("https://qodana" + "Xcloud/projects/p1/reports/r", None),
        ("https://qodana" + ".com/projects/p1/reports/r", None),
        ("https://www.www.qodana" + ".cloud/projects/p1/reports/r", None),
        ("https://wwwqodana" + ".cloud/projects/p1/reports/r", None),
    ],
)
def test_rule_qodana_link(text: str, expected: str | None) -> None:
    assert matched(QODANA_RULE, text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (KEY, KEY),
        ("k=" + KEY + " x", KEY),
        (KEY + "a", KEY),
        ("sk-or-" + "v1-" + "0123456789abcdef" * 4, "sk-or-" + "v1-" + "0123456789abcdef" * 4),
        # near misses
        ("sk-or-" + "v1-" + "a" * 63, None),
        ("sk-or-" + "v1-" + "a" * 63 + "g", None),
        ("sk-or-" + "v1-" + "A" * 64, None),
        ("sk-or-" + "v2-" + "a" * 64, None),
        ("sk-or-" + "a" * 64, None),
        ("sk-ant-" + "v1-" + "a" * 64, None),
        ("sk-or-" + "v1" + "a" * 64, None),
        ("sk-or-" + "v1-" + "a" * 32 + "-" + "a" * 31, None),
        ("Sk-or-" + "v1-" + "a" * 64, None),
    ],
)
def test_rule_openrouter_key(text: str, expected: str | None) -> None:
    assert matched(KEY_RULE, text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (TOKEN, TOKEN),
        (TOKEN_NAME + ": " + "t" * 20, TOKEN_NAME + ": " + "t" * 20),
        (TOKEN_NAME + " = " + "t" * 20, TOKEN_NAME + " = " + "t" * 20),
        (TOKEN_NAME + '="' + "t" * 20 + '"', TOKEN_NAME + '="' + "t" * 20),
        (TOKEN_NAME + "='" + "t" * 20 + "'", TOKEN_NAME + "='" + "t" * 20),
        (TOKEN_NAME + "=" + "t" * 25 + " x", TOKEN_NAME + "=" + "t" * 25),
        (TOKEN_NAME + "=" + "a.b-c_d" * 4, TOKEN_NAME + "=" + "a.b-c_d" * 4),
        (TOKEN_NAME + "=" + "0123456789" * 2, TOKEN_NAME + "=" + "0123456789" * 2),
        ("export " + TOKEN, TOKEN),
        ('"' + TOKEN_NAME + '": "' + "t" * 20 + '"', TOKEN_NAME + '": "' + "t" * 20),
        ('{"' + TOKEN_NAME + '":"' + "t" * 20 + '"}', TOKEN_NAME + '":"' + "t" * 20),
        ("'" + TOKEN_NAME + "': '" + "t" * 22 + "'", TOKEN_NAME + "': '" + "t" * 22),
        ('"' + TOKEN_NAME + '" : ' + "t" * 20, TOKEN_NAME + '" : ' + "t" * 20),
        ('"' + TOKEN_NAME + '"=' + "t" * 20, TOKEN_NAME + '"=' + "t" * 20),
        (TOKEN_NAME + "' = " + "t" * 20, TOKEN_NAME + "' = " + "t" * 20),
        (TOKEN_NAME + "=" + "t" * 20 + "$x", TOKEN),
        # near misses
        (TOKEN_NAME + "=" + "t" * 19, None),
        (TOKEN_NAME + "=" + "t" * 9 + " " + "t" * 10, None),
        (TOKEN_NAME + "=${" + TOKEN_NAME + "}", None),
        (TOKEN_NAME + "=$(cat file-with-the-token)", None),
        (TOKEN_NAME, None),
        (TOKEN_NAME + "==" + "t" * 20, None),
        (TOKEN_NAME + "X=" + "t" * 20, None),
        (TOKEN_NAME.lower() + "=" + "t" * 20, None),
        ("QODANA" + "-TOKEN=" + "t" * 20, None),
        (TOKEN_NAME + "=", None),
        (TOKEN_NAME + "=<your token here>", None),
        ('"' + TOKEN_NAME + '": "' + "t" * 19 + '"', None),
        ('"' + TOKEN_NAME + '": ""', None),
        ('"' + TOKEN_NAME + '"', None),
        (TOKEN_NAME + '""=' + "t" * 20, None),
        ('"' + TOKEN_NAME + 'X": "' + "t" * 20 + '"', None),
        ('"' + TOKEN_NAME.lower() + '": "' + "t" * 20 + '"', None),
    ],
)
def test_rule_qodana_token(text: str, expected: str | None) -> None:
    assert matched(TOKEN_RULE, text) == expected


def test_rule_token_value_may_not_start_with_a_second_quote() -> None:
    assert matched(TOKEN_RULE, TOKEN_NAME + "=" + '""' + "t" * 20) is None


def test_scan_line_reports_links_without_a_scheme_and_each_one_of_a_pair() -> None:
    bare = "claude" + ".ai/chat/c1"
    assert git_guard.scan_line("go to " + bare, "w") == [Finding("w", LINK_RULE, "claude.ai/ch" + ELL)]
    both = f"{bare} www.qodana" + ".cloud/projects/p/reports/r"
    assert git_guard.scan_line(both, "w") == [
        Finding("w", LINK_RULE, "claude.ai/ch" + ELL),
        Finding("w", QODANA_RULE, "www.qodana.c" + ELL),
    ]


def test_scan_line_a_scheme_and_a_scheme_less_link_are_one_match_not_two() -> None:
    assert git_guard.scan_line(CLAUDE_LINK, "w") == [link_finding("w")]
    assert git_guard.scan_line(QODANA_LINK, "w") == [Finding("w", QODANA_RULE, QODANA_DETAIL)]


def test_rule_claude_link_with_user_info_after_the_scheme() -> None:
    assert matched(LINK_RULE, "https://user@claude" + ".ai/chat/c1") is not None


# ---- scan_line / scan_text / allow marker ---------------------------------------------------------------------------


def test_scan_line_reports_every_rule_with_masked_detail() -> None:
    assert git_guard.scan_line(CLAUDE_LINK, "w") == [link_finding("w")]
    assert git_guard.scan_line(QODANA_LINK, "w") == [Finding("w", QODANA_RULE, QODANA_DETAIL)]
    assert git_guard.scan_line(KEY, "w") == [key_finding("w")]
    assert git_guard.scan_line(TOKEN, "w") == [Finding("w", TOKEN_RULE, TOKEN_DETAIL)]


def test_scan_line_clean_and_empty() -> None:
    assert git_guard.scan_line("", "w") == []
    assert git_guard.scan_line("nothing to see https://example.com/artifact/x", "w") == []


def test_scan_line_orders_by_rule_then_position() -> None:
    line = f"{KEY} {CLAUDE_LINK} {QODANA_LINK} {CLAUDE_LINK_2}"
    assert git_guard.scan_line(line, "w") == [
        link_finding("w"),
        link_finding("w"),
        Finding("w", QODANA_RULE, QODANA_DETAIL),
        key_finding("w"),
    ]


def test_scan_line_masks_each_match_separately() -> None:
    first = "https://claude" + ".ai/chat/first"
    second = "https://claude" + ".ai/chat/second"
    assert git_guard.scan_line(f"{first} {second}", "w") == [link_finding("w"), link_finding("w")]


@pytest.mark.parametrize(
    "line",
    [
        f"{CLAUDE_LINK} git-guard: allow",
        f"git-guard: allow {CLAUDE_LINK}",
        f"# {KEY}  # git-guard: allow (documented example)",
        f"{TOKEN}git-guard: allow",
        f"{QODANA_LINK} {CLAUDE_LINK} git-guard: allow",
    ],
)
def test_allow_marker_skips_the_line(line: str) -> None:
    assert git_guard.scan_line(line, "w") == []


@pytest.mark.parametrize(
    "marker",
    ["git-guard:allow", "git-guard: allo", "Git-Guard: Allow", "git_guard: allow", "git-guard allow", "guard: allow"],
)
def test_near_miss_allow_marker_does_not_skip(marker: str) -> None:
    assert git_guard.scan_line(f"{CLAUDE_LINK} {marker}", "w") == [link_finding("w")]


def test_scan_text_numbers_lines_from_one() -> None:
    text = f"clean\n{CLAUDE_LINK}\nclean\n{KEY}\n"
    assert git_guard.scan_text(text, "msg") == [link_finding("msg:2"), key_finding("msg:4")]


def test_scan_text_first_line_and_no_trailing_newline() -> None:
    assert git_guard.scan_text(CLAUDE_LINK, "msg") == [link_finding("msg:1")]


def test_scan_text_empty_and_allow() -> None:
    assert git_guard.scan_text("", "msg") == []
    assert git_guard.scan_text(f"{CLAUDE_LINK} git-guard: allow\n{KEY}", "msg") == [key_finding("msg:2")]


def test_scan_text_keeps_comment_lines() -> None:
    assert git_guard.scan_text(f"# {CLAUDE_LINK}", "msg") == [link_finding("msg:1")]


# ---- scan_message ---------------------------------------------------------------------------------------------------


def scissors(char: str = "#") -> str:
    return f"{char} {'-' * 24} >8 {'-' * 24}"


DIFF_LINE = "diff --git a/f.txt b/f.txt"


def with_diff(*lines: str) -> str:
    """LINES, then the way `commit -v` goes on after its scissors line: a diff header and what it adds."""
    return "\n".join([*lines, DIFF_LINE, "+" + KEY, ""])


def test_scan_message_scans_comment_lines_too_and_counts_every_line() -> None:
    text = f"subject\n# {CLAUDE_LINK}\n{KEY}\n"
    assert git_guard.scan_message(text, "commit message") == [
        link_finding("commit message:2"),
        key_finding("commit message:3"),
    ]


def test_scan_message_comment_char_makes_no_difference() -> None:
    text = f"; {CLAUDE_LINK}\n@ {KEY}\n// {TOKEN}\n"
    assert git_guard.scan_message(text, "m") == [
        link_finding("m:1"),
        key_finding("m:2"),
        Finding("m:3", TOKEN_RULE, TOKEN_DETAIL),
    ]


@pytest.mark.parametrize("char", ["#", ";", "@", "!", "%", "|", "x", "-", "ä", "//", "##", ">>>", "--", "<!--"])
def test_scan_message_cuts_at_the_scissors_line_of_any_comment_string(char: str) -> None:
    text = f"{CLAUDE_LINK}\n{scissors(char)}\n{DIFF_LINE}\n{KEY}\n{TOKEN}\n"
    assert git_guard.scan_message(text, "m") == [link_finding("m:1")]


@pytest.mark.parametrize(
    "diff", ["diff --git a b", "diff --cc f.txt", "diff --combined f.txt", "diff --git ", "diff --cc "]
)
def test_scan_message_cuts_where_any_kind_of_diff_header_follows(diff: str) -> None:
    assert git_guard.scan_message(f"{CLAUDE_LINK}\n{scissors()}\n{diff}\n{KEY}\n", "m") == [link_finding("m:1")]
    assert git_guard.scan_message(f"{CLAUDE_LINK}\n{scissors()}\nfiller\n{diff}", "m") == [link_finding("m:1")]


@pytest.mark.parametrize(
    "diff",
    [
        "diff --git",
        "diff --cc",
        " diff --git a b",
        "\tdiff --git a b",
        "x diff --git a b",
        "Diff --git a b",
        "diff --Git a b",
        "diff --gitx a b",
        "diff --ccx f",
        "diff --combinedx f",
        "diff  --git a b",
        "diff -git a b",
        "diff --foo a b",
        "diff --- a b",
        "diff",
        "xdiff --git a b",
        "--- a/f.txt",
        "+++ b/f.txt",
        "@@ -1 +1 @@",
        "index 1111111..2222222 100644",
    ],
)
def test_scan_message_a_scissors_line_needs_a_diff_header_after_it(diff: str) -> None:
    text = f"s\n{scissors()}\n{diff}\n{KEY}\n"
    assert git_guard.scan_message(text, "m") == [key_finding("m:4")]


def test_scan_message_a_diff_header_before_the_scissors_line_does_not_count() -> None:
    assert git_guard.scan_message(f"{DIFF_LINE}\n{scissors()}\n{KEY}\n", "m") == [key_finding("m:3")]


def test_scan_message_a_scissors_line_alone_cuts_nothing() -> None:
    assert git_guard.scan_message(f"s\n{scissors()}\n{KEY}\n{TOKEN}\n", "m") == [
        key_finding("m:3"),
        Finding("m:4", TOKEN_RULE, TOKEN_DETAIL),
    ]
    assert git_guard.scan_message(f"{KEY}\n{scissors()}", "m") == [key_finding("m:1")]


def test_scan_message_cuts_at_the_last_scissors_line_that_a_diff_follows() -> None:
    text = f"a\n{scissors(';')}\n{DIFF_LINE}\n{KEY}\n{scissors()}\n{DIFF_LINE}\n{TOKEN}\n"
    assert git_guard.scan_message(text, "m") == [key_finding("m:4")]
    # what lies between two scissors lines counts as message while a diff follows the second
    text = f"a\n{scissors()}\n{CLAUDE_LINK}\n{scissors(';')}\n{DIFF_LINE}\n{KEY}\n"
    assert git_guard.scan_message(text, "m") == [link_finding("m:3")]


def test_scan_message_two_scissors_lines_and_a_diff_only_after_the_second() -> None:
    assert git_guard.scan_message(with_diff("a", scissors(), "b", scissors(";")), "m") == []
    text = with_diff("a", scissors(), CLAUDE_LINK, scissors(";"))
    assert git_guard.scan_message(text, "m") == [link_finding("m:3")]
    text = with_diff("a", scissors(), CLAUDE_LINK, "diff-less", scissors(";"), TOKEN)
    assert git_guard.scan_message(text, "m") == [link_finding("m:3")]


def test_scan_message_a_scissors_line_without_a_diff_after_it_is_not_a_cut() -> None:
    text = f"a\n{scissors()}\n{CLAUDE_LINK}\n{scissors(';')}\n{TOKEN}\n"
    assert git_guard.scan_message(text, "m") == [link_finding("m:3"), Finding("m:5", TOKEN_RULE, TOKEN_DETAIL)]
    # only the first scissors line has a diff after it
    text = f"a\n{scissors()}\n{DIFF_LINE}\n{scissors(';')}\n{TOKEN}\n"
    assert git_guard.scan_message(text, "m") == []


def test_scan_message_scissors_typed_into_a_message_cuts_nothing() -> None:
    text = f"subject\n\n{scissors()}\n\n{CLAUDE_LINK}\n"
    assert git_guard.scan_message(text, "m") == [link_finding("m:5")]


def test_scan_message_scissors_first_line_leaves_nothing() -> None:
    assert git_guard.scan_message(with_diff(scissors()), "m") == []
    assert git_guard.scan_message(with_diff(scissors(";")), "m") == []
    assert git_guard.scan_message(f"{scissors()}\n{DIFF_LINE}", "m") == []


def test_scan_message_scissors_cut_without_a_final_newline() -> None:
    assert git_guard.scan_message(f"{KEY}\n{scissors()}\n{DIFF_LINE}", "m") == [key_finding("m:1")]


def test_scan_message_scissors_cut_keeps_the_line_before_it_whole() -> None:
    assert git_guard.scan_message(with_diff(KEY, scissors(), KEY), "m") == [key_finding("m:1")]
    assert git_guard.scan_message(f"{KEY}\n{scissors()}\n{DIFF_LINE}\n{KEY}", "m") == [key_finding("m:1")]


def test_scan_message_the_scissors_line_itself_is_not_scanned() -> None:
    assert git_guard.scan_message(with_diff("a", f"{CLAUDE_LINK} {'-' * 24} >8 {'-' * 24}"), "m") == []


@pytest.mark.parametrize(
    "line",
    [
        "# " + "-" * 23 + " >8 " + "-" * 24,
        "# " + "-" * 24 + " >8 " + "-" * 23,
        "# " + "-" * 25 + " >8 " + "-" * 24,
        "# " + "-" * 24 + " >8 " + "-" * 25,
        "#" + "-" * 24 + " >8 " + "-" * 24,
        "# " + "-" * 24 + ">8 " + "-" * 24,
        "# " + "-" * 24 + " >8" + "-" * 24,
        "# " + "-" * 24 + " > 8 " + "-" * 24,
        "# " + "-" * 24 + " 8< " + "-" * 24,
        "  " + "-" * 24 + " >8 " + "-" * 24,
        " # " + "-" * 24 + " >8 " + "-" * 24,
        "# " + "-" * 24 + " >8 " + "-" * 24 + " ",
        "# " + "-" * 24 + " >8 " + "-" * 24 + "x",
        "x # " + "-" * 24 + " >8 " + "-" * 24,
        "\t" + "-" * 24 + " >8 " + "-" * 24,
        "# " + "-" * 24 + "  >8 " + "-" * 24,
        "# # " + "-" * 24 + " >8 " + "-" * 24,
        "# " + "-" * 24 + " >8 " + "-" * 24 + " #",
        "-" * 24 + " >8 " + "-" * 24,
        "# " + "-" * 24 + " >8 ",
        "# " + "-" * 24 + " >8 " + "-" * 24 + "\r",
    ],
)
def test_scan_message_near_miss_scissors_lines_cut_nothing(line: str) -> None:
    assert git_guard.scan_message(f"{line}\n{DIFF_LINE}\n{KEY}\n", "m") == [key_finding("m:3")]


def test_scan_message_scissors_must_start_the_line() -> None:
    assert git_guard.scan_message(f"x {scissors()}\n{DIFF_LINE}\n{KEY}", "m") == [key_finding("m:3")]


def test_scan_message_allow_marker_and_empty() -> None:
    assert git_guard.scan_message("", "m") == []
    assert git_guard.scan_message(f"{CLAUDE_LINK} git-guard: allow\n", "m") == []


def test_scan_message_appends_findings_in_order() -> None:
    assert git_guard.scan_message(f"{KEY}\n{CLAUDE_LINK}", "m") == [key_finding("m:1"), link_finding("m:2")]


@pytest.mark.parametrize("sep", [*LINE_BREAKS, "\r\r"])
def test_scan_message_only_a_newline_ends_a_line(sep: str) -> None:
    text = f"a{sep}{CLAUDE_LINK}\n{KEY}\n"
    assert git_guard.scan_message(text, "m") == [link_finding("m:1"), key_finding("m:2")]


@pytest.mark.parametrize("sep", LINE_BREAKS)
def test_scan_message_a_line_break_character_cannot_fake_a_scissors_line_or_a_diff_header(sep: str) -> None:
    assert git_guard.scan_message(f"a{sep}{scissors()}\n{DIFF_LINE}\n{KEY}", "m") == [key_finding("m:3")]
    assert git_guard.scan_message(f"{scissors()}\na{sep}{DIFF_LINE}\n{KEY}", "m") == [key_finding("m:3")]


def test_scan_message_utf16_text() -> None:
    assert git_guard.scan_message(utf16(f"a\n{CLAUDE_LINK}\n"), "m") == [link_finding("m:2")]


# ---- env_files ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".env.production",
        ".env.",
        ".env.example.bak",
        ".env.examples",
        ".env.a.b",
        ".envrc",
        "sub/.envrc",
        "a.env/.envrc",
        ".ENV",
        ".Env.Local",
        ".ENVRC",
        "sub/.ENVRC",
        "X.ENV",
        "dir/prod.ENV",
        "Prod.Env",
        ".env-prod",
        ".env_prod",
        ".env-x",
        ".env_",
        ".env-",
        ".env.sample.bak",
        ".env.examplex",
        ".env-example",  # a template needs a dot before its word
        ".env_sample",
        ".env-template",
        ".env.local-sample",
        "x.env",
        "prod.env",
        "sub/.env",
        "a/b/.env.local",
        "config/prod.env",
        "a.b.env",
        "sub dir/.env",
    ],
)
def test_env_files_flags_env_files(path: str) -> None:
    assert git_guard.env_files([path]) == [Finding(path, "env file", ENV_DETAIL)]


@pytest.mark.parametrize(
    "path",
    [
        ".env.example",
        ".env.sample",
        ".env.template",
        "sub/.env.example",
        "a/b/.env.sample",
        "x/.env.template",
        ".env.local.example",
        ".env.production.sample",
        ".ENV.EXAMPLE",
        ".env.Sample",
        ".env.TEMPLATE",
        ".Env.Local.Example",
        ".env-prod.example",
        ".env_prod.sample",
        "prod.env.example",
        "X.ENV.TEMPLATE",
        "README.example",
        "x.example",
        "config.sample",
        "a.template",
        ".envrc.example",
        ".envx",
        ".env2",
        "a/.envrc/b",
        ".env.d/app.conf",  # a directory named like an env file: only the last component counts
        ".env-dir/x",
        "sub/.env_x/y",
        "env",
        "x/env",
        "foo.environment",
        "my.env.bak",
        "dir.env/file.txt",
        ".environment",
        "notenv",
        "sub.env.example",
        "",
        ".env/file",
        "x/y.txt",
        "env.txt",
    ],
)
def test_env_files_allows_the_rest(path: str) -> None:
    assert git_guard.env_files([path]) == []


def test_env_files_prefix_order_and_iterables() -> None:
    paths = iter(["a.txt", ".env", "ok/.env.example", "sub/prod.env"])
    assert git_guard.env_files(paths, "abc123 ") == [
        Finding("abc123 .env", "env file", ENV_DETAIL),
        Finding("abc123 sub/prod.env", "env file", ENV_DETAIL),
    ]
    assert git_guard.env_files([]) == []
    assert git_guard.env_files([".env"], "") == [Finding(".env", "env file", ENV_DETAIL)]


# ---- scan_patch -----------------------------------------------------------------------------------------------------


def patch(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def file_header(name: str) -> list[str]:
    return [f"diff --git a/{name} b/{name}", "index 1111111..2222222 100644", f"--- a/{name}", f"+++ b/{name}"]


def test_scan_patch_path_and_line_numbers_from_hunks() -> None:
    text = patch(
        *file_header("f.txt"),
        "@@ -3,2 +10,3 @@ def f():",
        "-" + CLAUDE_LINK_2,
        "+" + CLAUDE_LINK,
        "+ok",
        "+" + KEY,
        "@@ -20 +30 @@",
        "+" + CLAUDE_LINK,
    )
    assert git_guard.scan_patch(text) == [link_finding("f.txt:10"), key_finding("f.txt:12"), link_finding("f.txt:30")]


def test_scan_patch_prefix() -> None:
    text = patch(*file_header("f.txt"), "@@ -0,0 +1 @@", "+" + CLAUDE_LINK)
    assert git_guard.scan_patch(text, "ab12 ") == [link_finding("ab12 f.txt:1")]
    assert git_guard.scan_patch(text, "") == [link_finding("f.txt:1")]


def test_scan_patch_removed_lines_do_not_advance_the_line_number() -> None:
    text = patch(*file_header("f.txt"), "@@ -5,3 +5,2 @@", "+x", "-y", "-z", "+" + CLAUDE_LINK)
    assert git_guard.scan_patch(text) == [link_finding("f.txt:6")]


def test_scan_patch_ignores_removed_context_and_marker_lines() -> None:
    text = patch(
        *file_header("f.txt"),
        "@@ -1,3 +1,3 @@",
        " " + CLAUDE_LINK,
        "-" + KEY,
        "\\ No newline at end of file",
        "+x",
    )
    assert git_guard.scan_patch(text) == []


def test_scan_patch_second_file_resets_path() -> None:
    text = patch(
        *file_header("a.txt"),
        "@@ -0,0 +1 @@",
        "+" + CLAUDE_LINK,
        *file_header("dir/b.txt"),
        "@@ -0,0 +4 @@",
        "+" + KEY,
    )
    assert git_guard.scan_patch(text) == [link_finding("a.txt:1"), key_finding("dir/b.txt:4")]


def test_scan_patch_plus_plus_plus_line_inside_a_hunk_is_added_content() -> None:
    text = patch(
        *file_header("f.txt"),
        "@@ -0,0 +1,3 @@",
        "+++ " + CLAUDE_LINK,
        "+++ b/evil.txt",
        "+" + KEY,
    )
    assert git_guard.scan_patch(text) == [link_finding("f.txt:1"), key_finding("f.txt:3")]


def test_scan_patch_plus_lines_in_the_header_are_ignored() -> None:
    text = patch("diff --git a/f.txt b/f.txt", "+" + CLAUDE_LINK, "--- a/f.txt", "+++ b/f.txt", "+" + KEY)
    assert git_guard.scan_patch(text) == []


def test_scan_patch_header_plus_plus_plus_with_a_secret_is_not_scanned() -> None:
    text = patch("diff --git a/f.txt b/f.txt", "+++ b/" + CLAUDE_LINK, "@@ -0,0 +1 @@", "+x")
    assert git_guard.scan_patch(text) == []


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("+++ b/f.txt", "f.txt"),
        ("+++ b/dir/sub/f.txt", "dir/sub/f.txt"),
        ("+++ b/b/f.txt", "b/f.txt"),
        ("+++ f.txt", "f.txt"),
        ("+++ a/f.txt", "a/f.txt"),
        ('+++ "b/caf\\303\\251.txt"', "caf\\303\\251.txt"),
        ('+++ "b/f.txt"', "f.txt"),
        ('+++ "f.txt"', "f.txt"),
        ("+++ /dev/null", "/dev/null"),
        ("+++ bb/f.txt", "bb/f.txt"),
        ("+++ b/fileX", "fileX"),
        ("+++ Xfile", "Xfile"),
        ('+++ "b/XfileX"', "XfileX"),
    ],
)
def test_scan_patch_paths(header: str, expected: str) -> None:
    text = patch("diff --git a/x b/x", header, "@@ -0,0 +1 @@", "+" + CLAUDE_LINK)
    assert git_guard.scan_patch(text) == [link_finding(f"{expected}:1")]


def test_scan_patch_nothing_before_the_first_hunk_header_is_scanned() -> None:
    assert git_guard.scan_patch("+" + CLAUDE_LINK + "\n") == []
    assert git_guard.scan_patch(patch("+" + CLAUDE_LINK, " " + KEY, "x" + KEY)) == []


def test_scan_patch_a_hunk_without_a_file_header_uses_the_unknown_path() -> None:
    assert git_guard.scan_patch(patch("@@ -1 +7 @@", "+" + CLAUDE_LINK, "+" + KEY)) == [
        link_finding("?:7"),
        key_finding("?:8"),
    ]


@pytest.mark.parametrize("hunk", ["@@ nonsense @@", "@@ -1 +x @@", "@@ -1,2 @@", "@@  -1 +2 @@", "@@ -1 -2 @@"])
def test_scan_patch_malformed_hunk_header_leaves_the_file_header_state(hunk: str) -> None:
    text = patch(*file_header("f.txt"), hunk, "+" + CLAUDE_LINK, "+" + KEY, "@@ -0,0 +5 @@", "+" + TOKEN)
    assert git_guard.scan_patch(text) == [Finding("f.txt:5", TOKEN_RULE, TOKEN_DETAIL)]


def test_scan_patch_the_header_of_the_next_file_is_not_scanned() -> None:
    text = patch(
        *file_header("a.txt"),
        "@@ -0,0 +1 @@",
        "+x",
        "diff --git a/b.txt b/b.txt",
        "new file mode 100644",
        "+" + CLAUDE_LINK,
        "--- /dev/null",
        "+++ b/b.txt",
        "+" + KEY,
        "@@ -0,0 +1 @@",
        "+" + TOKEN,
    )
    assert git_guard.scan_patch(text) == [Finding("b.txt:1", TOKEN_RULE, TOKEN_DETAIL)]


def test_scan_patch_the_path_of_a_file_without_hunks_is_not_carried_over() -> None:
    text = patch(
        *file_header("a.txt"),
        "@@ -0,0 +1 @@",
        "+x",
        "diff --git a/mode.sh b/mode.sh",
        "old mode 100644",
        "new mode 100755",
        *file_header("c.txt"),
        "@@ -0,0 +3 @@",
        "+" + CLAUDE_LINK,
    )
    assert git_guard.scan_patch(text) == [link_finding("c.txt:3")]


def test_scan_patch_hunk_header_with_section_text_and_multi_digit_lines() -> None:
    text = patch(*file_header("f.txt"), "@@ -123,4 +4567,9 @@ class A: # +9", "+" + CLAUDE_LINK)
    assert git_guard.scan_patch(text) == [link_finding("f.txt:4567")]


def test_scan_patch_allow_marker_and_empty() -> None:
    assert git_guard.scan_patch("") == []
    text = patch(*file_header("f.txt"), "@@ -0,0 +1,2 @@", "+" + CLAUDE_LINK + " git-guard: allow", "+" + KEY)
    assert git_guard.scan_patch(text) == [key_finding("f.txt:2")]


def test_scan_patch_a_hunk_header_starts_the_line() -> None:
    text = patch(*file_header("f.txt"), " @@ -0,0 +4 @@", "x @@ -0,0 +4 @@", "+" + CLAUDE_LINK)
    assert git_guard.scan_patch(text) == []
    text = patch(*file_header("f.txt"), "@@ -0,0 +1,2 @@", " @@ -0,0 +9 @@", "+" + CLAUDE_LINK)
    assert git_guard.scan_patch(text) == [link_finding("f.txt:2")]


def test_scan_patch_scans_the_added_line_without_its_plus() -> None:
    text = patch(*file_header("f.txt"), "@@ -0,0 +1 @@", "++" + CLAUDE_LINK)
    assert git_guard.scan_patch(text) == [link_finding("f.txt:1")]
    # a pattern that needs the line start would miss if the "+" were kept
    assert git_guard.scan_patch(patch(*file_header("f.txt"), "@@ -0,0 +1 @@", "+" + "x" * 3)) == []


def combined_header(name: str, parents: int) -> list[str]:
    """The header of a file in a merge's combined diff."""
    ids = ",".join(["1111111"] * parents)
    return [f"diff --cc {name}", f"index {ids}..2222222", f"--- a/{name}", f"+++ b/{name}"]


def test_scan_patch_combined_diff_flags_lines_every_parent_lacks() -> None:
    text = patch(
        *combined_header("f.txt", 2),
        "@@@ -1,3 -1,3 +1,5 @@@ def f():",
        "++" + CLAUDE_LINK,
        "  context",
        "++" + KEY,
        "++clean",
        "++" + TOKEN,
    )
    assert git_guard.scan_patch(text, "ab12 ") == [
        link_finding("ab12 f.txt:1"),
        key_finding("ab12 f.txt:3"),
        Finding("ab12 f.txt:5", TOKEN_RULE, TOKEN_DETAIL),
    ]


def test_scan_patch_combined_diff_does_not_flag_lines_a_parent_has() -> None:
    text = patch(
        *combined_header("f.txt", 2),
        "@@@ -1 -1 +1,4 @@@",
        " +" + CLAUDE_LINK,
        "+ " + CLAUDE_LINK,
        "  " + CLAUDE_LINK,
        "++" + KEY,
    )
    assert git_guard.scan_patch(text) == [key_finding("f.txt:4")]


def test_scan_patch_combined_diff_counts_the_lines_of_the_new_version() -> None:
    text = patch(
        *combined_header("f.txt", 2),
        "@@@ -1,6 -1,5 +10,7 @@@",
        "  context",  # 10
        " +from the second parent",  # 11
        "+ from the first parent",  # 12
        "- removed from the first parent",
        " -removed from the second parent",
        "--removed from both",
        "+-removed from the second, added to the first",
        "-+removed from the first, added to the second",
        "++" + CLAUDE_LINK,  # 13
        "  " + KEY,  # 14
        "++" + KEY,  # 15
    )
    assert git_guard.scan_patch(text) == [link_finding("f.txt:13"), key_finding("f.txt:15")]


def test_scan_patch_combined_diff_hunks_and_files_follow_each_other() -> None:
    text = patch(
        *combined_header("a.txt", 2),
        "@@@ -1 -1 +1 @@@",
        "++" + CLAUDE_LINK,
        "@@@ -9,2 -9,2 +20,2 @@@",
        " +x",
        "++" + KEY,
        *combined_header("dir/b.txt", 2),
        "@@@ -4 -4 +40 @@@",
        "++" + TOKEN,
    )
    assert git_guard.scan_patch(text) == [
        link_finding("a.txt:1"),
        key_finding("a.txt:21"),
        Finding("dir/b.txt:40", TOKEN_RULE, TOKEN_DETAIL),
    ]


def test_scan_patch_octopus_merge_needs_a_plus_in_all_three_columns() -> None:
    text = patch(
        *combined_header("f.txt", 3),
        "@@@@ -1,2 -1,2 -1,2 +1,6 @@@@",
        "+++" + CLAUDE_LINK,
        "++ " + CLAUDE_LINK,
        "+ +" + CLAUDE_LINK,
        " ++" + CLAUDE_LINK,
        "+  " + CLAUDE_LINK,
        "  +" + CLAUDE_LINK,
        "+++" + KEY,
        "-++" + KEY,
        "+-+" + KEY,
        "++-" + KEY,
        "+++" + TOKEN,
    )
    assert git_guard.scan_patch(text) == [
        link_finding("f.txt:1"),
        key_finding("f.txt:7"),
        Finding("f.txt:8", TOKEN_RULE, TOKEN_DETAIL),
    ]


def test_scan_patch_the_number_of_columns_follows_each_hunk_header() -> None:
    text = patch(
        *file_header("f.txt"),
        "@@ -1 +1 @@",
        "+" + CLAUDE_LINK,
        "@@@ -1 -1 +5 @@@",
        "+ " + KEY,
        "++" + TOKEN,
        "@@ -1 +9 @@",
        "+ " + CLAUDE_LINK,
    )
    assert git_guard.scan_patch(text) == [
        link_finding("f.txt:1"),
        Finding("f.txt:6", TOKEN_RULE, TOKEN_DETAIL),
        link_finding("f.txt:9"),
    ]


def test_scan_patch_a_removed_line_in_a_plain_diff_is_not_a_line_of_the_new_version() -> None:
    text = patch(*file_header("f.txt"), "@@ -1,2 +1,2 @@", "-" + KEY, "-" + KEY, "+" + CLAUDE_LINK)
    assert git_guard.scan_patch(text) == [link_finding("f.txt:1")]


# ---- files ----------------------------------------------------------------------------------------------------------


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory that is not a repository: files() reads the given files and runs no git command."""
    root = tmp_path / "tree"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


def put(root: Path, name: str, content: str | bytes) -> str:
    """Write ROOT/NAME and return NAME."""
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    return name


def env_finding(path: str) -> Finding:
    return Finding(path, "env file", ENV_DETAIL)


def test_files_nothing_and_clean_files(tree: Path) -> None:
    assert git_guard.files([]) == []
    assert (
        git_guard.files([put(tree, "a.txt", "fine\n"), put(tree, "empty.txt", ""), put(tree, "nl.txt", "\n\n\n")]) == []
    )


def test_files_reads_the_whole_content_with_line_numbers(tree: Path) -> None:
    name = put(tree, "notes.txt", f"one\n{CLAUDE_LINK}\nthree\n{KEY}\n\n{TOKEN}")
    assert git_guard.files([name]) == [
        link_finding("notes.txt:2"),
        key_finding("notes.txt:4"),
        Finding("notes.txt:6", TOKEN_RULE, TOKEN_DETAIL),
    ]


def test_files_finds_the_first_and_the_last_line_without_a_newline(tree: Path) -> None:
    assert git_guard.files([put(tree, "one.txt", CLAUDE_LINK)]) == [link_finding("one.txt:1")]
    assert git_guard.files([put(tree, "two.txt", f"x\n{KEY}")]) == [key_finding("two.txt:2")]


def test_files_every_rule_in_one_file(tree: Path) -> None:
    name = put(tree, "all.txt", f"{KEY} {CLAUDE_LINK} {QODANA_LINK} {TOKEN}\n")
    assert git_guard.files([name]) == [
        link_finding("all.txt:1"),
        Finding("all.txt:1", QODANA_RULE, QODANA_DETAIL),
        key_finding("all.txt:1"),
        Finding("all.txt:1", TOKEN_RULE, TOKEN_DETAIL),
    ]


def test_files_a_file_that_is_not_a_change_is_read_whole(tree: Path) -> None:
    """The hook reads what is on disk, not a diff: a link on line 1 of an old file is found."""
    name = put(tree, "old.txt", f"{CLAUDE_LINK}\n" + "filler\n" * 50 + f"{KEY}\n")
    assert git_guard.files([name]) == [link_finding("old.txt:1"), key_finding("old.txt:52")]


def test_files_env_file_names_and_their_contents(tree: Path) -> None:
    name = put(tree, ".env", f"A=1\n{KEY}\n")
    assert git_guard.files([name]) == [env_finding(".env"), key_finding(".env:2")]


@pytest.mark.parametrize(
    "name",
    [".env.local", "sub/.envrc", ".ENV", "prod.env", ".env-prod", ".env_prod", "dir/.env.production", "a b/.env"],
)
def test_files_flags_every_kind_of_env_file_name(tree: Path, name: str) -> None:
    put(tree, name, "A=1\n")
    assert git_guard.files([name]) == [env_finding(name)]


@pytest.mark.parametrize(
    "name", [".env.example", ".env.local.example", ".ENV.SAMPLE", "x/.env.template", "README.example", "plain.txt"]
)
def test_files_does_not_flag_templates_and_other_names(tree: Path, name: str) -> None:
    put(tree, name, "A=1\n")
    assert git_guard.files([name]) == []


def test_files_scans_the_contents_of_a_template_too(tree: Path) -> None:
    name = put(tree, ".env.example", f"A=\n{KEY}\n")
    assert git_guard.files([name]) == [key_finding(".env.example:2")]


def test_files_reports_the_path_as_given(tree: Path) -> None:
    put(tree, "sub/dir/f.txt", f"{CLAUDE_LINK}\n")
    absolute = str(tree / "sub" / "dir" / "f.txt")
    assert git_guard.files(["sub/dir/f.txt", "./sub/dir/f.txt", absolute]) == [
        link_finding("sub/dir/f.txt:1"),
        link_finding("./sub/dir/f.txt:1"),
        link_finding(f"{absolute}:1"),
    ]
    assert git_guard.files([str(tree / ".." / "tree" / "sub" / "dir" / "f.txt")]) == [
        link_finding(f"{tree}/../tree/sub/dir/f.txt:1")
    ]


def test_files_several_paths_env_names_first_then_contents_in_order(tree: Path) -> None:
    paths = [
        put(tree, "a.txt", f"{KEY}\n"),
        put(tree, ".env", "A=1\n"),
        put(tree, "b.txt", f"x\n{CLAUDE_LINK}\n"),
        put(tree, "sub/prod.env", f"{TOKEN}\n"),
        put(tree, "clean.txt", "fine\n"),
    ]
    assert git_guard.files(paths) == [
        env_finding(".env"),
        env_finding("sub/prod.env"),
        key_finding("a.txt:1"),
        link_finding("b.txt:2"),
        Finding("sub/prod.env:1", TOKEN_RULE, TOKEN_DETAIL),
    ]
    assert git_guard.files(paths[::-1]) == [
        env_finding("sub/prod.env"),
        env_finding(".env"),
        Finding("sub/prod.env:1", TOKEN_RULE, TOKEN_DETAIL),
        link_finding("b.txt:2"),
        key_finding("a.txt:1"),
    ]


def test_files_a_path_given_twice_is_reported_twice(tree: Path) -> None:
    name = put(tree, "a.txt", f"{KEY}\n")
    assert git_guard.files([name, name]) == [key_finding("a.txt:1"), key_finding("a.txt:1")]


def test_files_the_allow_marker_skips_a_line(tree: Path) -> None:
    name = put(tree, "m.txt", f"{CLAUDE_LINK} git-guard: allow\n{KEY}\n# git-guard: allow {TOKEN}\n{CLAUDE_LINK_2}\n")
    assert git_guard.files([name]) == [key_finding("m.txt:2"), link_finding("m.txt:4")]


def test_files_the_allow_marker_does_not_exempt_the_env_file_name(tree: Path) -> None:
    name = put(tree, ".env", f"{CLAUDE_LINK} git-guard: allow\n")
    assert git_guard.files([name]) == [env_finding(".env")]


def test_files_crlf_line_endings(tree: Path) -> None:
    name = put(tree, "win.txt", f"a\r\nb\r\n{CLAUDE_LINK}\r\n{KEY}\r\n")
    assert git_guard.files([name]) == [link_finding("win.txt:3"), key_finding("win.txt:4")]


@pytest.mark.parametrize("sep", LINE_BREAKS)
def test_files_only_a_newline_ends_a_line(tree: Path, sep: str) -> None:
    name = put(tree, "f.txt", f"a{sep}b\n{CLAUDE_LINK}\n")
    assert git_guard.files([name]) == [link_finding("f.txt:2")]


def test_files_binary_content_and_invalid_utf8_are_decoded_with_replacement(tree: Path) -> None:
    raw = b"\xff\xfe\x00 caf\xe9\n\x00\x01\x02\n" + CLAUDE_LINK.encode() + b"\n\xff\xff " + KEY.encode() + b"\x00\n"
    name = put(tree, "blob.bin", raw)
    assert git_guard.files([name]) == [link_finding("blob.bin:3"), key_finding("blob.bin:4")]


def test_files_invalid_utf8_alone_does_not_raise(tree: Path) -> None:
    assert git_guard.files([put(tree, "x.dat", b"\xff\xfe\x80\n\xc3\x28\n")]) == []


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_files_utf16_content(tree: Path, encoding: str, newline: str) -> None:
    text = newline.join(["first", CLAUDE_LINK, f"{KEY} git-guard: allow", TOKEN, ""])
    name = put(tree, "u16.txt", text.encode(encoding))
    assert git_guard.files([name]) == [link_finding("u16.txt:2"), Finding("u16.txt:4", TOKEN_RULE, TOKEN_DETAIL)]


def test_files_utf16_without_a_final_newline_and_with_the_env_name(tree: Path) -> None:
    name = put(tree, ".env.prod", f"A=1\n{KEY}".encode("utf-16"))
    assert git_guard.files([name]) == [env_finding(".env.prod"), key_finding(".env.prod:2")]


def test_files_a_symlink_is_its_target_text_not_the_file_it_points_to(tree: Path) -> None:
    put(tree, "real.txt", f"{CLAUDE_LINK}\n{KEY}\n")
    (tree / "ln").symlink_to("real.txt")
    assert git_guard.files(["ln"]) == []
    assert git_guard.files(["real.txt"]) == [link_finding("real.txt:1"), key_finding("real.txt:2")]


def bare_finding(where: str) -> Finding:
    return Finding(where, LINK_RULE, BARE_DETAIL)


def test_files_a_symlink_whose_target_holds_a_link(tree: Path) -> None:
    (tree / "ln").symlink_to(BARE_LINK)  # dangling: git stores the target text, and so is it read
    (tree / "key").symlink_to(KEY)
    assert git_guard.files(["ln", "key"]) == [bare_finding("ln:1"), key_finding("key:1")]


def test_files_a_symlink_target_is_scanned_as_git_stores_it(tree: Path) -> None:
    (tree / "ln").symlink_to(CLAUDE_LINK)
    assert os.readlink(tree / "ln") == CLAUDE_LINK  # noqa: PTH115 (pathlib is what changes the text)
    assert git_guard.files(["ln"]) == [link_finding("ln:1")]


def test_files_a_symlink_target_is_a_single_line_numbered_from_one(tree: Path) -> None:
    (tree / "sub").mkdir()
    (tree / "sub" / "ln").symlink_to(f"../{BARE_LINK}")
    assert git_guard.files(["sub/ln"]) == [bare_finding("sub/ln:1")]


def test_files_a_symlink_target_with_the_allow_marker(tree: Path) -> None:
    (tree / "ln").symlink_to(f"{BARE_LINK} git-guard: allow")
    assert git_guard.files(["ln"]) == []


def test_files_a_symlink_with_an_env_name(tree: Path) -> None:
    (tree / ".env").symlink_to(KEY)
    (tree / "ok.txt").symlink_to("target")
    assert git_guard.files([".env", "ok.txt"]) == [env_finding(".env"), key_finding(".env:1")]


def test_files_a_symlink_to_a_directory_and_a_dangling_one(tree: Path) -> None:
    (tree / "dir").mkdir()
    (tree / "to-dir").symlink_to("dir")
    (tree / "dangling").symlink_to("nowhere/at/all")
    assert git_guard.files(["to-dir", "dangling"]) == []


def test_files_a_symlink_target_that_is_not_utf8(tree: Path) -> None:
    (tree / "ln").symlink_to(os.fsdecode(b"\xff\xfe/" + BARE_LINK.encode()))
    (tree / "plain").symlink_to(os.fsdecode(b"\xff\xfe"))
    assert os.fsencode(os.readlink(tree / "ln")).startswith(b"\xff\xfe/")  # noqa: PTH115
    assert git_guard.files(["ln", "plain"]) == [bare_finding("ln:1")]


def test_files_a_missing_path_raises(tree: Path) -> None:
    with pytest.raises(FileNotFoundError):
        git_guard.files(["nosuchfile.txt"])
    put(tree, "a.txt", f"{KEY}\n")
    with pytest.raises(FileNotFoundError):
        git_guard.files(["a.txt", "nosuchfile.txt"])
    with pytest.raises(FileNotFoundError):
        git_guard.files(["." + "env"])  # an env file name, and nothing to read


def test_files_a_directory_raises(tree: Path) -> None:
    (tree / "dir").mkdir()
    with pytest.raises(IsADirectoryError):
        git_guard.files(["dir"])


def test_files_runs_no_git_command(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **_kwargs: calls.append(args))
    put(tree, "a.txt", f"{KEY}\n")
    assert git_guard.files(["a.txt"]) == [key_finding("a.txt:1")]
    assert git_guard.run(["files", "a.txt"]) == [key_finding("a.txt:1")]
    assert calls == []


def test_files_in_a_repository_reads_the_working_tree_not_the_index(repo: Repo) -> None:
    repo.commit({"f.txt": "clean\n"})
    repo.stage({"f.txt": f"{KEY}\n"})
    repo.write("f.txt", "clean again\n")
    assert git_guard.files(["f.txt"]) == []
    repo.write("g.txt", f"{CLAUDE_LINK}\n")  # untracked
    assert git_guard.files(["g.txt"]) == [link_finding("g.txt:1")]
    assert git_guard.staged() == [key_finding("f.txt:1")]


def test_run_files(tree: Path) -> None:
    put(tree, "a.txt", f"x\n{KEY}\n")
    put(tree, ".env", "A=1\n")
    assert git_guard.run(["files", "a.txt"]) == [key_finding("a.txt:2")]
    assert git_guard.run(["files", "a.txt", ".env"]) == [env_finding(".env"), key_finding("a.txt:2")]
    assert git_guard.run(["files", "a.txt", "a.txt", "a.txt"]) == [key_finding("a.txt:2")] * 3


def test_run_files_without_paths_is_an_empty_list_not_none(tree: Path) -> None:
    assert git_guard.run(["files"]) == []


def test_run_files_clean_is_an_empty_list_not_none(tree: Path) -> None:
    assert git_guard.run(["files", put(tree, "ok.txt", "fine\n")]) == []


def test_run_files_a_missing_path_raises(tree: Path) -> None:
    with pytest.raises(FileNotFoundError):
        git_guard.run(["files", "nosuchfile.txt"])


def test_run_files_does_not_use_the_environment_or_a_repository(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRE_COMMIT_TO_REF", "nosuchref")
    monkeypatch.setenv("PRE_COMMIT_REMOTE_NAME", "nosuchremote")
    assert git_guard.run(["files", put(tree, "a.txt", f"{KEY}\n")]) == [key_finding("a.txt:1")]


def test_main_files_findings(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    put(tree, ".env", f"A=1\n{KEY}\n")
    put(tree, "f.txt", f"x\n{CLAUDE_LINK}\n")
    put(tree, "ok.txt", "fine\n")
    assert git_guard.main(["files", ".env", "f.txt", "ok.txt"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f".env: env file: {ENV_DETAIL}\n"
        f".env:2: {KEY_RULE}: {KEY_DETAIL}\n"
        f"f.txt:2: {LINK_RULE}: {LINK_DETAIL}\n" + SUMMARY.format(n=3) + "\n"
    )


def test_main_files_a_symlink(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tree / "ln").symlink_to(BARE_LINK)
    assert git_guard.main(["files", "ln"]) == 1
    assert capsys.readouterr().err == f"ln:1: {LINK_RULE}: {BARE_DETAIL}\n" + SUMMARY.format(n=1) + "\n"


def test_main_files_clean_and_without_paths_are_silent_exit_0(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert git_guard.main(["files", put(tree, "ok.txt", "fine\n"), put(tree, ".env.example", "A=\n")]) == 0
    assert git_guard.main(["files"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_main_files_a_missing_path_is_not_caught(tree: Path) -> None:
    with pytest.raises(FileNotFoundError):
        git_guard.main(["files", "nosuchfile.txt"])


def test_main_files_reads_sys_argv(
    tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    put(tree, "a.txt", f"{TOKEN}\n")
    monkeypatch.setattr(sys, "argv", ["git_guard.py", "files", "a.txt"])
    assert git_guard.main() == 1
    assert capsys.readouterr().err == f"a.txt:1: {TOKEN_RULE}: {TOKEN_DETAIL}\n" + SUMMARY.format(n=1) + "\n"


def test_dunder_main_files(tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    put(tree, "a.txt", f"{KEY}\n")
    put(tree, "ok.txt", "fine\n")
    assert run_as_script(monkeypatch, "files", "ok.txt") == 0
    assert run_as_script(monkeypatch, "files", "ok.txt", "a.txt") == 1
    assert capsys.readouterr().err == f"a.txt:1: {KEY_RULE}: {KEY_DETAIL}\n" + SUMMARY.format(n=1) + "\n"


# ---- staged ---------------------------------------------------------------------------------------------------------


def test_staged_nothing(repo: Repo) -> None:
    assert git_guard.staged() == []
    repo.commit({"a.txt": "1\n"})
    assert git_guard.staged() == []


def test_staged_new_file_with_line_numbers(repo: Repo) -> None:
    repo.stage({"notes.txt": f"one\n{CLAUDE_LINK}\nthree\n{KEY}\n"})
    assert git_guard.staged() == [link_finding("notes.txt:2"), key_finding("notes.txt:4")]


def test_staged_only_reports_added_lines_with_exact_numbers(repo: Repo) -> None:
    old = "\n".join(["l1", CLAUDE_LINK_2, "l3", "l4", "l5", "l6", "l7"]) + "\n"
    repo.commit({"f.txt": old})
    new = "\n".join(["l1", CLAUDE_LINK_2, "l3", "l4 changed", KEY, "l5", "l6", "l7"]) + "\n"
    repo.stage({"f.txt": new})
    repo.write("f.txt", new + CLAUDE_LINK + "\n")  # unstaged, not reported
    assert git_guard.staged() == [key_finding("f.txt:5")]


def test_staged_deleted_secret_lines_are_not_reported(repo: Repo) -> None:
    repo.commit({"f.txt": f"{CLAUDE_LINK}\nkeep\n"})
    repo.stage({"f.txt": "keep\n"})
    assert git_guard.staged() == []


def test_staged_env_files_come_before_patch_findings(repo: Repo) -> None:
    repo.stage(
        {
            ".env": f"A=1\n{KEY}\n",
            "sub/prod.env": "B=2\n",
            "sub/.env.example": "C=3\n",
            ".envrc": "export A=1\n",
            "ok.txt": "fine\n",
        },
    )
    assert git_guard.staged() == [
        Finding(".env", "env file", ENV_DETAIL),
        Finding(".envrc", "env file", ENV_DETAIL),
        Finding("sub/prod.env", "env file", ENV_DETAIL),
        key_finding(".env:2"),
    ]


def test_staged_every_kind_of_env_file_name_and_the_templates(repo: Repo) -> None:
    names = [".ENV", ".env-prod", ".env.local", ".env_prod", "a/.envrc", "x.ENV"]
    templates = [".env.local.example", ".env.Sample", "README.example", "x.ENV.TEMPLATE"]
    repo.stage(dict.fromkeys([*names, *templates], "A=1\n"))
    assert git_guard.staged() == [Finding(name, "env file", ENV_DETAIL) for name in names]


def test_staged_deleted_env_file_is_fine(repo: Repo) -> None:
    repo.commit({".env": "A=1\n", "keep.txt": "x\n"})
    repo.git("rm", "-q", ".env")
    assert git_guard.staged() == []


def test_staged_renamed_to_env_file_is_flagged(repo: Repo) -> None:
    repo.commit({"old.txt": "A=1\nB=2\nC=3\n"})
    repo.git("mv", "old.txt", ".env")
    assert git_guard.staged() == [Finding(".env", "env file", ENV_DETAIL)]


def test_staged_modified_env_file_is_flagged(repo: Repo) -> None:
    repo.commit({"sub/.env": "A=1\n"})
    repo.stage({"sub/.env": "A=2\n"})
    assert git_guard.staged() == [Finding("sub/.env", "env file", ENV_DETAIL)]


def test_staged_non_ascii_path(repo: Repo) -> None:
    repo.stage({"café/über.txt": f"{CLAUDE_LINK}\n", "café/.env": "A=1\n"})
    assert git_guard.staged() == [
        Finding("café/.env", "env file", ENV_DETAIL),
        link_finding("café/über.txt:1"),
    ]


def test_staged_many_files_in_one_patch(repo: Repo) -> None:
    repo.stage({"a.txt": f"{CLAUDE_LINK}\n", "b.txt": f"x\n{KEY}\n", "c.txt": "clean\n"})
    assert git_guard.staged() == [link_finding("a.txt:1"), key_finding("b.txt:2")]


def test_staged_content_that_looks_like_a_diff_header(repo: Repo) -> None:
    repo.stage({"f.txt": f"++ b/evil.txt\n{CLAUDE_LINK}\n"})
    assert git_guard.staged() == [link_finding("f.txt:2")]


def test_staged_invalid_utf8_is_decoded_with_replacement(repo: Repo) -> None:
    repo.stage({"raw.txt": b"\xff\xfe caf\xe9 " + CLAUDE_LINK.encode() + b"\n"})
    assert git_guard.staged() == [link_finding("raw.txt:1")]


def test_staged_ignores_user_diff_configuration(repo: Repo) -> None:
    repo.git("config", "color.ui", "always")
    repo.git("config", "diff.external", "true")
    repo.commit({"f.txt": "a\nb\nc\nd\ne\n"})
    repo.stage({"f.txt": f"a\nb\n{CLAUDE_LINK}\nd\ne\n"})
    assert git_guard.staged() == [link_finding("f.txt:3")]


def test_staged_path_with_a_space(repo: Repo) -> None:
    repo.stage({"sp ace.txt": f"{CLAUDE_LINK}\n"})
    assert git_guard.staged() == [link_finding("sp ace.txt:1")]


def test_staged_with_mnemonic_prefix_config(repo: Repo) -> None:
    repo.git("config", "diff.mnemonicPrefix", "true")
    repo.stage({"f.txt": f"{CLAUDE_LINK}\n"})
    assert git_guard.staged() == [link_finding("f.txt:1")]


def test_staged_file_turned_into_a_symlink(repo: Repo) -> None:
    repo.commit({"f": "plain\n"})
    (repo.path / "f").unlink()
    (repo.path / "f").symlink_to(CLAUDE_LINK)
    repo.git("add", "-A")
    assert git_guard.staged() == [link_finding("f:1")]


def test_staged_with_no_prefix_config(repo: Repo) -> None:
    repo.git("config", "diff.noprefix", "true")
    repo.stage({"f.txt": f"{CLAUDE_LINK}\n"})
    assert git_guard.staged() == [link_finding("f.txt:1")]


# ---- commits --------------------------------------------------------------------------------------------------------


def test_commits_base_to_tip_newest_first(history: list[str]) -> None:
    c1, c2, c3 = history
    assert git_guard.commits(c1, c3) == [c3, c2]
    assert git_guard.commits(c2, c3) == [c3]
    assert git_guard.commits(c3, c3) == []
    assert git_guard.commits(c1, "HEAD") == [c3, c2]


@pytest.mark.parametrize("base", ["", "0", "0" * 40, "0" * 64])
def test_commits_without_a_base_lists_everything_no_remote_has(base: str, history: list[str]) -> None:
    assert git_guard.commits(base, "HEAD") == list(reversed(history))


def test_commits_empty_base_lists_whole_history_when_there_is_no_remote(history: list[str]) -> None:
    assert git_guard.commits("", history[1]) == [history[1], history[0]]


def test_commits_whitespace_base_is_not_a_sha(history: list[str]) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        git_guard.commits("  ", "HEAD")


def test_commits_a_base_that_is_not_hex_is_not_taken_for_zeros(history: list[str]) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        git_guard.commits("X", "HEAD")
    with pytest.raises(subprocess.CalledProcessError):
        git_guard.commits("0X0", "HEAD")


def test_commits_without_a_base_skips_what_a_remote_has(repo: Repo, remote: Repo, history: list[str]) -> None:
    repo.git("push", "-q", "origin", "main")
    c4 = repo.commit({"a.txt": "4\n"})
    c5 = repo.commit({"a.txt": "5\n"})
    assert git_guard.commits("", "HEAD") == [c5, c4]
    assert git_guard.commits("0" * 40, "HEAD") == [c5, c4]
    assert git_guard.commits("0" * 64, c4) == [c4]
    assert git_guard.commits("", history[2]) == []
    assert remote.git("rev-parse", "main").strip() == history[2]


def test_commits_new_branch_only_lists_its_own_commits(repo: Repo, remote: Repo, history: list[str]) -> None:
    repo.git("push", "-q", "origin", "main")
    repo.checkout("-b", "feature", history[1])
    f1 = repo.commit({"f.txt": "1\n"})
    f2 = repo.commit({"f.txt": "2\n"})
    assert git_guard.commits("", "feature") == [f2, f1]
    assert git_guard.commits("0" * 40, f2) == [f2, f1]
    assert remote.git("rev-parse", "main").strip() == history[2]


def test_commits_merge_includes_both_sides(repo: Repo, history: list[str]) -> None:
    repo.checkout("-b", "feature", history[0])
    feat = repo.commit({"b.txt": "b\n"}, "feat")
    repo.checkout("main")
    main2 = repo.commit({"c.txt": "c\n"}, "main2")
    repo.git("merge", "-q", "--no-ff", "-m", "merge", "feature")
    merge = repo.head()
    assert sorted(git_guard.commits(history[2], merge)) == sorted([merge, feat, main2])
    assert git_guard.commits(history[2], merge)[0] == merge
    assert git_guard.commits(merge, merge) == []


def test_commits_unknown_ref_raises(history: list[str]) -> None:
    with pytest.raises(subprocess.CalledProcessError) as exc:
        git_guard.commits(history[0], "nosuchtip")
    assert exc.value.cmd[-2:] == ["rev-list", f"{history[0]}..nosuchtip"]
    with pytest.raises(subprocess.CalledProcessError) as exc2:
        git_guard.commits("", "nosuchtip")
    assert exc2.value.cmd[-4:] == ["rev-list", "nosuchtip", "--not", "--remotes"]


@pytest.mark.xfail(strict=True, reason="a merge being committed is diffed against its first parent only")
def test_staged_merge_in_progress_does_not_report_what_the_merged_branch_brought(repo: Repo) -> None:
    base = repo.commit({"f.txt": "a\nb\nc\n", "g.txt": "g\n"}, "base")
    repo.checkout("-b", "feature", base)
    repo.commit({"f.txt": "theirs\nb\nc\n", "g.txt": f"{CLAUDE_LINK}\n"}, "feat")
    repo.checkout("main")
    repo.commit({"f.txt": "ours\nb\nc\n"}, "main2")
    repo.git("merge", "feature", check=False)
    repo.stage({"f.txt": "both\nb\nc\n"})
    assert git_guard.staged() == []


# ---- the git commands ------------------------------------------------------------------------------------------------

DIFF_ARGS = ["--diff-filter=ACMRT", "--text", "--no-textconv", "--no-ext-diff", "--no-color"]


@pytest.fixture
def git_calls(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The commands the guard runs: what subprocess.run is given from here on."""
    calls: list[list[str]] = []
    run = subprocess.run

    def spy(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(cmd)
        return run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    return calls


def test_staged_reads_the_names_and_a_patch_without_context(repo: Repo, git_calls: list[list[str]]) -> None:
    assert git_guard.staged() == []
    assert git_calls == [
        [*GIT, "diff", "--cached", "--name-only", "-z", *DIFF_ARGS],
        [*GIT, "diff", "--cached", "-U0", *DIFF_ARGS],
    ]


def test_pushed_reads_names_patch_message_and_tags_of_every_commit(
    history: list[str], git_calls: list[list[str]]
) -> None:
    assert git_guard.pushed(history[:2]) == []
    expected: list[list[str]] = []
    for sha in history[:2]:
        show = [*GIT, "show", "--format=", *DIFF_ARGS, sha]
        expected += [
            [*show, "--name-only", "-z"],
            [*show, "-U0"],
            [*GIT, "log", "-1", "--format=%B", sha],
            [*GIT, "tag", "--points-at", sha, "--format=%(if:equals=tag)%(objecttype)%(then)%(contents)%(end)"],
        ]
    assert git_calls == expected


# ---- pushed ---------------------------------------------------------------------------------------------------------


def test_pushed_nothing(history: list[str]) -> None:
    assert pushed_range(history[2], history[2]) == []
    assert pushed_range(history[0], "HEAD") == []
    assert pushed_range("", "HEAD") == []


def test_pushed_reports_patch_and_message_with_short_hash(repo: Repo, history: list[str]) -> None:
    sha = repo.commit({"f.txt": f"ok\n{CLAUDE_LINK}\n"}, "subject", f"see {KEY}")
    assert pushed_range(history[2], sha) == [
        link_finding(f"{sha12(sha)} f.txt:2"),
        key_finding(f"{sha12(sha)} message:3"),
    ]
    assert len(sha12(sha)) == 12


def test_pushed_order_is_newest_commit_first_then_files_then_message(repo: Repo, history: list[str]) -> None:
    c4 = repo.commit({"a.txt": f"{CLAUDE_LINK}\n", ".env": "A=1\n"}, f"{KEY}")
    c5 = repo.commit({"b.txt": f"{KEY}\n"}, "fine")
    assert pushed_range(history[2], c5) == [
        key_finding(f"{sha12(c5)} b.txt:1"),
        Finding(f"{sha12(c4)} .env", "env file", ENV_DETAIL),
        link_finding(f"{sha12(c4)} a.txt:1"),
        key_finding(f"{sha12(c4)} message:1"),
    ]


def test_pushed_message_scan_keeps_comment_lines_and_honours_allow(repo: Repo, history: list[str]) -> None:
    sha = repo.commit({"x.txt": "x\n"}, f"subject\n\n# {CLAUDE_LINK}\n{KEY} git-guard: allow\n{TOKEN}")
    assert pushed_range(history[2], sha) == [
        link_finding(f"{sha12(sha)} message:3"),
        Finding(f"{sha12(sha)} message:5", TOKEN_RULE, TOKEN_DETAIL),
    ]


def test_pushed_added_line_with_allow_marker(repo: Repo, history: list[str]) -> None:
    sha = repo.commit({"x.txt": f"{CLAUDE_LINK} git-guard: allow\n{KEY}\n"})
    assert pushed_range(history[2], sha) == [key_finding(f"{sha12(sha)} x.txt:2")]


def test_pushed_env_file_in_a_commit(repo: Repo, history: list[str]) -> None:
    sha = repo.commit({"sub/.env.local": "A=1\n", "sub/.env.example": "A=\n"})
    assert pushed_range(history[2], sha) == [Finding(f"{sha12(sha)} sub/.env.local", "env file", ENV_DETAIL)]


def test_pushed_deleted_and_removed_secrets_are_fine(repo: Repo, history: list[str]) -> None:
    bad = repo.commit({".env": "A=1\n", "f.txt": f"{CLAUDE_LINK}\n"})
    repo.git("rm", "-q", ".env")
    fixed = repo.commit({"f.txt": "clean\n"})
    assert pushed_range(bad, fixed) == []
    assert pushed_range(history[2], bad) == [
        Finding(f"{sha12(bad)} .env", "env file", ENV_DETAIL),
        link_finding(f"{sha12(bad)} f.txt:1"),
    ]


def test_pushed_line_numbers_use_zero_context(repo: Repo, history: list[str]) -> None:
    repo.commit({"f.txt": "a\nb\nc\nd\ne\n"})
    sha = repo.commit({"f.txt": f"a\nb\n{CLAUDE_LINK}\nd\ne\n"})
    assert pushed_range(history[2], sha)[0] == link_finding(f"{sha12(sha)} f.txt:3")


def test_pushed_root_commit(repo: Repo) -> None:
    root = repo.commit({"f.txt": f"x\n{CLAUDE_LINK}\n", ".env": "A=1\n"}, f"root {KEY}")
    expected = [
        Finding(f"{sha12(root)} .env", "env file", ENV_DETAIL),
        link_finding(f"{sha12(root)} f.txt:2"),
        key_finding(f"{sha12(root)} message:1"),
    ]
    assert pushed_range("", root) == expected
    assert pushed_range("0" * 40, root) == expected
    assert pushed_range("0" * 64, "HEAD") == expected


def test_pushed_non_ascii_path_and_invalid_utf8_message(repo: Repo) -> None:
    sha = repo.commit({"café.txt": f"{CLAUDE_LINK}\n"}, f"subject é {KEY}")
    assert pushed_range("", sha) == [
        link_finding(f"{sha12(sha)} café.txt:1"),
        key_finding(f"{sha12(sha)} message:1"),
    ]


def test_pushed_ignores_user_diff_configuration(repo: Repo) -> None:
    repo.git("config", "color.ui", "always")
    repo.git("config", "diff.external", "true")
    base = repo.commit({"f.txt": "a\nb\n"})
    sha = repo.commit({"f.txt": f"a\n{CLAUDE_LINK}\nb\n"})
    assert pushed_range(base, sha) == [link_finding(f"{sha12(sha)} f.txt:2")]


def test_pushed_merge_commit_adds_nothing_of_its_own_when_it_is_clean(repo: Repo, history: list[str]) -> None:
    repo.checkout("-b", "feature", history[2])
    feat = repo.commit({"b.txt": f"{CLAUDE_LINK}\n"}, "feat")
    repo.checkout("main")
    main2 = repo.commit({"c.txt": f"{KEY}\n"}, "main2")
    repo.git("merge", "-q", "--no-ff", "-m", "merge", "feature")
    merge = repo.head()
    assert git_guard.pushed([merge]) == []
    assert pushed_range(history[2], merge) == [
        key_finding(f"{sha12(main2)} c.txt:1"),
        link_finding(f"{sha12(feat)} b.txt:1"),
    ]


@pytest.mark.parametrize("into", ["main", "feature"])
def test_pushed_clean_merge_of_a_link_the_remote_already_has(
    repo: Repo, remote: Repo, history: list[str], monkeypatch: pytest.MonkeyPatch, into: str
) -> None:
    repo.git("push", "-q", "origin", "main")
    repo.checkout("-b", "feature", history[2])
    repo.commit({"b.txt": f"{CLAUDE_LINK}\n"}, "feat")
    repo.git("push", "-q", "origin", "feature")
    repo.checkout("main")
    repo.commit({"c.txt": "clean\n"}, "main2")
    if into == "feature":
        repo.checkout("feature")
    repo.git("merge", "-q", "--no-ff", "-m", "merge", "main" if into == "feature" else "feature")
    assert len(repo.git("rev-list", "--parents", "-n1", "HEAD").split()) == 3
    assert git_guard.pushed([repo.head()]) == []
    assert git_guard.run(["pushed"]) == []
    monkeypatch.setenv("PRE_COMMIT_REMOTE_NAME", "origin")
    assert git_guard.run(["pushed"]) == []
    assert remote.git("rev-parse", "feature").strip() != repo.head()


def diverge(repo: Repo) -> tuple[str, str]:
    """Two branches that change different lines of f.txt; main is checked out, feature is not merged yet."""
    base = repo.commit({"f.txt": "1\n2\n3\n4\n5\n"}, "base")
    repo.checkout("-b", "feature", base)
    feat = repo.commit({"f.txt": "one\n2\n3\n4\n5\n"}, "feat")
    repo.checkout("main")
    main2 = repo.commit({"f.txt": "1\n2\n3\n4\nfive\n"}, "main2")
    return feat, main2


def test_pushed_evil_merge_reports_the_link_added_in_the_merge_result(repo: Repo) -> None:
    feat, main2 = diverge(repo)
    repo.git("merge", "-q", "--no-commit", "--no-ff", "feature")
    repo.stage({"f.txt": f"one\n2\n3\n4\nfive\n{CLAUDE_LINK}\n"})
    repo.git("commit", "-q", "-m", "merge")
    merge = repo.head()
    assert repo.git("rev-list", "--parents", "-n1", "HEAD").split() == [merge, main2, feat]
    assert repo.git("show", "--format=", "-U0", "HEAD").startswith("diff --cc f.txt\n")
    assert git_guard.pushed([merge]) == [link_finding(f"{sha12(merge)} f.txt:6")]
    assert git_guard.pushed([merge, feat]) == [link_finding(f"{sha12(merge)} f.txt:6")]


def test_pushed_evil_merge_reports_every_kind_of_finding_with_the_right_lines(repo: Repo) -> None:
    diverge(repo)
    repo.git("merge", "-q", "--no-commit", "--no-ff", "feature")
    repo.stage({"f.txt": f"{KEY}\none\n2\n3\n4\nfive\n{CLAUDE_LINK}\n{TOKEN}\n"})
    repo.git("commit", "-q", "-m", f"merge {CLAUDE_LINK_2}")
    merge = repo.head()
    assert git_guard.pushed([merge]) == [
        key_finding(f"{sha12(merge)} f.txt:1"),
        link_finding(f"{sha12(merge)} f.txt:7"),
        Finding(f"{sha12(merge)} f.txt:8", TOKEN_RULE, TOKEN_DETAIL),
        link_finding(f"{sha12(merge)} message:1"),
    ]


def test_pushed_evil_merge_only_reports_the_lines_no_parent_has(repo: Repo) -> None:
    base = repo.commit({"f.txt": "1\n2\n3\n4\n5\n"}, "base")
    repo.checkout("-b", "feature", base)
    repo.commit({"f.txt": f"{KEY}\n2\n3\n4\n5\n"}, "feat")
    repo.checkout("main")
    repo.commit({"f.txt": f"1\n2\n3\n4\n{CLAUDE_LINK}\n"}, "main2")
    repo.git("merge", "-q", "--no-commit", "--no-ff", "feature")
    repo.stage({"f.txt": f"{KEY}\n2\n3\n4\n{CLAUDE_LINK}\n{TOKEN}\n"})
    repo.git("commit", "-q", "-m", "merge")
    merge = repo.head()
    assert git_guard.pushed([merge]) == [Finding(f"{sha12(merge)} f.txt:6", TOKEN_RULE, TOKEN_DETAIL)]


@pytest.mark.parametrize("link_side", ["main", "feature"])
def test_pushed_conflict_resolution_keeping_a_parents_link_adds_nothing(repo: Repo, link_side: str) -> None:
    base = repo.commit({"f.txt": "a\nb\nc\n"}, "base")
    repo.checkout("-b", "feature", base)
    feat = repo.commit({"f.txt": f"{CLAUDE_LINK if link_side == 'feature' else 'theirs'}\nf\nc\n"}, "feat")
    repo.checkout("main")
    main2 = repo.commit({"f.txt": f"{CLAUDE_LINK if link_side == 'main' else 'ours'}\nm\nc\n"}, "main2")
    repo.git("merge", "feature", check=False)
    assert "<<<<<<<" in (repo.path / "f.txt").read_text(encoding="utf-8")
    repo.stage({"f.txt": f"{CLAUDE_LINK}\n{KEY}\nc\n"})
    repo.git("commit", "-q", "-m", "merge")
    merge = repo.head()
    assert repo.git("show", "--format=", "-U0", "HEAD").startswith("diff --cc f.txt\n")
    assert git_guard.pushed([merge]) == [key_finding(f"{sha12(merge)} f.txt:2")]
    link_commit = main2 if link_side == "main" else feat
    assert git_guard.pushed([link_commit]) == [link_finding(f"{sha12(link_commit)} f.txt:1")]


def test_pushed_conflict_resolution_adding_a_link_in_a_merge_commit(repo: Repo) -> None:
    base = repo.commit({"f.txt": "base\n"})
    repo.checkout("-b", "feature", base)
    repo.commit({"f.txt": "feature\n"}, "feat")
    repo.checkout("main")
    repo.commit({"f.txt": "main\n"}, "main2")
    repo.git("merge", "feature", check=False)
    repo.stage({"f.txt": f"resolved\n{CLAUDE_LINK}\n"})
    repo.git("commit", "-q", "-m", "merge")
    merge = repo.head()
    assert git_guard.pushed([merge]) == [link_finding(f"{sha12(merge)} f.txt:2")]


def test_pushed_octopus_merge_reports_the_link_added_in_the_merge_result(repo: Repo) -> None:
    base = repo.commit({"f.txt": "1\n2\n3\n4\n5\n6\n7\n"}, "base")
    repo.checkout("-b", "one", base)
    one = repo.commit({"f.txt": "ONE\n2\n3\n4\n5\n6\n7\n"}, "one")
    repo.checkout("-b", "two", base)
    two = repo.commit({"f.txt": "1\n2\n3\nTWO\n5\n6\n7\n"}, "two")
    repo.checkout("main")
    main2 = repo.commit({"f.txt": "1\n2\n3\n4\n5\n6\nSEVEN\n"}, "main2")
    repo.git("merge", "-q", "--no-commit", "one", "two")
    repo.stage({"f.txt": f"ONE\n2\n3\nTWO\n5\n6\nSEVEN\n{CLAUDE_LINK}\n{KEY}\n"})
    repo.git("commit", "-q", "-m", "octopus")
    merge = repo.head()
    assert repo.git("rev-list", "--parents", "-n1", "HEAD").split() == [merge, main2, one, two]
    assert "\n@@@@ -" in repo.git("show", "--format=", "-U0", "HEAD")
    assert git_guard.pushed([merge]) == [
        link_finding(f"{sha12(merge)} f.txt:8"),
        key_finding(f"{sha12(merge)} f.txt:9"),
    ]


def test_pushed_clean_octopus_merge_adds_nothing(repo: Repo) -> None:
    base = repo.commit({"f.txt": "1\n2\n3\n"}, "base")
    repo.checkout("-b", "one", base)
    repo.commit({"a.txt": f"{CLAUDE_LINK}\n"}, "one")
    repo.checkout("-b", "two", base)
    repo.commit({"b.txt": f"{KEY}\n"}, "two")
    repo.checkout("main")
    repo.commit({"c.txt": "c\n"}, "main2")
    repo.git("merge", "-q", "one", "two")
    assert len(repo.git("rev-list", "--parents", "-n1", "HEAD").split()) == 4
    assert git_guard.pushed([repo.head()]) == []


def test_pushed_all_commits_without_a_remote_vs_with_one(repo: Repo, remote: Repo) -> None:
    old = repo.commit({"a.txt": f"{CLAUDE_LINK}\n"})
    repo.git("push", "-q", "origin", "main")
    new = repo.commit({"b.txt": f"{KEY}\n"})
    assert pushed_range("", new) == [key_finding(f"{sha12(new)} b.txt:1")]
    assert pushed_range("0" * 40, new) == [key_finding(f"{sha12(new)} b.txt:1")]
    assert pushed_range(old, new) == [key_finding(f"{sha12(new)} b.txt:1")]


def test_pushed_file_turned_into_a_symlink(repo: Repo) -> None:
    base = repo.commit({"f": "plain\n"})
    (repo.path / "f").unlink()
    (repo.path / "f").symlink_to(CLAUDE_LINK)
    repo.git("add", "-A")
    sha = repo.commit({})
    assert pushed_range(base, sha) == [link_finding(f"{sha12(sha)} f:1")]


def test_pushed_unknown_ref_raises(history: list[str]) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        pushed_range(history[0], "nosuchtip")


# ---- how git is run: prefixes, quoting, binary files, symlinks, odd characters --------------------------------------


def test_scan_patch_path_loses_the_tab_git_adds_after_a_name_with_a_space() -> None:
    text = patch("diff --git a/sp ace.txt b/sp ace.txt", "--- a/sp ace.txt\t", "+++ b/sp ace.txt\t", "@@ -0,0 +1 @@")
    assert git_guard.scan_patch(text + "+" + CLAUDE_LINK + "\n") == [link_finding("sp ace.txt:1")]


def test_scan_patch_path_keeps_a_trailing_space_of_the_name() -> None:
    text = patch("diff --git a/x b/x", "+++ b/trail \t", "@@ -0,0 +1 @@", "+" + CLAUDE_LINK)
    assert git_guard.scan_patch(text) == [link_finding("trail :1")]


def test_scan_patch_path_loses_every_trailing_tab() -> None:
    text = patch("diff --git a/x b/x", "+++ b/f.txt\t\t", "@@ -0,0 +1 @@", "+" + CLAUDE_LINK)
    assert git_guard.scan_patch(text) == [link_finding("f.txt:1")]


@pytest.mark.parametrize("sep", LINE_BREAKS)
def test_scan_patch_only_a_newline_ends_a_patch_line(sep: str) -> None:
    text = patch(*file_header("f.txt"), "@@ -0,0 +1,3 @@", f"+a{sep}{CLAUDE_LINK}", "+b", f"+{KEY}")
    assert git_guard.scan_patch(text) == [link_finding("f.txt:1"), key_finding("f.txt:3")]


@pytest.mark.parametrize("sep", LINE_BREAKS)
def test_scan_patch_a_line_break_character_cannot_fake_a_header(sep: str) -> None:
    text = patch(*file_header("f.txt"), "@@ -0,0 +1,2 @@", f"+a{sep}+++ b/evil.txt", f"+{sep}@@ -0,0 +9 @@", "+" + KEY)
    assert git_guard.scan_patch(text) == [key_finding("f.txt:3")]


@pytest.mark.parametrize("sep", LINE_BREAKS)
def test_a_line_break_character_before_a_link_in_added_lines_and_in_the_message(repo: Repo, sep: str) -> None:
    repo.stage({"f.txt": f"a{sep}{CLAUDE_LINK}\nsecond\n{KEY}\n"})
    assert git_guard.staged() == [link_finding("f.txt:1"), key_finding("f.txt:3")]
    sha = repo.commit({}, f"s{sep}{CLAUDE_LINK}\nlast {KEY}")
    assert git_guard.pushed([sha]) == [
        link_finding(f"{sha12(sha)} f.txt:1"),
        key_finding(f"{sha12(sha)} f.txt:3"),
        link_finding(f"{sha12(sha)} message:1"),
        key_finding(f"{sha12(sha)} message:2"),
    ]


@pytest.mark.parametrize("sep", LINE_BREAKS)
def test_a_line_break_character_in_a_message_file(tmp_path: Path, sep: str) -> None:
    msg = tmp_path / "MSG"
    msg.write_bytes(f"s{sep}{CLAUDE_LINK}\n{KEY}\n".encode())
    assert git_guard.run(["message", str(msg)]) == [link_finding("commit message:1"), key_finding("commit message:2")]


def test_staged_file_with_a_nul_byte(repo: Repo) -> None:
    repo.stage({"data.bin": b"\x00\x01bin\n" + CLAUDE_LINK.encode() + b"\n\x00" + KEY.encode()})
    assert git_guard.staged() == [link_finding("data.bin:2"), key_finding("data.bin:3")]


def test_pushed_file_with_a_nul_byte(repo: Repo) -> None:
    sha = repo.commit({"data.bin": b"\x00\x01bin\n" + CLAUDE_LINK.encode() + b"\n"})
    assert git_guard.pushed([sha]) == [link_finding(f"{sha12(sha)} data.bin:2")]


@pytest.mark.parametrize("attribute", ["binary", "-diff", "-text"])
def test_staged_and_pushed_file_with_a_binary_gitattribute(repo: Repo, attribute: str) -> None:
    repo.stage({".gitattributes": f"*.dat {attribute}\n", "plain.dat": f"x\n{CLAUDE_LINK}\n"})
    assert git_guard.staged() == [link_finding("plain.dat:2")]
    sha = repo.commit({})
    assert git_guard.pushed([sha]) == [link_finding(f"{sha12(sha)} plain.dat:2")]


def test_staged_and_pushed_ignore_textconv_and_external_diff_drivers(repo: Repo) -> None:
    repo.git("config", "diff.hide.textconv", "true")
    repo.git("config", "diff.hide.command", "true")
    repo.stage({".gitattributes": "*.dat diff=hide\n", "x.dat": f"{CLAUDE_LINK}\n"})
    assert git_guard.staged() == [link_finding("x.dat:1")]
    sha = repo.commit({})
    assert git_guard.pushed([sha]) == [link_finding(f"{sha12(sha)} x.dat:1")]


def test_staged_ignores_a_global_external_diff_and_color(repo: Repo) -> None:
    repo.git("config", "diff.external", "true")
    repo.git("config", "color.diff", "always")
    repo.git("config", "color.ui", "always")
    repo.stage({"f.txt": f"{CLAUDE_LINK}\n"})
    assert git_guard.staged() == [link_finding("f.txt:1")]


def test_staged_new_symlink(repo: Repo) -> None:
    repo.stage({"x": "x\n"})
    (repo.path / "link").symlink_to(CLAUDE_LINK)
    repo.git("add", "-A")
    assert git_guard.staged() == [link_finding("link:1")]


def test_staged_and_pushed_path_with_a_space(repo: Repo) -> None:
    repo.stage({"my dir/sp ace.txt": f"x\n{CLAUDE_LINK}\n", "trail ": f"{KEY}\n", "my dir/.env": "A=1\n"})
    assert git_guard.staged() == [
        Finding("my dir/.env", "env file", ENV_DETAIL),
        link_finding("my dir/sp ace.txt:2"),
        key_finding("trail :1"),
    ]
    sha = repo.commit({})
    assert git_guard.pushed([sha]) == [
        Finding(f"{sha12(sha)} my dir/.env", "env file", ENV_DETAIL),
        link_finding(f"{sha12(sha)} my dir/sp ace.txt:2"),
        key_finding(f"{sha12(sha)} trail :1"),
    ]


def test_staged_and_pushed_renamed_file_with_an_added_line(repo: Repo) -> None:
    repo.commit({"old name.txt": "1\n2\n3\n4\n5\n6\n"})
    repo.git("mv", "old name.txt", "new name.txt")
    repo.stage({"new name.txt": f"1\n2\n3\n4\n5\n6\n{CLAUDE_LINK}\n"})
    assert git_guard.staged() == [link_finding("new name.txt:7")]
    sha = repo.commit({})
    assert git_guard.pushed([sha]) == [link_finding(f"{sha12(sha)} new name.txt:7")]


@pytest.mark.parametrize("config", [["diff.mnemonicPrefix", "true"], ["diff.noprefix", "true"]])
def test_staged_and_pushed_with_prefix_settings_in_the_repository(repo: Repo, config: list[str]) -> None:
    repo.git("config", *config)
    repo.stage({"f.txt": f"{CLAUDE_LINK}\n"})
    assert git_guard.staged() == [link_finding("f.txt:1")]
    sha = repo.commit({})
    assert git_guard.pushed([sha]) == [link_finding(f"{sha12(sha)} f.txt:1")]


def test_staged_and_pushed_with_both_prefix_settings(repo: Repo) -> None:
    repo.git("config", "diff.mnemonicPrefix", "true")
    repo.git("config", "diff.noprefix", "true")
    repo.stage({"f.txt": f"{CLAUDE_LINK}\n"})
    assert git_guard.staged() == [link_finding("f.txt:1")]
    sha = repo.commit({})
    assert git_guard.pushed([sha]) == [link_finding(f"{sha12(sha)} f.txt:1")]


def test_staged_and_pushed_noprefix_setting_does_not_swallow_a_b_directory(repo: Repo) -> None:
    repo.git("config", "diff.noprefix", "true")
    repo.stage({"b/z.txt": f"{CLAUDE_LINK}\n", "a/y.txt": f"{KEY}\n"})
    assert git_guard.staged() == [key_finding("a/y.txt:1"), link_finding("b/z.txt:1")]
    sha = repo.commit({})
    assert git_guard.pushed([sha]) == [key_finding(f"{sha12(sha)} a/y.txt:1"), link_finding(f"{sha12(sha)} b/z.txt:1")]


GIT_SETTINGS = [
    ("core.quotePath", "true", "false"),
    ("diff.noprefix", "true", "false"),
    ("diff.mnemonicPrefix", "true", "false"),
    ("diff.srcPrefix", "old/", "a/"),
    ("diff.dstPrefix", "new/", "b/"),
]


@pytest.mark.parametrize(("key", "user", "reset"), GIT_SETTINGS)
def test_git_overrides_the_settings_of_the_repository_that_change_the_diff_paths(
    repo: Repo, key: str, user: str, reset: str
) -> None:
    repo.git("config", key, user)
    assert repo.git("config", "--get", key) == user + "\n"
    assert git_guard.git("config", "--get", key) == reset + "\n"


def test_git_overrides_come_with_their_c_options_in_order(repo: Repo) -> None:
    assert [setting for setting, _, _ in GIT_SETTINGS] == [name.split("=")[0] for name in GIT_CONFIG]
    with pytest.raises(subprocess.CalledProcessError) as exc:
        git_guard.git("config", "--get", "no.such.key")
    assert exc.value.cmd == [*GIT, "config", "--get", "no.such.key"]
    assert exc.value.returncode == 1


@pytest.mark.parametrize(("key", "user", "_reset"), GIT_SETTINGS)
def test_staged_and_pushed_with_each_setting_in_the_repository(repo: Repo, key: str, user: str, _reset: str) -> None:
    repo.git("config", key, user)
    repo.stage({"f.txt": f"{CLAUDE_LINK}\n", "café/b/über.txt": f"x\n{KEY}\n", "a/y.txt": f"{TOKEN}\n"})
    expected = [
        Finding("a/y.txt:1", TOKEN_RULE, TOKEN_DETAIL),
        key_finding("café/b/über.txt:2"),
        link_finding("f.txt:1"),
    ]
    assert git_guard.staged() == expected
    sha = repo.commit({})
    assert git_guard.pushed([sha]) == [Finding(f"{sha12(sha)} {f.where}", f.rule, f.detail) for f in expected]


@pytest.mark.parametrize("prefix", ["new/", "", "b/", "x/b/", "b", "w/", "+++ "])
def test_staged_and_pushed_with_a_destination_prefix_setting(repo: Repo, prefix: str) -> None:
    repo.git("config", "diff.dstPrefix", prefix)
    repo.stage({"f.txt": f"{CLAUDE_LINK}\n", "b/g.txt": f"{KEY}\n"})
    assert git_guard.staged() == [key_finding("b/g.txt:1"), link_finding("f.txt:1")]
    sha = repo.commit({})
    assert git_guard.pushed([sha]) == [key_finding(f"{sha12(sha)} b/g.txt:1"), link_finding(f"{sha12(sha)} f.txt:1")]


@pytest.mark.parametrize("prefix", ["old/", "", "a/", "+++ ", "diff "])
def test_staged_and_pushed_with_a_source_prefix_setting(repo: Repo, prefix: str) -> None:
    repo.git("config", "diff.srcPrefix", prefix)
    base = repo.commit({"f.txt": "1\n2\n"})
    repo.stage({"f.txt": f"1\n{CLAUDE_LINK}\n2\n", "g.txt": f"{KEY}\n"})
    assert git_guard.staged() == [link_finding("f.txt:2"), key_finding("g.txt:1")]
    sha = repo.commit({})
    assert pushed_range(base, sha) == [link_finding(f"{sha12(sha)} f.txt:2"), key_finding(f"{sha12(sha)} g.txt:1")]


def test_staged_and_pushed_with_a_prefix_setting_in_a_combined_diff(repo: Repo) -> None:
    for key, value in (("diff.srcPrefix", "old/"), ("diff.dstPrefix", "new/"), ("diff.mnemonicPrefix", "true")):
        repo.git("config", key, value)
    diverge(repo)
    repo.git("merge", "-q", "--no-commit", "--no-ff", "feature")
    repo.stage({"f.txt": f"one\n2\n3\n4\nfive\n{CLAUDE_LINK}\n"})
    repo.git("commit", "-q", "-m", "merge")
    assert git_guard.pushed([repo.head()]) == [link_finding(f"{sha12(repo.head())} f.txt:6")]


# ---- commit messages as git writes them ------------------------------------------------------------------------------


def edited_message(repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subject: str = "subject") -> str:
    """The message file of `git commit -v` as the commit-msg hook sees it, after an editor put SUBJECT in front."""
    saved = tmp_path / "saved-message"
    editor = tmp_path / "editor.sh"
    editor.write_text(f'#!/bin/sh\n{{ echo {shlex.quote(subject)}; cat "$1"; }} > "{saved}"\ncp "{saved}" "$1"\n')
    editor.chmod(0o755)
    monkeypatch.setenv("GIT_EDITOR", str(editor))
    repo.git("commit", "-q", "-v")
    return saved.read_text(encoding="utf-8")


@pytest.mark.parametrize("char", [";", "@", "#"])
def test_commit_v_message_is_scanned_up_to_its_scissors_line(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, char: str
) -> None:
    repo.git("config", "core.commentChar", char)
    repo.stage({"f.txt": f"{CLAUDE_LINK}\nx\n{KEY}\n"})
    text = edited_message(repo, tmp_path, monkeypatch, f"subject {QODANA_LINK}")
    assert f"\n{scissors(char)}\n" in text
    assert text.index("\ndiff --git a/f.txt b/f.txt\n") > text.index(scissors(char))
    assert CLAUDE_LINK in text
    msg = tmp_path / "msg"
    msg.write_text(text, encoding="utf-8")
    assert git_guard.run(["message", str(msg)]) == [Finding("commit message:1", QODANA_RULE, QODANA_DETAIL)]


def test_commit_v_message_with_a_clean_subject_is_clean(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.git("config", "core.commentChar", ";")
    repo.stage({"f.txt": f"{CLAUDE_LINK}\n"})
    msg = tmp_path / "msg"
    msg.write_text(edited_message(repo, tmp_path, monkeypatch), encoding="utf-8")
    assert git_guard.run(["message", str(msg)]) == []


def test_commit_v_diff_that_removes_a_link_is_clean(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.commit({"f.txt": f"keep\n{CLAUDE_LINK}\n{KEY}\n"})
    repo.stage({"f.txt": "keep\n"})
    text = edited_message(repo, tmp_path, monkeypatch)
    assert f"-{CLAUDE_LINK}\n" in text
    assert f"-{KEY}\n" in text
    msg = tmp_path / "msg"
    msg.write_text(text, encoding="utf-8")
    assert git_guard.run(["message", str(msg)]) == []


@pytest.mark.parametrize("comment", ["//", "##", ">>", "<!--", "ä"])
def test_commit_v_message_with_a_comment_string_is_scanned_up_to_its_scissors_line(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, comment: str
) -> None:
    repo.git("config", "core.commentString", comment)
    repo.stage({"f.txt": f"{CLAUDE_LINK}\n"})
    text = edited_message(repo, tmp_path, monkeypatch, f"subject {QODANA_LINK}")
    assert f"\n{scissors(comment)}\n" in text
    msg = tmp_path / "msg"
    msg.write_text(text, encoding="utf-8")
    assert git_guard.run(["message", str(msg)]) == [Finding("commit message:1", QODANA_RULE, QODANA_DETAIL)]


def test_commit_v_message_with_a_typed_scissors_line_is_scanned_up_to_the_real_one(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.stage({"f.txt": f"{CLAUDE_LINK}\n"})
    msg = tmp_path / "msg"
    msg.write_text(edited_message(repo, tmp_path, monkeypatch, f"subject\n{scissors()}\nsee {KEY}"), encoding="utf-8")
    assert git_guard.run(["message", str(msg)]) == [key_finding("commit message:3")]


def test_a_scissors_line_given_with_m_cuts_nothing(repo: Repo) -> None:
    repo.stage({"f.txt": "x\n"})
    repo.git("commit", "-q", "-m", "subject", "-m", scissors(), "-m", f"see {CLAUDE_LINK}")
    edit_msg = repo.path / ".git" / "COMMIT_EDITMSG"
    assert scissors() in edit_msg.read_text(encoding="utf-8")
    assert git_guard.run(["message", str(edit_msg)]) == [link_finding("commit message:5")]
    sha = repo.head()
    assert git_guard.pushed([sha]) == [link_finding(f"{sha12(sha)} message:5")]


def test_comment_lines_given_with_m_are_kept_by_git_and_scanned(repo: Repo) -> None:
    repo.stage({"f.txt": "x\n"})
    repo.git("commit", "-q", "-m", "subject", "-m", f"# {CLAUDE_LINK}", "-m", f"; {KEY}")
    edit_msg = repo.path / ".git" / "COMMIT_EDITMSG"
    assert f"# {CLAUDE_LINK}" in edit_msg.read_text(encoding="utf-8")
    assert git_guard.run(["message", str(edit_msg)]) == [
        link_finding("commit message:3"),
        key_finding("commit message:5"),
    ]
    sha = repo.head()
    assert git_guard.pushed([sha]) == [link_finding(f"{sha12(sha)} message:3"), key_finding(f"{sha12(sha)} message:5")]


# ---- tags -----------------------------------------------------------------------------------------------------------


def tag(repo: Repo, name: str, message: str, target: str = "HEAD") -> None:
    """An annotated tag whose message git keeps as it is."""
    repo.git("tag", "-a", "--cleanup=verbatim", "-m", message, name, target)


def test_pushed_scans_the_message_of_an_annotated_tag(repo: Repo, history: list[str]) -> None:
    tag(repo, "v1", f"release\n\nsee {CLAUDE_LINK}\n{KEY} git-guard: allow\n# {TOKEN}")
    sha = history[2]
    assert git_guard.pushed([sha]) == [
        link_finding(f"{sha12(sha)} tag message:3"),
        Finding(f"{sha12(sha)} tag message:5", TOKEN_RULE, TOKEN_DETAIL),
    ]


def test_pushed_scans_the_tags_of_every_commit_given(repo: Repo, history: list[str]) -> None:
    tag(repo, "old", f"old {KEY}", history[0])
    tag(repo, "new", f"new {CLAUDE_LINK}", history[2])
    assert git_guard.pushed([history[2], history[1], history[0]]) == [
        link_finding(f"{sha12(history[2])} tag message:1"),
        key_finding(f"{sha12(history[0])} tag message:1"),
    ]


def test_pushed_tag_messages_of_one_commit_count_lines_across_its_tags(repo: Repo, history: list[str]) -> None:
    tag(repo, "a-first", "one\ntwo")
    tag(repo, "b-second", f"three {KEY}\nfour\n{CLAUDE_LINK}")
    sha = history[2]
    assert git_guard.pushed([sha]) == [
        key_finding(f"{sha12(sha)} tag message:3"),
        link_finding(f"{sha12(sha)} tag message:5"),
    ]


def test_pushed_a_lightweight_tag_does_not_repeat_the_commit_message(repo: Repo) -> None:
    sha = repo.commit({"f.txt": "x\n"}, f"subject {CLAUDE_LINK}")
    repo.git("tag", "light")
    repo.git("tag", "other-light")
    assert git_guard.pushed([sha]) == [link_finding(f"{sha12(sha)} message:1")]
    tag(repo, "v1", "clean release")
    assert git_guard.pushed([sha]) == [link_finding(f"{sha12(sha)} message:1")]


def test_pushed_a_lightweight_tag_still_takes_a_line_of_the_tag_messages(repo: Repo) -> None:
    sha = repo.commit({"f.txt": "x\n"})
    repo.git("tag", "a-light")
    tag(repo, "b-annotated", f"see {CLAUDE_LINK}")
    assert git_guard.pushed([sha]) == [link_finding(f"{sha12(sha)} tag message:2")]


def test_pushed_a_tag_on_a_commit_outside_the_pushed_list_is_not_scanned(repo: Repo, history: list[str]) -> None:
    tag(repo, "v1", f"release {CLAUDE_LINK}", history[0])
    assert git_guard.pushed([history[2], history[1]]) == []
    assert git_guard.pushed([]) == []
    assert pushed_range(history[0], history[2]) == []
    assert pushed_range("", history[0]) == [link_finding(f"{sha12(history[0])} tag message:1")]


def test_pushed_commit_patch_message_and_tag_message_in_that_order(repo: Repo, history: list[str]) -> None:
    sha = repo.commit({"f.txt": f"{CLAUDE_LINK}\n", ".env": "A=1\n"}, f"subject {KEY}")
    tag(repo, "v1", f"release {TOKEN}")
    assert git_guard.pushed([sha]) == [
        Finding(f"{sha12(sha)} .env", "env file", ENV_DETAIL),
        link_finding(f"{sha12(sha)} f.txt:1"),
        key_finding(f"{sha12(sha)} message:1"),
        Finding(f"{sha12(sha)} tag message:1", TOKEN_RULE, TOKEN_DETAIL),
    ]


def test_pushed_tag_message_allow_marker_and_clean_tags(repo: Repo, history: list[str]) -> None:
    tag(repo, "v1", f"{CLAUDE_LINK} git-guard: allow")
    tag(repo, "v2", "clean")
    assert git_guard.pushed([history[2]]) == []


def test_pushed_commit_without_any_tag(history: list[str]) -> None:
    assert git_guard.pushed([history[0]]) == []


def test_run_pushed_scans_the_annotated_tags_of_the_unpushed_commits(
    repo: Repo, remote: Repo, history: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.git("push", "-q", "origin", "main")
    tag(repo, "on-pushed-commit", f"old {KEY}", history[1])
    repo.checkout("--detach", history[1])
    loose = repo.commit({"x.txt": "x\n"})
    tag(repo, "on-loose-commit", f"new {CLAUDE_LINK}", loose)
    repo.checkout("main")
    monkeypatch.setenv("PRE_COMMIT_REMOTE_NAME", "origin")
    assert git_guard.run(["pushed"]) == [link_finding(f"{sha12(loose)} tag message:1")]
    assert git_guard.main(["pushed"]) == 1


def test_main_pushed_reports_a_tag_message(repo: Repo, history: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    tag(repo, "v1", f"release {CLAUDE_LINK}")
    assert git_guard.main(["pushed", history[1], history[2]]) == 1
    assert capsys.readouterr().err == (
        f"{sha12(history[2])} tag message:1: {LINK_RULE}: {LINK_DETAIL}\n" + SUMMARY.format(n=1) + "\n"
    )


# ---- UTF-16 text ----------------------------------------------------------------------------------------------------


def test_scan_line_sees_through_the_nul_beside_every_character_of_utf16_text() -> None:
    assert git_guard.scan_line(utf16(CLAUDE_LINK), "w") == [link_finding("w")]
    assert git_guard.scan_line("\0" + utf16(KEY), "w") == [key_finding("w")]
    assert git_guard.scan_line(utf16(QODANA_LINK + " " + TOKEN), "w") == [
        Finding("w", QODANA_RULE, QODANA_DETAIL),
        Finding("w", TOKEN_RULE, TOKEN_DETAIL),
    ]
    assert git_guard.scan_line("ht\0tps://claude" + ".ai/chat/x\0", "w") == [link_finding("w")]


def test_scan_line_the_allow_marker_works_in_utf16_text_too() -> None:
    assert git_guard.scan_line(utf16(f"{CLAUDE_LINK} git-guard: allow"), "w") == []
    assert git_guard.scan_line(utf16(f"git-guard: allow {KEY}"), "w") == []
    assert git_guard.scan_line(utf16(f"{CLAUDE_LINK} git-guard:allow"), "w") == [link_finding("w")]
    assert git_guard.scan_line(f"git-guard: al\0low {CLAUDE_LINK}", "w") == []


def test_scan_text_and_scan_patch_utf16() -> None:
    assert git_guard.scan_text(utf16(f"a\n{CLAUDE_LINK} git-guard: allow\n{KEY}\n"), "m") == [key_finding("m:3")]
    text = patch(*file_header("f.txt"), "@@ -0,0 +1,3 @@", "+" + utf16("k"), "+" + utf16(CLAUDE_LINK), "+" + utf16(KEY))
    assert git_guard.scan_patch(text) == [link_finding("f.txt:2"), key_finding("f.txt:3")]


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_staged_and_pushed_utf16_file(repo: Repo, encoding: str, newline: str) -> None:
    lines = ["k", CLAUDE_LINK, "z", f"{KEY} git-guard: allow", TOKEN, "end", ""]
    repo.stage({"u.txt": newline.join(lines).encode(encoding)})
    expected = [link_finding("u.txt:2"), Finding("u.txt:5", TOKEN_RULE, TOKEN_DETAIL)]
    assert git_guard.staged() == expected
    sha = repo.commit({})
    assert git_guard.pushed([sha]) == [Finding(f"{sha12(sha)} {f.where}", f.rule, f.detail) for f in expected]


def test_staged_utf16_file_whose_every_secret_line_is_allowed(repo: Repo) -> None:
    text = f"{CLAUDE_LINK} git-guard: allow\n{KEY}  git-guard: allow\n"
    repo.stage({"u.txt": text.encode("utf-16")})
    assert git_guard.staged() == []


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le"])
def test_run_message_utf16_file(tmp_path: Path, encoding: str) -> None:
    msg = tmp_path / "MSG"
    msg.write_bytes(f"subject\n{CLAUDE_LINK}\n{KEY} git-guard: allow\n{TOKEN}\n".encode(encoding))
    assert git_guard.run(["message", str(msg)]) == [
        link_finding("commit message:2"),
        Finding("commit message:4", TOKEN_RULE, TOKEN_DETAIL),
    ]


# ---- a file that lost its last newline ------------------------------------------------------------------------------


def test_scan_patch_line_numbers_after_the_no_newline_marker() -> None:
    text = patch(
        *file_header("f.txt"),
        "@@ -1 +1,2 @@",
        "-x",
        "\\ No newline at end of file",
        "+x",
        "+" + CLAUDE_LINK,
    )
    assert git_guard.scan_patch(text) == [link_finding("f.txt:2")]


def test_staged_line_numbers_of_a_file_that_gets_its_last_newline(repo: Repo) -> None:
    repo.commit({"f.txt": "x"})
    repo.stage({"f.txt": f"x\n{CLAUDE_LINK}\n"})
    assert git_guard.staged() == [link_finding("f.txt:2")]


NO_NEWLINE = "\\ No newline at end of file"


def test_scan_patch_the_no_newline_marker_is_not_a_line_and_is_not_scanned() -> None:
    text = patch(
        *file_header("f.txt"),
        "@@ -1,2 +1,3 @@",
        "-old",
        "-x",
        NO_NEWLINE,
        "+x",
        "+" + CLAUDE_LINK,
        "+" + KEY,
    )
    assert git_guard.scan_patch(text) == [link_finding("f.txt:2"), key_finding("f.txt:3")]


def test_scan_patch_the_no_newline_marker_after_an_added_line() -> None:
    text = patch(
        *file_header("f.txt"),
        "@@ -0,0 +1,2 @@",
        "+first",
        "+" + CLAUDE_LINK,
        NO_NEWLINE,
        "@@ -5 +6 @@",
        "-gone",
        NO_NEWLINE,
        "+" + KEY,
    )
    assert git_guard.scan_patch(text) == [link_finding("f.txt:2"), key_finding("f.txt:6")]


def test_scan_patch_the_no_newline_marker_in_a_combined_diff_is_not_a_line() -> None:
    text = patch(
        *combined_header("f.txt", 2),
        "@@@ -1 -1 +1,2 @@@",
        "- x",
        " +x",
        NO_NEWLINE,
        "++" + CLAUDE_LINK,
        "++" + KEY,
    )
    assert git_guard.scan_patch(text) == [link_finding("f.txt:2"), key_finding("f.txt:3")]


def test_pushed_line_numbers_of_a_file_that_gets_its_last_newline(repo: Repo, history: list[str]) -> None:
    repo.commit({"f.txt": "x"})
    sha = repo.commit({"f.txt": f"x\n{CLAUDE_LINK}\n{KEY}\n"})
    assert git_guard.pushed([sha]) == [link_finding(f"{sha12(sha)} f.txt:2"), key_finding(f"{sha12(sha)} f.txt:3")]


def test_staged_a_file_that_loses_its_last_newline_is_scanned(repo: Repo) -> None:
    repo.commit({"f.txt": f"x\n{KEY}\n"})
    repo.stage({"f.txt": f"x\n{KEY}"})
    assert git_guard.staged() == [key_finding("f.txt:2")]


# ---- unpushed -------------------------------------------------------------------------------------------------------


def add_remote(repo: Repo, tmp_path: Path, name: str) -> Repo:
    bare = tmp_path / f"{name}.git"
    bare.mkdir()
    bare_repo = Repo(bare)
    bare_repo.git("init", "-q", "--bare", "-b", "main", "--template=")
    repo.git("remote", "add", name, str(bare))
    return bare_repo


def test_unpushed_without_any_remote_is_every_commit(history: list[str]) -> None:
    assert sorted(git_guard.unpushed(None, [])) == sorted(history)
    assert sorted(git_guard.unpushed("origin", [])) == sorted(history)
    assert sorted(git_guard.unpushed("", [])) == sorted(history)


def test_unpushed_lists_full_hashes_newest_first_without_duplicates(repo: Repo) -> None:
    c1 = repo.commit({"a": "1\n"})
    repo.git("branch", "twin")
    repo.git("tag", "t1")
    c2 = repo.commit({"a": "2\n"})
    assert git_guard.unpushed(None, []) == [c2, c1]


def test_unpushed_with_a_configured_remote_vs_every_remote(repo: Repo, tmp_path: Path, history: list[str]) -> None:
    add_remote(repo, tmp_path, "origin")
    add_remote(repo, tmp_path, "backup")
    repo.git("push", "-q", "origin", f"{history[0]}:refs/heads/main")
    repo.git("push", "-q", "backup", "main")
    assert git_guard.unpushed("origin", []) == [history[2], history[1]]
    assert git_guard.unpushed("backup", []) == []
    assert git_guard.unpushed(None, []) == []
    assert git_guard.unpushed("", []) == []


def test_unpushed_an_unknown_remote_name_counts_every_remote(repo: Repo, tmp_path: Path, history: list[str]) -> None:
    add_remote(repo, tmp_path, "origin")
    repo.git("push", "-q", "origin", f"{history[0]}:refs/heads/main")
    assert git_guard.unpushed("origin", []) == [history[2], history[1]]
    # not a configured remote: whatever any remote has is pushed, as for no name at all
    assert git_guard.unpushed("nosuchremote", []) == [history[2], history[1]]
    add_remote(repo, tmp_path, "backup")
    repo.git("push", "-q", "backup", "main")
    assert git_guard.unpushed("nosuchremote", []) == []
    assert git_guard.unpushed("origin", []) == [history[2], history[1]]


def test_unpushed_a_url_counts_every_remote(repo: Repo, tmp_path: Path, history: list[str]) -> None:
    origin = add_remote(repo, tmp_path, "origin")
    add_remote(repo, tmp_path, "backup")
    repo.git("push", "-q", "origin", f"{history[0]}:refs/heads/main")
    repo.git("push", "-q", "backup", "main")
    assert git_guard.unpushed("origin", []) == [history[2], history[1]]
    for url in (str(origin.path), f"file://{origin.path}", "git@host:team/repo.git", "https://host/team/repo.git"):
        assert git_guard.unpushed(url, []) == []
    repo.git("remote", "remove", "backup")
    assert git_guard.unpushed(str(origin.path), []) == [history[2], history[1]]


def test_unpushed_remote_name_is_a_whole_name_not_a_prefix(repo: Repo, tmp_path: Path, history: list[str]) -> None:
    add_remote(repo, tmp_path, "origin")
    add_remote(repo, tmp_path, "origin2")
    repo.git("push", "-q", "origin2", "main")
    assert git_guard.unpushed("origin2", []) == []
    assert sorted(git_guard.unpushed("origin", [])) == sorted(history)
    assert git_guard.unpushed("origin22", []) == []  # not configured: every remote
    assert git_guard.unpushed(None, []) == []


def test_unpushed_remote_name_is_a_whole_name_not_a_part_of_one(repo: Repo, tmp_path: Path, history: list[str]) -> None:
    add_remote(repo, tmp_path, "origin")
    repo.git("push", "-q", "origin", "main")
    for part in ("rig", "orig", "igin", "o", "origin\n", "origin backup"):
        assert git_guard.unpushed(part, []) == []  # not configured: every remote, and origin has it all
    repo.commit({"b.txt": "1\n"})
    for part in ("rig", "orig", "igin", "o"):
        assert len(git_guard.unpushed(part, [])) == 1
    assert len(git_guard.unpushed("origin", [])) == 1


def test_unpushed_every_remote_counts_when_no_name_is_given(repo: Repo, tmp_path: Path, history: list[str]) -> None:
    add_remote(repo, tmp_path, "origin")
    add_remote(repo, tmp_path, "backup")
    repo.git("push", "-q", "origin", "main")
    c4 = repo.commit({"a.txt": "4\n"})
    repo.git("push", "-q", "backup", "main")
    assert git_guard.unpushed(None, []) == []
    assert git_guard.unpushed("origin", []) == [c4]
    assert git_guard.unpushed("backup", []) == []


def test_unpushed_covers_every_local_branch(repo: Repo, tmp_path: Path, history: list[str]) -> None:
    add_remote(repo, tmp_path, "origin")
    repo.git("push", "-q", "origin", "main")
    repo.checkout("-b", "feature")
    f1 = repo.commit({"f.txt": "1\n"})
    repo.checkout("-b", "other", history[0])
    o1 = repo.commit({"o.txt": "1\n"})
    repo.checkout("main")
    assert sorted(git_guard.unpushed("origin", [])) == sorted([f1, o1])
    repo.git("push", "-q", "origin", "feature")
    assert git_guard.unpushed("origin", []) == [o1]


def test_unpushed_covers_tags_lightweight_and_annotated(repo: Repo, tmp_path: Path, history: list[str]) -> None:
    add_remote(repo, tmp_path, "origin")
    repo.git("push", "-q", "origin", "main")
    repo.checkout("--detach", history[0])
    light = repo.commit({"l.txt": "1\n"})
    repo.git("tag", "light")
    annotated = repo.commit({"n.txt": "1\n"})
    repo.git("tag", "-a", "-m", "release", "annotated")
    repo.commit({"u.txt": "1\n"})  # on no branch and no tag
    repo.checkout("main")
    assert git_guard.unpushed("origin", []) == [annotated, light]


def test_unpushed_with_an_extra_tip(repo: Repo, tmp_path: Path, history: list[str]) -> None:
    add_remote(repo, tmp_path, "origin")
    repo.git("push", "-q", "origin", "main")
    repo.checkout("--detach", history[1])
    d1 = repo.commit({"d.txt": "1\n"})
    d2 = repo.commit({"d.txt": "2\n"})
    assert git_guard.unpushed("origin", []) == []
    assert git_guard.unpushed("origin", [d2]) == [d2, d1]
    assert git_guard.unpushed("origin", ["HEAD"]) == [d2, d1]
    assert git_guard.unpushed("origin", [d1]) == [d1]
    assert git_guard.unpushed("origin", [history[2]]) == []
    repo.git("push", "-q", "origin", f"{d2}:refs/heads/detached")
    assert git_guard.unpushed("origin", [d2]) == []


def test_unpushed_with_several_tips(repo: Repo, tmp_path: Path, history: list[str]) -> None:
    add_remote(repo, tmp_path, "origin")
    repo.git("push", "-q", "origin", "main")
    repo.checkout("--detach", history[1])
    d1 = repo.commit({"d.txt": "1\n"})
    d2 = repo.commit({"d.txt": "2\n"})
    repo.checkout("--detach", history[0])
    e1 = repo.commit({"e.txt": "1\n"})
    repo.checkout("main")
    assert sorted(git_guard.unpushed("origin", [d2, e1])) == sorted([d2, d1, e1])
    assert sorted(git_guard.unpushed("origin", [e1, d2])) == sorted([d2, d1, e1])
    assert sorted(git_guard.unpushed("origin", [d1, e1])) == sorted([d1, e1])
    assert sorted(git_guard.unpushed("origin", [e1, e1, e1])) == [e1]
    assert sorted(git_guard.unpushed("origin", [d2, d1])) == sorted([d2, d1])
    assert sorted(git_guard.unpushed(None, [d2, e1])) == sorted([d2, d1, e1])
    assert sorted(git_guard.unpushed("origin", [d2, history[2]])) == sorted([d2, d1])
    repo.git("push", "-q", "origin", f"{e1}:refs/heads/e")
    assert sorted(git_guard.unpushed("origin", [d2, e1])) == sorted([d2, d1])


def test_unpushed_with_no_tip_and_with_an_extra_tip_and_no_remote(history: list[str]) -> None:
    assert git_guard.unpushed(None, [history[1]]) == [history[2], history[1], history[0]]
    assert git_guard.unpushed("", [history[1]]) == [history[2], history[1], history[0]]
    assert git_guard.unpushed("origin", [history[1], history[0]]) == [history[2], history[1], history[0]]


def test_unpushed_tip_given_as_a_tag_name(repo: Repo, tmp_path: Path, history: list[str]) -> None:
    add_remote(repo, tmp_path, "origin")
    repo.git("push", "-q", "origin", "main")
    repo.checkout("--detach", history[1])
    d1 = repo.commit({"d.txt": "1\n"})
    repo.git("tag", "-a", "-m", "m", "v9")
    repo.checkout("main")
    assert git_guard.unpushed("origin", ["v9"]) == [d1]


def test_unpushed_unknown_tip_raises_with_the_command_line(repo: Repo, tmp_path: Path, history: list[str]) -> None:
    add_remote(repo, tmp_path, "origin")
    with pytest.raises(subprocess.CalledProcessError) as exc:
        git_guard.unpushed("origin", ["nosuchtip"])
    assert exc.value.cmd == [*GIT, "rev-list", "--branches", "--tags", "nosuchtip", "--not", "--remotes=origin"]
    for remote in (None, "", "nosuchremote"):
        with pytest.raises(subprocess.CalledProcessError) as exc2:
            git_guard.unpushed(remote, ["HEAD", "nosuchtip"])
        assert exc2.value.cmd == [*GIT, "rev-list", "--branches", "--tags", "HEAD", "nosuchtip", "--not", "--remotes"]


def test_unpushed_command_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake(*args: str) -> str:
        calls.append(args)
        return "origin\nbackup\n" if args == ("remote",) else "a\nb\n"

    monkeypatch.setattr(git_guard, "git", fake)
    assert git_guard.unpushed("origin", []) == ["a", "b"]
    assert git_guard.unpushed("backup", ["v1", "v2"]) == ["a", "b"]
    assert git_guard.unpushed(None, ["v1"]) == ["a", "b"]
    assert git_guard.unpushed("", ["v1"]) == ["a", "b"]
    assert git_guard.unpushed("elsewhere", []) == ["a", "b"]
    assert git_guard.unpushed("orig", []) == ["a", "b"]
    assert git_guard.unpushed(None, []) == ["a", "b"]
    remote = ("remote",)
    assert calls == [
        remote,
        ("rev-list", "--branches", "--tags", "--not", "--remotes=origin"),
        remote,
        ("rev-list", "--branches", "--tags", "v1", "v2", "--not", "--remotes=backup"),
        remote,
        ("rev-list", "--branches", "--tags", "v1", "--not", "--remotes"),
        remote,
        ("rev-list", "--branches", "--tags", "v1", "--not", "--remotes"),
        remote,
        ("rev-list", "--branches", "--tags", "--not", "--remotes"),
        remote,
        ("rev-list", "--branches", "--tags", "--not", "--remotes"),
        remote,
        ("rev-list", "--branches", "--tags", "--not", "--remotes"),
    ]


def test_unpushed_asks_git_for_the_configured_remotes_with_the_fixed_command_line(
    repo: Repo, git_calls: list[list[str]]
) -> None:
    assert git_guard.unpushed("origin", []) == []
    assert git_calls == [
        [*GIT, "remote"],
        [*GIT, "rev-list", "--branches", "--tags", "--not", "--remotes"],
    ]


def test_git_a_replace_ref_cannot_hide_a_commit(repo: Repo, history: list[str]) -> None:
    """`git replace` swaps an object for another in every git command that does not pass --no-replace-objects."""
    bad = repo.commit({"f.txt": f"ok\n{CLAUDE_LINK}\n"}, f"subject {KEY}")
    repo.checkout("--detach", history[2])
    clean = repo.commit({"f.txt": "ok\n"}, "all clean")
    repo.checkout("main")
    repo.git("replace", bad, clean)
    assert KEY not in repo.git("log", "-1", "--format=%B", bad)  # the replacement is what git shows
    assert git_guard.git("log", "-1", "--format=%B", bad) == f"subject {KEY}\n\n"
    assert git_guard.git("rev-parse", "--verify", bad) == bad + "\n"
    assert pushed_range(history[2], bad) == [
        link_finding(f"{sha12(bad)} f.txt:2"),
        key_finding(f"{sha12(bad)} message:1"),
    ]
    assert git_guard.unpushed(None, []) == [bad, *reversed(history)]


def test_git_a_replace_ref_cannot_drop_a_commit_from_the_list(repo: Repo, history: list[str]) -> None:
    bad = repo.commit({"f.txt": f"{CLAUDE_LINK}\n"})
    child = repo.commit({"g.txt": "ok\n"}, "child")
    skipping = repo.git("commit-tree", f"{child}^{{tree}}", "-p", history[2], "-m", "skips the bad commit").strip()
    repo.git("replace", child, skipping)  # git now takes the child for a commit whose parent is the old tip
    assert repo.git("rev-list", f"{history[2]}..{child}").split() == [child]
    assert git_guard.commits(history[2], child) == [child, bad]
    assert git_guard.commits("", child) == [child, bad, *reversed(history)]
    assert pushed_range(history[2], child) == [link_finding(f"{sha12(bad)} f.txt:1")]
    assert git_guard.run(["pushed"]) == [link_finding(f"{sha12(bad)} f.txt:1")]
    assert git_guard.unpushed(None, []) == [child, bad, *reversed(history)]
    repo.stage({"h.txt": "x\n"})
    assert git_guard.checked_out(child) == [DIRTY]


def test_git_a_replace_ref_cannot_hide_an_annotated_tag_message(repo: Repo, history: list[str]) -> None:
    tag(repo, "v1", f"release {KEY}")
    original = repo.git("rev-parse", "v1").strip()
    tag(repo, "decoy", "all clean")
    repo.git("replace", original, repo.git("rev-parse", "decoy").strip())
    repo.git("tag", "-d", "decoy")
    shown = repo.git("tag", "--points-at", history[2], "--format=%(contents)")
    assert KEY not in shown  # the replacement is what git shows
    assert git_guard.pushed([history[2]]) == [Finding(f"{sha12(history[2])} tag message:1", KEY_RULE, KEY_DETAIL)]


# ---- checked_out ----------------------------------------------------------------------------------------------------


def test_checked_out_clean_without_tip(history: list[str]) -> None:
    assert git_guard.checked_out(None) == []
    assert git_guard.checked_out("") == []


def test_checked_out_tip_is_head(history: list[str]) -> None:
    assert git_guard.checked_out(history[2]) == []


def test_checked_out_tip_is_not_head(history: list[str]) -> None:
    assert git_guard.checked_out(history[1]) == [
        Finding("HEAD", "not the pushed commit", f"check out {history[1][:12]} or push HEAD"),
    ]
    assert git_guard.checked_out("main") == []
    assert git_guard.checked_out("HEAD~1") == [
        Finding("HEAD", "not the pushed commit", "check out HEAD~1 or push HEAD"),
    ]


def test_checked_out_head_check_is_skipped_for_empty_tip_even_when_head_differs(history: list[str]) -> None:
    assert git_guard.checked_out("") == []


def test_checked_out_dirty_tracked_file(repo: Repo, history: list[str]) -> None:
    repo.write("a.txt", "changed\n")
    assert git_guard.checked_out(None) == [DIRTY]
    assert git_guard.checked_out(history[2]) == [DIRTY]


def test_checked_out_staged_change(repo: Repo, history: list[str]) -> None:
    repo.stage({"new.txt": "x\n"})
    assert git_guard.checked_out(None) == [DIRTY]


def test_checked_out_untracked_file(repo: Repo, history: list[str]) -> None:
    repo.write("untracked.txt", "x\n")
    assert git_guard.checked_out(history[2]) == [
        DIRTY,
    ]


@pytest.mark.parametrize("setting", ["no", "false", "off"])
def test_checked_out_an_untracked_file_shows_whatever_status_showuntrackedfiles_says(
    repo: Repo, history: list[str], setting: str
) -> None:
    repo.git("config", "status.showUntrackedFiles", setting)
    repo.write("untracked.txt", "x\n")
    assert repo.git("status", "--porcelain") == ""  # what the setting would hide
    assert git_guard.checked_out(None) == [DIRTY]
    assert git_guard.checked_out(history[2]) == [DIRTY]
    assert git_guard.run(["checked-out"]) == [DIRTY]


def test_checked_out_an_untracked_file_in_a_new_directory(repo: Repo, history: list[str]) -> None:
    repo.git("config", "status.showUntrackedFiles", "no")
    repo.write("new/dir/untracked.txt", "x\n")
    assert git_guard.checked_out(None) == [DIRTY]


def test_checked_out_asks_git_for_the_tip_and_the_status(
    repo: Repo, history: list[str], git_calls: list[list[str]]
) -> None:
    assert git_guard.checked_out(history[1]) == [
        Finding("HEAD", "not the pushed commit", f"check out {history[1][:12]} or push HEAD"),
    ]
    assert git_calls == [
        [*GIT, "rev-parse", "HEAD"],
        [*GIT, "rev-parse", f"{history[1]}^{{commit}}"],
        [*GIT, "status", "--porcelain", "--untracked-files=normal"],
    ]


def test_checked_out_ignored_file_is_not_dirty(repo: Repo, history: list[str]) -> None:
    repo.commit({".gitignore": "ignored.txt\n"})
    repo.write("ignored.txt", "x\n")
    assert git_guard.checked_out(None) == []


def test_checked_out_reports_both_in_order(repo: Repo, history: list[str]) -> None:
    repo.write("untracked.txt", "x\n")
    assert git_guard.checked_out(history[0]) == [
        Finding("HEAD", "not the pushed commit", f"check out {history[0][:12]} or push HEAD"),
        DIRTY,
    ]


def test_checked_out_outside_a_repository_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises(subprocess.CalledProcessError):
        git_guard.checked_out(None)


def test_checked_out_annotated_tag_of_head(repo: Repo, history: list[str]) -> None:
    repo.git("tag", "-a", "-m", "release", "v1")
    assert repo.git("rev-parse", "v1").strip() != history[2]  # the tag object, not the commit
    assert git_guard.checked_out("v1") == []


def test_checked_out_annotated_tag_of_another_commit(repo: Repo, history: list[str]) -> None:
    repo.git("tag", "-a", "-m", "release", "old", history[0])
    assert git_guard.checked_out("old") == [Finding("HEAD", "not the pushed commit", "check out old or push HEAD")]


def test_checked_out_lightweight_tag_and_branch_names(repo: Repo, history: list[str]) -> None:
    repo.git("tag", "light-head")
    repo.git("tag", "light-old", history[1])
    repo.git("branch", "side", history[0])
    assert git_guard.checked_out("light-head") == []
    assert git_guard.checked_out("light-old") == [
        Finding("HEAD", "not the pushed commit", "check out light-old or push HEAD"),
    ]
    assert git_guard.checked_out("side") == [Finding("HEAD", "not the pushed commit", "check out side or push HEAD")]


def test_checked_out_unknown_tip_raises(history: list[str]) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        git_guard.checked_out("nosuchtip")


def test_checked_out_detail_names_the_first_twelve_characters_of_a_long_tip(history: list[str]) -> None:
    assert git_guard.checked_out(history[0]) == [
        Finding("HEAD", "not the pushed commit", f"check out {history[0][:12]} or push HEAD"),
    ]
    assert len(history[0][:12]) == 12


def test_run_checked_out_annotated_tag_from_the_environment(
    repo: Repo, history: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.git("tag", "-a", "-m", "release", "v1")
    monkeypatch.setenv("PRE_COMMIT_TO_REF", "v1")
    assert git_guard.run(["checked-out"]) == []
    repo.git("tag", "-a", "-m", "release", "old", history[0])
    monkeypatch.setenv("PRE_COMMIT_TO_REF", "old")
    assert git_guard.run(["checked-out"]) == [Finding("HEAD", "not the pushed commit", "check out old or push HEAD")]


# ---- run ------------------------------------------------------------------------------------------------------------


def test_run_staged(repo: Repo) -> None:
    repo.stage({"f.txt": f"{CLAUDE_LINK}\n"})
    assert git_guard.run(["staged"]) == [link_finding("f.txt:1")]


def test_run_staged_clean_is_an_empty_list_not_none(repo: Repo) -> None:
    assert git_guard.run(["staged"]) == []


def test_run_message(tmp_path: Path) -> None:
    msg = tmp_path / "MSG"
    msg.write_text(f"subject\n# {KEY}\n{CLAUDE_LINK}\n", encoding="utf-8")
    assert git_guard.run(["message", str(msg)]) == [key_finding("commit message:2"), link_finding("commit message:3")]
    msg.write_text("fine\n", encoding="utf-8")
    assert git_guard.run(["message", str(msg)]) == []


def test_run_message_decodes_invalid_utf8(tmp_path: Path) -> None:
    msg = tmp_path / "MSG"
    msg.write_bytes(b"caf\xe9 \xff\n\xff " + CLAUDE_LINK.encode() + b"\n")
    assert git_guard.run(["message", str(msg)]) == [link_finding("commit message:2")]


def test_run_message_cuts_at_scissors(tmp_path: Path) -> None:
    msg = tmp_path / "MSG"
    msg.write_text(with_diff("subject", scissors()), encoding="utf-8")
    assert git_guard.run(["message", str(msg)]) == []
    msg.write_text(with_diff("subject", scissors(";")), encoding="utf-8")
    assert git_guard.run(["message", str(msg)]) == []
    msg.write_text(f"subject\n{scissors()}\n{KEY}\n", encoding="utf-8")
    assert git_guard.run(["message", str(msg)]) == [key_finding("commit message:3")]


def test_run_pushed_with_explicit_range(repo: Repo, history: list[str]) -> None:
    bad = repo.commit({"f.txt": f"{KEY}\n"})
    assert git_guard.run(["pushed", history[2], bad]) == [key_finding(f"{sha12(bad)} f.txt:1")]
    assert git_guard.run(["pushed", history[2], history[2]]) == []


def test_run_pushed_explicit_range_beats_the_environment(
    repo: Repo, history: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = repo.commit({"f.txt": f"{KEY}\n"})
    monkeypatch.setenv("PRE_COMMIT_FROM_REF", bad)
    monkeypatch.setenv("PRE_COMMIT_TO_REF", bad)
    assert git_guard.run(["pushed", history[2], bad]) == [key_finding(f"{sha12(bad)} f.txt:1")]


def test_run_pushed_without_environment_scans_everything_no_remote_has(repo: Repo, history: list[str]) -> None:
    bad = repo.commit({"f.txt": f"{KEY}\n"})
    repo.git("branch", "other", history[0])
    assert git_guard.run(["pushed"]) == [key_finding(f"{sha12(bad)} f.txt:1")]
    repo.checkout("--detach", history[1])
    assert git_guard.run(["pushed"]) == [key_finding(f"{sha12(bad)} f.txt:1")]


def test_run_pushed_without_environment_scans_every_local_branch_and_tag(repo: Repo, history: list[str]) -> None:
    repo.checkout("-b", "feature", history[0])
    on_branch = repo.commit({"f.txt": f"{KEY}\n"})
    repo.checkout("main")
    on_main = repo.commit({"g.txt": f"{CLAUDE_LINK}\n"})
    repo.checkout("--detach", history[1])
    on_tag = repo.commit({"h.txt": f"{TOKEN}\n"})
    repo.git("tag", "-a", "-m", "t", "v1", on_tag)
    repo.checkout("main")
    findings = git_guard.run(["pushed"])
    assert findings is not None
    assert sorted(findings, key=str) == sorted(
        [
            key_finding(f"{sha12(on_branch)} f.txt:1"),
            link_finding(f"{sha12(on_main)} g.txt:1"),
            Finding(f"{sha12(on_tag)} h.txt:1", TOKEN_RULE, TOKEN_DETAIL),
        ],
        key=str,
    )


def test_run_pushed_remote_name_from_the_environment(
    repo: Repo, remote: Repo, tmp_path: Path, history: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.git("push", "-q", "origin", "main")
    add_remote(repo, tmp_path, "backup")
    bad = repo.commit({"f.txt": f"{KEY}\n"})
    repo.git("push", "-q", "backup", "main")
    assert git_guard.run(["pushed"]) == []
    monkeypatch.setenv("PRE_COMMIT_REMOTE_NAME", "origin")
    assert git_guard.run(["pushed"]) == [key_finding(f"{sha12(bad)} f.txt:1")]
    monkeypatch.setenv("PRE_COMMIT_REMOTE_NAME", "backup")
    assert git_guard.run(["pushed"]) == []
    monkeypatch.setenv("PRE_COMMIT_REMOTE_NAME", "elsewhere")  # not a configured remote: every remote counts
    assert git_guard.run(["pushed"]) == []
    monkeypatch.setenv("PRE_COMMIT_REMOTE_NAME", str(remote.path))  # a URL: the same
    assert git_guard.run(["pushed"]) == []
    monkeypatch.setenv("PRE_COMMIT_REMOTE_NAME", "")
    assert git_guard.run(["pushed"]) == []
    assert remote.git("rev-parse", "main").strip() == history[2]


def test_run_pushed_without_remote_name_every_remote_counts(
    repo: Repo, remote: Repo, tmp_path: Path, history: list[str]
) -> None:
    repo.git("push", "-q", "origin", "main")
    other = tmp_path / "other.git"
    other.mkdir()
    Repo(other).git("init", "-q", "--bare", "-b", "main", "--template=")
    repo.git("remote", "add", "other", str(other))
    repo.commit({"f.txt": f"{KEY}\n"})
    repo.git("push", "-q", "other", "main")
    assert git_guard.run(["pushed"]) == []
    assert remote.git("rev-parse", "main").strip() == history[2]


def test_run_pushed_extra_tip_from_the_environment(
    repo: Repo, history: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.checkout("--detach", history[1])
    loose = repo.commit({"f.txt": f"{KEY}\n"})
    repo.checkout("main")
    assert git_guard.run(["pushed"]) == []
    monkeypatch.setenv("PRE_COMMIT_TO_REF", loose)
    assert git_guard.run(["pushed"]) == [key_finding(f"{sha12(loose)} f.txt:1")]
    monkeypatch.setenv("PRE_COMMIT_TO_REF", "")
    assert git_guard.run(["pushed"]) == []


def test_run_pushed_local_branch_from_the_environment(
    repo: Repo, history: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.checkout("--detach", history[1])
    loose = repo.commit({"f.txt": f"{KEY}\n"})
    repo.checkout("main")
    repo.git("update-ref", "refs/loose", loose)  # on no branch and no tag
    assert git_guard.run(["pushed"]) == []
    monkeypatch.setenv("PRE_COMMIT_LOCAL_BRANCH", "refs/loose")
    assert git_guard.run(["pushed"]) == [key_finding(f"{sha12(loose)} f.txt:1")]
    monkeypatch.setenv("PRE_COMMIT_LOCAL_BRANCH", loose)
    assert git_guard.run(["pushed"]) == [key_finding(f"{sha12(loose)} f.txt:1")]
    monkeypatch.setenv("PRE_COMMIT_LOCAL_BRANCH", "")
    assert git_guard.run(["pushed"]) == []


def test_run_pushed_to_ref_and_local_branch_are_both_tips(
    repo: Repo, history: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.checkout("--detach", history[1])
    to_ref = repo.commit({"t.txt": f"{KEY}\n"})
    repo.checkout("--detach", history[0])
    local = repo.commit({"l.txt": f"{CLAUDE_LINK}\n"})
    repo.checkout("main")
    monkeypatch.setenv("PRE_COMMIT_TO_REF", to_ref)
    assert git_guard.run(["pushed"]) == [key_finding(f"{sha12(to_ref)} t.txt:1")]
    monkeypatch.delenv("PRE_COMMIT_TO_REF")
    monkeypatch.setenv("PRE_COMMIT_LOCAL_BRANCH", local)
    assert git_guard.run(["pushed"]) == [link_finding(f"{sha12(local)} l.txt:1")]
    monkeypatch.setenv("PRE_COMMIT_TO_REF", to_ref)
    monkeypatch.setenv("PRE_COMMIT_LOCAL_BRANCH", local)
    findings = git_guard.run(["pushed"])
    assert findings is not None
    assert sorted(findings, key=str) == sorted(
        [key_finding(f"{sha12(to_ref)} t.txt:1"), link_finding(f"{sha12(local)} l.txt:1")], key=str
    )
    monkeypatch.setenv("PRE_COMMIT_TO_REF", "")
    assert git_guard.run(["pushed"]) == [link_finding(f"{sha12(local)} l.txt:1")]
    monkeypatch.setenv("PRE_COMMIT_TO_REF", to_ref)
    monkeypatch.setenv("PRE_COMMIT_LOCAL_BRANCH", "")
    assert git_guard.run(["pushed"]) == [key_finding(f"{sha12(to_ref)} t.txt:1")]


def test_run_pushed_unknown_local_branch_raises(history: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRE_COMMIT_LOCAL_BRANCH", "nosuchref")
    with pytest.raises(subprocess.CalledProcessError):
        git_guard.run(["pushed"])


def test_run_pushed_asks_for_every_tip_in_the_environment(
    repo: Repo, history: list[str], git_calls: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRE_COMMIT_TO_REF", history[2])
    monkeypatch.setenv("PRE_COMMIT_LOCAL_BRANCH", "main")
    monkeypatch.setenv("PRE_COMMIT_REMOTE_NAME", "origin")
    assert git_guard.run(["pushed"]) == []
    assert git_calls[:2] == [
        [*GIT, "remote"],
        [*GIT, "rev-list", "--branches", "--tags", history[2], "main", "--not", "--remotes"],
    ]


def test_run_pushed_ignores_from_ref(repo: Repo, history: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    bad = repo.commit({"f.txt": f"{KEY}\n"})
    monkeypatch.setenv("PRE_COMMIT_FROM_REF", bad)
    assert git_guard.run(["pushed"]) == [key_finding(f"{sha12(bad)} f.txt:1")]


def test_run_pushed_unknown_to_ref_raises(history: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRE_COMMIT_TO_REF", "nosuchref")
    with pytest.raises(subprocess.CalledProcessError):
        git_guard.run(["pushed"])


def test_run_checked_out_with_explicit_tip(repo: Repo, history: list[str]) -> None:
    assert git_guard.run(["checked-out", history[2]]) == []
    assert git_guard.run(["checked-out", history[0]]) == [
        Finding("HEAD", "not the pushed commit", f"check out {history[0][:12]} or push HEAD"),
    ]


def test_run_checked_out_explicit_tip_beats_the_environment(
    repo: Repo, history: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRE_COMMIT_TO_REF", history[0])
    assert git_guard.run(["checked-out", history[2]]) == []


def test_run_checked_out_from_environment(repo: Repo, history: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRE_COMMIT_TO_REF", history[2])
    assert git_guard.run(["checked-out"]) == []
    monkeypatch.setenv("PRE_COMMIT_TO_REF", history[1])
    assert git_guard.run(["checked-out"]) == [
        Finding("HEAD", "not the pushed commit", f"check out {history[1][:12]} or push HEAD"),
    ]
    monkeypatch.setenv("PRE_COMMIT_TO_REF", "")
    assert git_guard.run(["checked-out"]) == []


def test_run_checked_out_from_the_local_branch_alone(
    repo: Repo, history: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRE_COMMIT_LOCAL_BRANCH", history[2])
    assert git_guard.run(["checked-out"]) == []
    monkeypatch.setenv("PRE_COMMIT_LOCAL_BRANCH", history[1])
    assert git_guard.run(["checked-out"]) == [
        Finding("HEAD", "not the pushed commit", f"check out {history[1][:12]} or push HEAD"),
    ]
    monkeypatch.setenv("PRE_COMMIT_LOCAL_BRANCH", "refs/heads/main")
    assert git_guard.run(["checked-out"]) == []
    monkeypatch.setenv("PRE_COMMIT_LOCAL_BRANCH", "")
    assert git_guard.run(["checked-out"]) == []


def test_run_checked_out_the_to_ref_wins_over_the_local_branch(
    repo: Repo, history: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRE_COMMIT_TO_REF", history[2])
    monkeypatch.setenv("PRE_COMMIT_LOCAL_BRANCH", history[0])
    assert git_guard.run(["checked-out"]) == []
    monkeypatch.setenv("PRE_COMMIT_TO_REF", history[0])
    monkeypatch.setenv("PRE_COMMIT_LOCAL_BRANCH", history[2])
    assert git_guard.run(["checked-out"]) == [
        Finding("HEAD", "not the pushed commit", f"check out {history[0][:12]} or push HEAD"),
    ]


def test_run_checked_out_an_empty_to_ref_falls_back_to_the_local_branch(
    repo: Repo, history: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRE_COMMIT_TO_REF", "")
    monkeypatch.setenv("PRE_COMMIT_LOCAL_BRANCH", history[1])
    assert git_guard.run(["checked-out"]) == [
        Finding("HEAD", "not the pushed commit", f"check out {history[1][:12]} or push HEAD"),
    ]
    monkeypatch.setenv("PRE_COMMIT_LOCAL_BRANCH", history[2])
    assert git_guard.run(["checked-out"]) == []


def test_run_checked_out_without_environment_only_checks_the_tree(repo: Repo, history: list[str]) -> None:
    assert git_guard.run(["checked-out"]) == []
    repo.write("dirty.txt", "x\n")
    assert git_guard.run(["checked-out"]) == [
        DIRTY,
    ]


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["bogus"],
        ["Staged"],
        ["staged", "extra"],
        ["message"],
        ["message", "a", "b"],
        ["pushed", "base"],
        ["pushed", "base", "tip", "extra"],
        ["checked-out", "a", "b"],
        ["checked_out"],
        ["--help"],
    ],
)
def test_run_bad_usage_is_none(args: list[str]) -> None:
    assert git_guard.run(args) is None


# ---- main -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [[], ["bogus"], ["staged", "x"], ["message"], ["pushed", "only-base"], ["checked-out", "a", "b"]],
)
def test_main_bad_usage(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert git_guard.main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == USAGE + "\n"


def test_main_clean_is_silent_exit_0(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.commit({"a.txt": "1\n"})
    for args in (["staged"], ["checked-out"], ["pushed"], ["checked-out", repo.head()]):
        assert git_guard.main(args) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_main_staged_findings(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.stage({"f.txt": f"x\n{CLAUDE_LINK}\n", ".env": f"{KEY}\n"})
    assert git_guard.main(["staged"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f".env: env file: {ENV_DETAIL}\n"
        f".env:1: {KEY_RULE}: {KEY_DETAIL}\n"
        f"f.txt:2: {LINK_RULE}: {LINK_DETAIL}\n" + SUMMARY.format(n=3) + "\n"
    )


def test_main_single_finding_summary(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.stage({"f.txt": f"{TOKEN}\n"})
    assert git_guard.main(["staged"]) == 1
    assert capsys.readouterr().err == f"f.txt:1: {TOKEN_RULE}: {TOKEN_DETAIL}\n" + SUMMARY.format(n=1) + "\n"


def test_main_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(f"subject\n\n{QODANA_LINK}\n# {KEY}\n", encoding="utf-8")
    assert git_guard.main(["message", str(msg)]) == 1
    assert capsys.readouterr().err == (
        f"commit message:3: {QODANA_RULE}: {QODANA_DETAIL}\n"
        f"commit message:4: {KEY_RULE}: {KEY_DETAIL}\n" + SUMMARY.format(n=2) + "\n"
    )


def test_main_message_clean_with_comments_and_scissors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(with_diff("subject", "# comment", scissors(), f"# {KEY}", CLAUDE_LINK), encoding="utf-8")
    assert git_guard.main(["message", str(msg)]) == 0
    assert capsys.readouterr().err == ""


def test_main_message_with_invalid_utf8(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    msg = tmp_path / "MSG"
    msg.write_bytes(b"\xff\xfe " + KEY.encode() + b"\n")
    assert git_guard.main(["message", str(msg)]) == 1
    assert capsys.readouterr().err == f"commit message:1: {KEY_RULE}: {KEY_DETAIL}\n" + SUMMARY.format(n=1) + "\n"


def test_main_pushed_findings(repo: Repo, history: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    bad = repo.commit({"f.txt": f"{KEY}\n"}, f"subject {CLAUDE_LINK}")
    assert git_guard.main(["pushed", history[2], bad]) == 1
    assert capsys.readouterr().err == (
        f"{sha12(bad)} f.txt:1: {KEY_RULE}: {KEY_DETAIL}\n"
        f"{sha12(bad)} message:1: {LINK_RULE}: {LINK_DETAIL}\n" + SUMMARY.format(n=2) + "\n"
    )


def test_main_pushed_reads_the_pre_commit_environment(
    repo: Repo, history: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = repo.commit({"f.txt": f"{KEY}\n"})
    monkeypatch.setenv("PRE_COMMIT_REMOTE_NAME", "origin")
    monkeypatch.setenv("PRE_COMMIT_TO_REF", bad)
    assert git_guard.main(["pushed"]) == 1
    assert capsys.readouterr().err == f"{sha12(bad)} f.txt:1: {KEY_RULE}: {KEY_DETAIL}\n" + SUMMARY.format(n=1) + "\n"


def test_main_checked_out_findings(repo: Repo, history: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    repo.write("dirty.txt", "x\n")
    assert git_guard.main(["checked-out", history[0]]) == 1
    assert capsys.readouterr().err == (
        f"HEAD: not the pushed commit: check out {history[0][:12]} or push HEAD\n"
        "working tree: staged or untracked changes: commit or stash them first\n" + SUMMARY.format(n=2) + "\n"
    )


def test_main_git_failure_is_exit_2_with_the_git_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert git_guard.main(["checked-out"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"git-guard: {GIT_TEXT} status --porcelain --untracked-files=normal failed: "
        "fatal: not a git repository (or any of the parent directories): .git\n"
    )


def test_main_bad_ref_is_exit_2(repo: Repo, history: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert git_guard.main(["pushed", history[0], "nosuchtip"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    first, *_ = captured.err.splitlines()
    assert first == (
        f"git-guard: {GIT_TEXT} rev-list {history[0]}..nosuchtip failed: "
        f"fatal: ambiguous argument '{history[0]}..nosuchtip': unknown revision or path not in the working tree."
    )
    assert captured.err.endswith("\n")
    assert "problem(s)" not in captured.err


def test_main_git_failure_decodes_stderr_with_replacement(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(128, ["git", "boom", "arg"], stderr=b"  fatal: \xff bad\n\n")

    monkeypatch.setattr(subprocess, "run", fail)
    assert git_guard.main(["checked-out"]) == 2
    assert capsys.readouterr().err == "git-guard: git boom arg failed: fatal: � bad\n"


def test_main_reads_sys_argv_when_called_without_arguments(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["git_guard.py", "bogus"])
    assert git_guard.main() == 2
    assert capsys.readouterr().err == USAGE + "\n"
    monkeypatch.setattr(sys, "argv", ["git_guard.py", "staged"])
    assert git_guard.main() == 0
    repo.stage({"f.txt": f"{CLAUDE_LINK}\n"})
    assert git_guard.main() == 1
    assert capsys.readouterr().err == f"f.txt:1: {LINK_RULE}: {LINK_DETAIL}\n" + SUMMARY.format(n=1) + "\n"


def test_main_empty_argv_list_is_used_not_sys_argv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["git_guard.py", "staged"])
    assert git_guard.main([]) == 2
    assert capsys.readouterr().err == USAGE + "\n"


# ---- __main__ -------------------------------------------------------------------------------------------------------


def run_as_script(monkeypatch: pytest.MonkeyPatch, *args: str) -> int | str | None:
    monkeypatch.setattr(sys, "argv", ["git_guard.py", *args])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(Path(git_guard.__file__)), run_name="__main__")
    return exc.value.code


def test_dunder_main_exits_with_main_status(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_as_script(monkeypatch, "staged") == 0
    assert capsys.readouterr().err == ""
    repo.stage({"f.txt": f"{CLAUDE_LINK}\n"})
    assert run_as_script(monkeypatch, "staged") == 1
    assert capsys.readouterr().err == f"f.txt:1: {LINK_RULE}: {LINK_DETAIL}\n" + SUMMARY.format(n=1) + "\n"
    assert run_as_script(monkeypatch, "nope") == 2
    assert capsys.readouterr().err == USAGE + "\n"
