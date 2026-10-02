"""Tests for research/build_page.py."""

from __future__ import annotations

import json
import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from research import build_page

Site = Callable[[dict[str, Any], str], Path]


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Site:
    """Point build_page.HERE at a tmp tree holding the given catalog and template; return that tree."""

    def make(catalog: dict[str, Any], template: str) -> Path:
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        (tmp_path / "page-template.html").write_text(template, encoding="utf-8")
        monkeypatch.setattr(build_page, "HERE", tmp_path)
        return tmp_path

    return make


def trunc(n: int) -> tuple[str, str]:
    """A value of exactly n chars and its expected result, then a value of n+1 chars and its result."""
    return "x" * n, "x" * (n - 1) + "…"


# ---- clean ----------------------------------------------------------------------------------------------------------


def test_clean_none_stays_none() -> None:
    assert build_page.clean(None) is None
    assert build_page.clean(None, 5) is None


def test_clean_collapses_whitespace_and_strips() -> None:
    assert build_page.clean("  a \t\n b\r\n\n  c  ") == "a b c"
    assert build_page.clean("") == ""
    assert build_page.clean("   ") == ""


def test_clean_stringifies_non_strings() -> None:
    assert build_page.clean(42) == "42"
    assert build_page.clean(0) == "0"
    assert build_page.clean(["a", 1]) == "['a', 1]"


def test_clean_truncates_only_above_limit() -> None:
    assert build_page.clean("abcde", 5) == "abcde"
    assert build_page.clean("abcdef", 5) == "abcd…"
    assert build_page.clean("abcdef", 1) == "…"
    assert build_page.clean("abcdef", 6) == "abcdef"
    assert build_page.clean("a" * 1000) == "a" * 1000


def test_clean_truncates_after_collapsing_and_scrubbing() -> None:
    assert build_page.clean("a   b   c", 5) == "a b c"
    assert build_page.clean("/home/gio/x", 4) == "~/x"
    assert build_page.clean("/home/gio/x", 3) == "~/x"
    assert build_page.clean("/home/gio/xy", 3) == "~/…"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/home/gio/proj", "~/proj"),
        ("a /home/gio b /home/gio", "a ~ b ~"),
        ("/media/nvme/Clienti/acme/x.py rest", "<client root> rest"),
        ("/media/hdd/Clienti/acme", "<client root>"),
        ("/media/nvme/Clienti", "<client root>"),
        ("/media/nvme/x/y.txt more", "<local path> more"),
        ("/media/hdd/x/y.txt", "/media/hdd/x/y.txt"),
        ("/tmp/claude-1000/a/b.log tail", "<scratch path> tail"),  # noqa: S108
        ("/tmp/other/a", "/tmp/other/a"),  # noqa: S108
        ("(/media/nvme/p)", "(<local path>)"),
        ("/media/nvme/p, q", "<local path>, q"),
        ("/media/nvme/p; q", "<local path>; q"),
        ("/media/nvme/p) q", "<local path>) q"),
        ("/tmp/claude-1000/p, q", "<scratch path>, q"),  # noqa: S108
        ("/tmp/claude-1000/p; q", "<scratch path>; q"),  # noqa: S108
        ("/tmp/claude-1000/p) q", "<scratch path>) q"),  # noqa: S108
        ("/media/nvme/Clienti/p, q", "<client root>, q"),
        ("/media/nvme/Clienti/p; q", "<client root>; q"),
        ("/media/nvme/Clienti/p) q", "<client root>) q"),
        ("/media/nvme/Clienti/p\tq", "<client root> q"),
    ],
)
def test_clean_scrubs_paths(raw: str, expected: str) -> None:
    assert build_page.clean(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("mail bob@acme.org now", "mail [email removed] now"),
        ("a.b_c%d+e-f@sub.host-1.io", "[email removed]"),
        ("x@a.b@c.de", "x@[email removed]"),
        ("bob@example.com", "bob@example.com"),
        ("bob@typesafe.ai", "bob@typesafe.ai"),
        ("bob@mail.example.com", "bob@mail.example.com"),
        ("bob@a-b.typesafe.ai", "bob@a-b.typesafe.ai"),
        ("bob@notexample.com", "[email removed]"),
        ("bob@example.community", "[email removed]"),
        ("bob@typesafe.airlines", "[email removed]"),
        ("bob@example.com.evil.org", "bob@example.com.evil.org"),
        ("bob@x.c", "bob@x.c"),
        ("bob@x.co", "[email removed]"),
        ("bob@localhost", "bob@localhost"),
        ("@acme.org", "@acme.org"),
        ("bob@acme.ORG", "bob@acme.ORG"),
    ],
)
def test_clean_scrubs_emails(raw: str, expected: str) -> None:
    assert build_page.clean(raw) == expected


def test_clean_scrubs_in_order() -> None:
    """A client path wins over the generic local-path rule; the home rule runs on what is left."""
    assert (
        build_page.clean("/home/gio and /media/nvme/Clienti/x and /media/nvme/y")
        == "~ and <client root> and <local path>"
    )


# ---- clean_list -----------------------------------------------------------------------------------------------------


def test_clean_list_empty_inputs() -> None:
    assert build_page.clean_list(None) == []
    assert build_page.clean_list([]) == []


def test_clean_list_drops_falsy_and_cleans() -> None:
    assert build_page.clean_list(["  a  b ", "", None, 0, 5]) == ["a b", "5"]


def test_clean_list_default_limits() -> None:
    assert build_page.clean_list(list("abcdefg")) == list("abcde")
    assert build_page.clean_list(["x" * 260]) == ["x" * 260]
    assert build_page.clean_list(["x" * 261]) == ["x" * 259 + "…"]


def test_clean_list_slices_before_filtering() -> None:
    assert build_page.clean_list(["", "", "a", "b"], 3) == ["a"]
    assert build_page.clean_list(["a", "b", "c"], 2) == ["a", "b"]
    assert build_page.clean_list(["a", "b", "c"], 3) == ["a", "b", "c"]
    assert build_page.clean_list(["a", "b", "c"], 0) == []


def test_clean_list_custom_item_limit() -> None:
    assert build_page.clean_list(["abcdef", "abcde"], 5, 5) == ["abcd…", "abcde"]


# ---- slim_entry -----------------------------------------------------------------------------------------------------


def full_entry() -> dict[str, Any]:
    return {
        "name": "  Some   Tool ",
        "url": "https://example.org/x",
        "category": "decision-mcp",
        "tier": "verified",
        "score": 7.5,
        "openrouter": "yes",
        "maturity": "stable",
        "stars": 120,
        "created": "2025-01-02",
        "what": "does /home/gio things",
        "pros": ["p1", "", "p2"],
        "cons": ["c1"],
        "verdict": "good",
        "autoInvocation": "auto",
        "interface": ["i1", "i2"],
        "security": ["s1"],
        "verification": {"note": "checked  by hand"},
        "kind": "library",
    }


def test_slim_entry_full() -> None:
    assert build_page.slim_entry(full_entry()) == {
        "n": "Some Tool",
        "u": "https://example.org/x",
        "c": "decision-mcp",
        "tier": "verified",
        "s": 7.5,
        "o": "yes",
        "m": "stable",
        "st": 120,
        "cr": "2025-01-02",
        "w": "does ~ things",
        "p": ["p1", "p2"],
        "k": ["c1"],
        "v": "good",
        "a": "auto",
        "i": ["i1", "i2"],
        "sec": ["s1"],
        "vn": "checked by hand",
        "mcp": True,
    }


def test_slim_entry_key_order() -> None:
    assert list(build_page.slim_entry(full_entry())) == [
        "n", "u", "c", "tier", "s", "o", "m", "st", "cr", "w", "p", "k", "v", "a", "i", "sec", "vn", "mcp",
    ]  # fmt: skip


def test_slim_entry_defaults() -> None:
    assert build_page.slim_entry({"tier": "other"}) == {
        "n": None,
        "u": None,
        "c": "other",
        "tier": "other",
        "s": None,
        "o": "unknown",
        "m": None,
        "st": None,
        "cr": None,
        "w": None,
        "p": [],
        "k": [],
        "v": None,
        "a": None,
        "i": [],
        "sec": [],
        "vn": None,
        "mcp": False,
    }


def test_slim_entry_requires_tier() -> None:
    with pytest.raises(KeyError, match="tier"):
        build_page.slim_entry({"name": "x"})


def test_slim_entry_falsy_values_use_defaults() -> None:
    e = {"tier": "t", "category": "", "openrouter": "", "created": "", "verification": None}
    out = build_page.slim_entry(e)
    assert (out["c"], out["o"], out["cr"], out["vn"]) == ("other", "unknown", None, None)
    assert build_page.slim_entry({"tier": "t", "verification": {}})["vn"] is None


@pytest.mark.parametrize(
    ("score", "expected"),
    [(3, 3), (0, 0), (2.5, 2.5), (0.0, 0.0), ("3", None), (None, None), ([1], None)],
)
def test_slim_entry_score_only_numbers(score: object, expected: object) -> None:
    assert build_page.slim_entry({"tier": "t", "score": score})["s"] == expected


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"kind": "mcp-server"}, True),
        ({"category": "decision-mcp"}, True),
        ({"category": "task-mcp"}, True),
        ({"kind": "mcp-server", "category": "other"}, True),
        ({"kind": "library", "category": "model-router"}, False),
        ({"kind": "mcp"}, False),
        ({"category": "mcp"}, False),
        ({}, False),
    ],
)
def test_slim_entry_mcp_flag(entry: dict[str, Any], expected: bool) -> None:
    assert build_page.slim_entry({"tier": "t", **entry})["mcp"] is expected


@pytest.mark.parametrize(
    ("field", "key", "limit"),
    [("name", "n", 90), ("what", "w", 320), ("verdict", "v", 400), ("autoInvocation", "a", 260)],
)
def test_slim_entry_text_limits(field: str, key: str, limit: int) -> None:
    ok, cut = trunc(limit)
    assert build_page.slim_entry({"tier": "t", field: ok})[key] == ok
    assert build_page.slim_entry({"tier": "t", field: ok + "x"})[key] == cut


def test_slim_entry_verification_note_limit() -> None:
    ok, cut = trunc(420)
    assert build_page.slim_entry({"tier": "t", "verification": {"note": ok}})["vn"] == ok
    assert build_page.slim_entry({"tier": "t", "verification": {"note": ok + "x"}})["vn"] == cut


@pytest.mark.parametrize(
    ("field", "key", "count", "limit"), [("pros", "p", 5, 260), ("cons", "k", 5, 260), ("security", "sec", 4, 240)]
)
def test_slim_entry_list_limits(field: str, key: str, count: int, limit: int) -> None:
    ok, cut = trunc(limit)
    assert build_page.slim_entry({"tier": "t", field: [ok, ok + "x"]})[key] == [ok, cut]
    items = [str(i) for i in range(count + 2)]
    assert build_page.slim_entry({"tier": "t", field: items})[key] == items[:count]


def test_slim_entry_interface_limits() -> None:
    ok, cut = trunc(60)
    items = [ok, ok + "x", None, "", "  a  b "] + [str(i) for i in range(10)]
    assert build_page.slim_entry({"tier": "t", "interface": items})["i"] == [ok, cut, None, "", "a b"] + [
        str(i) for i in range(7)
    ]
    assert len(build_page.slim_entry({"tier": "t", "interface": [str(i) for i in range(13)]})["i"]) == 12


def test_slim_entry_passes_url_stars_maturity_through_unscrubbed() -> None:
    e = {"tier": "t", "url": "/home/gio/a  b", "maturity": " m ", "stars": 0}
    out = build_page.slim_entry(e)
    assert (out["u"], out["m"], out["st"]) == ("/home/gio/a  b", " m ", 0)


# ---- slim_listed ----------------------------------------------------------------------------------------------------


def test_slim_listed_basic() -> None:
    x = {"name": "N /home/gio", "url": "/home/gio/u", "what": "see [docs](http://a.b) now", "sections": ["A", "B"]}
    assert build_page.slim_listed(x) == {
        "n": "N /home/gio",
        "u": "/home/gio/u",
        "tier": "listed-only",
        "w": "see now",
        "sec": "A, B",
        "p": [],
        "k": [],
        "i": [],
    }
    assert list(build_page.slim_listed(x)) == ["n", "u", "tier", "w", "sec", "p", "k", "i"]


def test_slim_listed_missing_optional_fields() -> None:
    out = build_page.slim_listed({"name": "n", "url": "u"})
    assert out["w"] == ""
    assert out["sec"] == ""
    assert build_page.slim_listed({"name": "n", "url": "u", "what": None, "sections": None})["w"] == ""
    assert build_page.slim_listed({"name": "n", "url": "u", "sections": None})["sec"] == ""


@pytest.mark.parametrize("missing", ["name", "url"])
def test_slim_listed_requires_name_and_url(missing: str) -> None:
    x = {"name": "n", "url": "u"}
    del x[missing]
    with pytest.raises(KeyError, match=missing):
        build_page.slim_listed(x)


@pytest.mark.parametrize(
    ("what", "expected"),
    [
        ("[a](b)", ""),
        ("x [](u) y", "x y"),
        ("x [a]() y", "x y"),
        ("[a](b) and [c](d)", "and"),
        ("[a]b](c) z", "[a]b](c) z"),
        ("[a] (b)", "[a] (b)"),
        ("[a(b)]", "[a(b)]"),
        ("[a](b(c) d", "d"),
    ],
)
def test_slim_listed_strips_markdown_links(what: str, expected: str) -> None:
    assert build_page.slim_listed({"name": "n", "url": "u", "what": what})["w"] == expected


def test_slim_listed_limits() -> None:
    ok, cut = trunc(240)
    assert build_page.slim_listed({"name": "n", "url": "u", "what": ok})["w"] == ok
    assert build_page.slim_listed({"name": "n", "url": "u", "what": ok + "x"})["w"] == cut
    ok, cut = trunc(80)
    assert build_page.slim_listed({"name": "n", "url": "u", "sections": [ok]})["sec"] == ok
    assert build_page.slim_listed({"name": "n", "url": "u", "sections": [ok + "x"]})["sec"] == cut
    assert build_page.slim_listed({"name": "n", "url": "u", "sections": ["a", "b", "c"]})["sec"] == "a, b, c"


# ---- page_data ------------------------------------------------------------------------------------------------------


def sample_catalog() -> dict[str, Any]:
    return {
        "entries": [
            {"name": "a", "tier": "verified", "kind": "mcp-server"},
            {"name": "b", "tier": "source-audited", "category": "task-mcp"},
            {"name": "c", "tier": "verified", "category": "other"},
            {"name": "d", "tier": "listed"},
        ],
        "listedOnly": [{"name": "l", "url": "lu", "what": "w"}],
        "categories": {
            "cat1": {"worthIt": "yes", "bestPick": {"name": "  Best  /home/gio "}},
            "cat2": {"worthIt": None},
            "cat3": {"bestPick": None},
            "cat4": {"bestPick": {}},
            "cat5": {"bestPick": {"name": "y" * 81}},
        },
    }


def test_page_data_shape_and_values() -> None:
    data = build_page.page_data(sample_catalog())
    assert list(data) == ["generated", "stats", "categories", "entries", "listed"]
    assert data["generated"] == "30 Sep 2026"
    assert data["stats"] == {"analysed": 4, "verified": 2, "source": 3, "mcp": 2}
    assert [e["n"] for e in data["entries"]] == ["a", "b", "c", "d"]
    assert data["entries"][0] == build_page.slim_entry(sample_catalog()["entries"][0])
    assert data["listed"] == [build_page.slim_listed(sample_catalog()["listedOnly"][0])]
    assert data["categories"] == {
        "cat1": {"worthIt": "yes", "bestPick": {"name": "Best ~"}},
        "cat2": {"worthIt": None, "bestPick": {"name": None}},
        "cat3": {"worthIt": None, "bestPick": {"name": None}},
        "cat4": {"worthIt": None, "bestPick": {"name": None}},
        "cat5": {"worthIt": None, "bestPick": {"name": "y" * 79 + "…"}},
    }


def test_page_data_empty_catalog() -> None:
    cat: dict[str, Any] = {"entries": [], "listedOnly": [], "categories": {}}
    assert build_page.page_data(cat) == {
        "generated": "30 Sep 2026",
        "stats": {"analysed": 0, "verified": 0, "source": 0, "mcp": 0},
        "categories": {},
        "entries": [],
        "listed": [],
    }


@pytest.mark.parametrize("missing", ["entries", "listedOnly", "categories"])
def test_page_data_requires_all_sections(missing: str) -> None:
    cat: dict[str, Any] = {"entries": [], "listedOnly": [], "categories": {}}
    del cat[missing]
    with pytest.raises(KeyError, match=missing):
        build_page.page_data(cat)


# ---- render_page ----------------------------------------------------------------------------------------------------


def test_render_page_embeds_compact_unescaped_json() -> None:
    out = build_page.render_page("<script>__DATA__</script>", {"a": [1, 2], "b": "é€"})
    assert out == '<script>{"a":[1,2],"b":"é€"}</script>'


def test_render_page_escapes_script_close() -> None:
    out = build_page.render_page("<script>__DATA__</script>", {"x": "</script><b>"})
    assert out == '<script>{"x":"<\\/script><b>"}</script>'
    assert out.count("</script>") == 1


def test_render_page_replaces_every_placeholder() -> None:
    assert build_page.render_page("__DATA__|__DATA__", {"k": 1}) == '{"k":1}|{"k":1}'


def test_render_page_without_placeholder_is_unchanged() -> None:
    assert build_page.render_page("plain", {"k": 1}) == "plain"


# ---- main -----------------------------------------------------------------------------------------------------------


MINI_CATALOG: dict[str, Any] = {
    "entries": [{"name": "é", "tier": "verified", "kind": "mcp-server"}],
    "listedOnly": [{"name": "l", "url": "lu"}],
    "categories": {"c": {"worthIt": "yes"}},
}


def test_main_writes_page_and_reports(site: Site, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    site(MINI_CATALOG, "<é>__DATA__</é>")
    out = tmp_path / "out.html"
    assert build_page.main([str(out)]) == 0
    expected = build_page.render_page("<é>__DATA__</é>", build_page.page_data(MINI_CATALOG))
    assert out.read_bytes() == expected.encode("utf-8")
    stats = {"analysed": 1, "verified": 1, "source": 1, "mcp": 1}
    assert (
        capsys.readouterr().out
        == f"{out} {len(expected.encode())} bytes; entries 1 listed 1 stats {stats} leak-hits 0\n"
    )
    assert len(expected.encode()) > len(expected)


def test_main_counts_leaks_in_template(site: Site, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    template = "/home/gio Clienti sk-or-ABCDEFGH sk-or-ABCDEFG bob@acme.org bob@example.com bob@typesafe.ai __DATA__"
    site(MINI_CATALOG, template)
    out = tmp_path / "out.html"
    assert build_page.main([str(out)]) == 0
    assert capsys.readouterr().out.endswith(" leak-hits 4\n")


def test_main_scrubbed_catalog_values_do_not_leak(
    site: Site, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cat = {
        **MINI_CATALOG,
        "entries": [{"name": "/home/gio/x bob@acme.org", "tier": "t", "what": "/media/nvme/Clienti/z"}],
    }
    site(cat, "__DATA__")
    out = tmp_path / "out.html"
    build_page.main([str(out)])
    assert capsys.readouterr().out.endswith(" leak-hits 0\n")
    page = out.read_text(encoding="utf-8")
    assert "~/x [email removed]" in page
    assert "<client root>" in page


def test_main_default_output_next_to_script(
    site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    here = site(MINI_CATALOG, "T:__DATA__")
    monkeypatch.setattr(sys, "argv", ["build_page.py", "ignored.html"])
    assert build_page.main([]) == 0
    default = here / "jev-in-claude-code.html"
    assert default.read_text(encoding="utf-8").startswith("T:{")
    assert capsys.readouterr().out.startswith(f"{default} ")
    assert not (here / "ignored.html").exists()


def test_main_none_reads_sys_argv(site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site(MINI_CATALOG, "T:__DATA__")
    out = tmp_path / "from-argv.html"
    monkeypatch.setattr(sys, "argv", ["build_page.py", str(out), str(tmp_path / "extra.html")])
    assert build_page.main() == 0
    assert out.read_text(encoding="utf-8").startswith("T:{")
    assert not (tmp_path / "extra.html").exists()


def test_main_none_without_args_uses_default(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    here = site(MINI_CATALOG, "T:__DATA__")
    monkeypatch.setattr(sys, "argv", ["build_page.py"])
    assert build_page.main() == 0
    assert (here / "jev-in-claude-code.html").is_file()


def test_main_explicit_args_ignore_sys_argv(site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site(MINI_CATALOG, "T:__DATA__")
    monkeypatch.setattr(sys, "argv", ["build_page.py", str(tmp_path / "wrong.html")])
    out = tmp_path / "right.html"
    build_page.main([str(out)])
    assert out.is_file()
    assert not (tmp_path / "wrong.html").exists()


def test_main_missing_catalog_raises(site: Site, tmp_path: Path) -> None:
    here = site(MINI_CATALOG, "T")
    (here / "data" / "catalog.json").unlink()
    with pytest.raises(FileNotFoundError, match=r"catalog\.json"):
        build_page.main([str(tmp_path / "o.html")])


def test_main_missing_template_raises(site: Site, tmp_path: Path) -> None:
    here = site(MINI_CATALOG, "T")
    (here / "page-template.html").unlink()
    with pytest.raises(FileNotFoundError, match=r"page-template\.html"):
        build_page.main([str(tmp_path / "o.html")])


def test_main_reproduces_committed_page(root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "page.html"
    assert build_page.main([str(out)]) == 0
    assert out.read_bytes() == (root / "research" / "jev-in-claude-code.html").read_bytes()
    assert capsys.readouterr().out.endswith(" leak-hits 0\n")


def test_script_entry_point(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "script.html"
    monkeypatch.setattr(sys, "argv", ["build_page.py", str(out)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(root / "research" / "build_page.py"), run_name="__main__")
    assert exc.value.code == 0
    assert out.read_bytes() == (root / "research" / "jev-in-claude-code.html").read_bytes()
