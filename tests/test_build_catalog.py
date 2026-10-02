"""Tests for research/build_catalog.py."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest

from research import build_catalog as bc

Entry = dict[str, Any]


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def make_data(tmp_path: Path, **parts: Any) -> Path:
    """Write a complete (mostly empty) data directory and return it.

    Keyword parts: deep, meta, missing, r2 (round2 overrides), awesome, routes.
    """
    assert set(parts) <= {"deep", "meta", "missing", "r2", "awesome", "routes"}
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    round2: Entry = {
        "categorize": {},
        "verify": {},
        "deep": {},
        "triage": {},
        "catjudge": {"model-router": {"summary": "S"}},
    }
    round2.update(parts.get("r2") or {})
    write_json(data / "deep_results.json", parts.get("deep") or {})
    write_json(data / "gh_meta.json", parts.get("meta") or {})
    write_json(data / "gh_meta_missing.json", parts.get("missing") or {})
    write_json(data / "round2_results.json", round2)
    write_json(data / "awesome_entries.json", parts.get("awesome") or {})
    write_json(data / "routes.json", parts.get("routes") or {"labels": {}, "legend": {}})
    return data


def deep_item(**kw: Any) -> Entry:
    item: Entry = {"exists": True, "isJevRelated": True, "name": "N", "url": "https://github.com/o/n"}
    item.update(kw)
    return item


def entry(**kw: Any) -> Entry:
    e: Entry = {
        "slug": "s",
        "name": "N",
        "url": "https://x.io/n",
        "category": "c",
        "tier": "verified",
        "score": 5,
        "openrouter": "yes",
        "maturity": "beta",
        "stars": 3,
        "what": "w",
        "pros": ["p"],
        "cons": ["k"],
    }
    e.update(kw)
    return e


# ---------- load / gh_key ----------


def test_load_reads_json_from_data_dir(tmp_path: Path) -> None:
    write_json(tmp_path / "x.json", {"é": [1, 2]})
    assert bc.load(tmp_path, "x.json") == {"é": [1, 2]}


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        bc.load(tmp_path, "nope.json")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (None, ""),
        ("", ""),
        (0, ""),
        ("  HTTPS://WWW.GitHub.com/Foo/Bar.git?x=1#frag  ", "gh:foo/bar"),
        ("http://github.com/Foo/Bar/", "gh:foo/bar"),
        ("github.com/foo/bar", "gh:foo/bar"),
        ("https://github.com/foo/bar/tree/main/src", "gh:foo/bar"),
        ("https://github.com/foo/bar.git/", "gh:foo/bar"),
        ("https://github.com/foo/bar#readme?x", "gh:foo/bar"),
        ("https://github.com/foo/bar?tab=x#y", "gh:foo/bar"),
        ("https://github.com/foo", "github.com/foo"),
        ("https://github.com/", "github.com"),
        ("https://gitlab.com/Foo/Bar.git", "gitlab.com/foo/bar"),
        ("ftp://www.example.com/a?b", "ftp://www.example.com/a"),
        ("https://example.com/a#b?c", "example.com/a"),
        ("https://example.com/a.git.git", "example.com/a.git"),
        ("https://xgithub.com/a/b", "xgithub.com/a/b"),
        ("https://example.com/www.x", "example.com/www.x"),
        ("https://example.com/http://y", "example.com/http://y"),
        ("https://example.com/a.gitx", "example.com/a.gitx"),
        ("https://example.com/agit", "example.com/agit"),
    ],
)
def test_gh_key(url: str | None, expected: str) -> None:
    assert bc.gh_key(url) == expected


def test_gh_key_distinguishes_owner_and_repo() -> None:
    assert bc.gh_key("github.com/owner/repo/extra") == "gh:owner/repo"


# ---------- GitHubIndex ----------


def test_github_index_canon_lowercases_and_keeps_none() -> None:
    gh = bc.GitHubIndex({"gh:a/b": {"nameWithOwner": "New/Name"}, "gh:c/d": None})
    assert gh.canon == {"gh:a/b": "new/name", "gh:c/d": None}


def test_canon_key_follows_rename_and_falls_back() -> None:
    gh = bc.GitHubIndex({"gh:a/b": {"nameWithOwner": "New/Name"}, "gh:c/d": None})
    assert gh.canon_key("https://github.com/A/B") == "gh:new/name"
    assert gh.canon_key("https://github.com/c/d") == "gh:c/d"
    assert gh.canon_key("https://github.com/e/f") == "gh:e/f"
    assert gh.canon_key("https://example.com/x/") == "example.com/x"


def test_meta_for_direct_hit() -> None:
    m = {"nameWithOwner": "a/b"}
    assert bc.GitHubIndex({"gh:a/b": m}).meta_for("https://github.com/a/b") is m


def test_meta_for_prefers_direct_key_over_same_named_duplicate() -> None:
    first, second = {"nameWithOwner": "x/y", "n": 1}, {"nameWithOwner": "x/y", "n": 2}
    gh = bc.GitHubIndex({"gh:c/d": first, "gh:a/b": second})
    assert gh.meta_for("https://github.com/a/b") is second
    assert gh.meta_for("https://github.com/c/d") is first


def test_meta_for_renamed_repo_found_by_scan() -> None:
    old = {"nameWithOwner": "New/Name"}
    gh = bc.GitHubIndex({"gh:old/name": old})
    assert gh.meta_for("https://github.com/NEW/name") is old


def test_meta_for_skips_none_values_in_scan() -> None:
    m = {"nameWithOwner": "A/B"}
    gh = bc.GitHubIndex({"gh:a/b": None, "gh:z/z": None, "gh:old/b": m})
    assert gh.meta_for("https://github.com/a/b") is m


def test_meta_for_unknown_returns_none() -> None:
    gh = bc.GitHubIndex({"gh:a/b": {"nameWithOwner": "a/b"}, "gh:c/d": None})
    assert gh.meta_for("https://github.com/x/y") is None
    assert bc.GitHubIndex({}).meta_for("https://github.com/x/y") is None


# ---------- index_items / judge_index ----------


def test_index_items_flattens_batches() -> None:
    groups = {
        "b1": {"items": [{"slug": "a", "v": 1}, {"slug": "b", "v": 2}]},
        "b2": None,
        "b3": {},
        "b4": {"items": [{"slug": "c", "v": 3}, {"slug": "a", "v": 4}]},
    }
    assert bc.index_items(groups) == {
        "a": {"slug": "a", "v": 4},
        "b": {"slug": "b", "v": 2},
        "c": {"slug": "c", "v": 3},
    }
    assert bc.index_items({}) == {}


def test_judge_index_builds_meta_and_ranking() -> None:
    gh = bc.GitHubIndex({"gh:a/b": {"nameWithOwner": "New/Repo"}})
    catjudge = {
        "empty": None,
        "blank": {},
        "cat1": {
            "summary": "S",
            "worthIt": "yes",
            "bestPick": {"name": "x"},
            "ranking": [
                {
                    "url": "https://github.com/a/b",
                    "score": 9,
                    "why": "w",
                    "pros": ["p"],
                    "cons": ["c"],
                    "verified": True,
                },
                {"url": "https://github.com/c/d"},
            ],
        },
        "cat2": {"summary": "T", "ranking": None},
    }
    cat_meta, judge_rank = bc.judge_index(catjudge, gh)
    keys = dict.fromkeys(bc.CAT_META_KEYS)
    assert cat_meta == {
        "cat1": {**keys, "summary": "S", "worthIt": "yes", "bestPick": {"name": "x"}},
        "cat2": {**keys, "summary": "T"},
    }
    assert list(cat_meta["cat1"]) == list(bc.CAT_META_KEYS)
    assert judge_rank == {
        "gh:new/repo": {
            "category": "cat1",
            "rank": 1,
            "score": 9,
            "why": "w",
            "pros": ["p"],
            "cons": ["c"],
            "verified": True,
        },
        "gh:c/d": {
            "category": "cat1",
            "rank": 2,
            "score": None,
            "why": None,
            "pros": None,
            "cons": None,
            "verified": None,
        },
    }


def test_judge_index_ranking_without_url_uses_empty_key() -> None:
    _, judge_rank = bc.judge_index({"c": {"ranking": [{"score": 1}]}}, bc.GitHubIndex({}))
    assert judge_rank[""]["rank"] == 1


# ---------- apply_verify ----------


def test_apply_verify_without_verdict_returns_entry_untouched() -> None:
    e = {"pros": ["p"], "cons": [], "score": 1, "openrouter": None, "tier": "source-audited"}
    snapshot = json.loads(json.dumps(e))
    assert bc.apply_verify(e, None) is e
    assert bc.apply_verify(e, {}) is e
    assert e == snapshot


def test_apply_verify_applies_corrections() -> None:
    e = {"pros": ["a", "b", "c"], "cons": ["x", "y"], "score": 4, "openrouter": "no", "tier": "source-audited"}
    v = {
        "refutedPros": ["b"],
        "refutedCons": ["x"],
        "addedPros": ["d"],
        "addedCons": ["z", "w"],
        "adjustedScore": 6.5,
        "openrouterSupportVerified": "yes",
        "confidence": "high",
        "corrections": [1, 2, 3],
        "note": "n",
    }
    out = bc.apply_verify(e, v)
    assert out is e
    assert e == {
        "pros": ["a", "c", "d"],
        "cons": ["y", "z", "w"],
        "score": 6.5,
        "openrouter": "yes",
        "tier": "verified",
        "verification": {"confidence": "high", "corrections": 3, "note": "n"},
    }


def test_apply_verify_sparse_verdict_keeps_existing_values() -> None:
    e = {"pros": ["a"], "cons": ["b"], "score": 4, "openrouter": "no", "tier": "readme-triaged"}
    bc.apply_verify(e, {"adjustedScore": "high", "refutedPros": None, "corrections": None, "x": 1})
    assert e == {
        "pros": ["a"],
        "cons": ["b"],
        "score": 4,
        "openrouter": "no",
        "tier": "verified",
        "verification": {"confidence": None, "corrections": 0, "note": None},
    }


def test_apply_verify_accepts_integer_zero_score() -> None:
    e = {"pros": [], "cons": [], "score": 4, "openrouter": None, "tier": "x"}
    bc.apply_verify(e, {"adjustedScore": 0})
    assert e["score"] == 0


# ---------- from_deep / from_triage / exclusion ----------


def test_from_deep_maps_all_fields() -> None:
    d = {
        "name": "N",
        "url": "https://github.com/o/n",
        "kind": "mcp",
        "score": 7,
        "providerSupport": {"openrouter": "yes"},
        "maturity": "beta",
        "dailyCodingFit": "good",
        "whatItDoes": "does",
        "interface": [{"name": f"t{i}"} for i in range(25)],
        "autoInvocation": "hook",
        "security": [f"s{i}" for i in range(9)],
        "dataEgress": "none",
        "failureMode": "open",
        "contextCost": "low",
        "pros": ("p1", "p2"),
        "cons": ["c1"],
        "verdict": "v",
        "license": "MIT",
        "stars": 12,
        "createdAt": "2025-01-02T03:04:05Z",
        "lastCommit": "2025-06-07T08:09:10Z",
    }
    e = bc.from_deep(d, "slug", "cat", "source-audited")
    assert e == {
        "slug": "slug",
        "name": "N",
        "url": "https://github.com/o/n",
        "category": "cat",
        "kind": "mcp",
        "tier": "source-audited",
        "score": 7,
        "openrouter": "yes",
        "maturity": "beta",
        "fit": "good",
        "what": "does",
        "interface": [f"t{i}" for i in range(20)],
        "autoInvocation": "hook",
        "security": [f"s{i}" for i in range(6)],
        "dataEgress": "none",
        "failureMode": "open",
        "contextCost": "low",
        "pros": ["p1", "p2"],
        "cons": ["c1"],
        "verdict": "v",
        "license": "MIT",
        "stars": 12,
        "created": "2025-01-02",
        "pushed": "2025-06-07",
    }
    assert list(e)[:4] == ["slug", "name", "url", "category"]


def test_from_deep_empty_input() -> None:
    assert bc.from_deep({}, "s", None, "verified") == {
        "slug": "s",
        "name": None,
        "url": None,
        "category": None,
        "kind": None,
        "tier": "verified",
        "score": None,
        "openrouter": None,
        "maturity": None,
        "fit": None,
        "what": None,
        "interface": [],
        "autoInvocation": None,
        "security": [],
        "dataEgress": None,
        "failureMode": None,
        "contextCost": None,
        "pros": [],
        "cons": [],
        "verdict": None,
        "license": None,
        "stars": None,
        "created": "",
        "pushed": "",
    }


def test_from_deep_null_provider_support_and_lists() -> None:
    e = bc.from_deep({"providerSupport": None, "interface": None, "security": None, "pros": None}, "s", "c", "t")
    assert e["openrouter"] is None
    assert e["interface"] == []
    assert e["security"] == []
    assert e["pros"] == []


def test_from_triage_maps_all_fields() -> None:
    t = {
        "name": "N",
        "url": "https://github.com/o/n",
        "category": "cat",
        "kind": "skill",
        "score": 3,
        "openrouter": "no",
        "maturity": "alpha",
        "what": "w",
        "interface": " a , b;c;; ,d ;" + ";".join(f"x{i}" for i in range(25)),
        "autoInvocation": "none",
        "pros": ["p"],
        "cons": ["c"],
        "stars": 4,
        "verdict": "ignored",
    }
    e = bc.from_triage(t, "slug")
    assert e == {
        "slug": "slug",
        "name": "N",
        "url": "https://github.com/o/n",
        "category": "cat",
        "kind": "skill",
        "tier": "readme-triaged",
        "score": 3,
        "openrouter": "no",
        "maturity": "alpha",
        "fit": None,
        "what": "w",
        "interface": ["a", "b", "c", "d"] + [f"x{i}" for i in range(16)],
        "autoInvocation": "none",
        "security": [],
        "pros": ["p"],
        "cons": ["c"],
        "verdict": None,
        "stars": 4,
    }
    assert list(e)[:4] == ["slug", "name", "url", "category"]


def test_from_triage_empty_input() -> None:
    e = bc.from_triage({}, "s")
    assert e["interface"] == []
    assert e["pros"] == []
    assert e["cons"] == []
    assert e["tier"] == "readme-triaged"
    assert e["name"] is None


def test_exclusion() -> None:
    assert bc.exclusion({"exists": True, "isJevRelated": True, "name": "n", "url": "u"}) is None
    assert bc.exclusion({"exists": True, "isJevRelated": False, "name": "n", "url": "u"}) == {
        "name": "n",
        "url": "u",
        "reason": "not Jev-related",
    }
    assert bc.exclusion({"exists": False, "isJevRelated": True, "name": "n", "url": "u"}) == {
        "name": "n",
        "url": "u",
        "reason": "nonexistent",
    }
    assert bc.exclusion({}) == {"name": None, "url": None, "reason": "nonexistent"}


# ---------- Catalog ----------


def new_catalog(meta: Entry | None = None, judge_rank: Entry | None = None) -> bc.Catalog:
    return bc.Catalog(bc.GitHubIndex(meta or {}), judge_rank or {})


def test_put_without_meta_or_judge_stores_entry_unchanged() -> None:
    cat = new_catalog()
    e = entry(url="https://github.com/o/n")
    expected = dict(e)
    cat.put(e)
    assert cat.entries == {"gh:o/n": expected}
    assert cat.entries["gh:o/n"] is e


def test_put_merges_github_metadata() -> None:
    meta = {
        "gh:o/n": {
            "nameWithOwner": "O/N",
            "stargazerCount": 99,
            "createdAt": "2020-01-02T03:04:05Z",
            "pushedAt": "2021-02-03T04:05:06Z",
            "licenseInfo": {"spdxId": "MIT"},
            "url": "https://github.com/O/N",
            "isArchived": True,
            "isFork": False,
        }
    }
    cat = new_catalog(meta)
    e = entry(url="https://github.com/o/n", license="GPL")
    cat.put(e)
    assert e["stars"] == 99
    assert e["created"] == "2020-01-02"
    assert e["pushed"] == "2021-02-03"
    assert e["license"] == "MIT"
    assert e["url"] == "https://github.com/O/N"
    assert e["archived"] is True
    assert e["fork"] is False


@pytest.mark.parametrize("license_info", [None, {}, {"spdxId": None}, {"spdxId": ""}])
def test_put_keeps_entry_license_when_meta_has_none(license_info: Entry | None) -> None:
    meta = {"gh:o/n": {"nameWithOwner": "o/n", "licenseInfo": license_info}}
    cat = new_catalog(meta)
    e = entry(url="https://github.com/o/n", license="GPL")
    cat.put(e)
    assert e["license"] == "GPL"
    e2 = entry(url="https://github.com/o/n")
    cat.put(e2)
    assert e2["license"] is None


def test_put_sparse_meta_falls_back() -> None:
    cat = new_catalog({"gh:o/n": {"nameWithOwner": "o/n"}})
    e = entry(url="https://github.com/o/n", stars=5)
    cat.put(e)
    assert e["stars"] is None
    assert e["created"] == ""
    assert e["pushed"] == ""
    assert e["url"] == "https://github.com/o/n"
    assert e["archived"] is None
    assert e["fork"] is None


def test_put_applies_judge_ranking() -> None:
    jr = {"category": "judged", "rank": 1, "score": 9.5}
    cat = new_catalog(judge_rank={"gh:o/n": jr})
    e = entry(url="https://github.com/o/n", score=2)
    cat.put(e)
    assert e["judge"] is jr
    assert e["score"] == 9.5
    assert e["category"] == "judged"


def test_put_judge_without_numeric_score_keeps_score() -> None:
    cat = new_catalog(judge_rank={"gh:o/n": {"category": "judged", "score": "n/a"}})
    e = entry(url="https://github.com/o/n", score=2)
    cat.put(e)
    assert e["score"] == 2
    assert e["category"] == "judged"
    cat2 = new_catalog(judge_rank={"gh:o/n": {"category": "judged", "score": None}})
    e2 = entry(url="https://github.com/o/n", score=3)
    cat2.put(e2)
    assert e2["score"] == 3


def test_put_judge_zero_score_is_applied() -> None:
    cat = new_catalog(judge_rank={"gh:o/n": {"category": "j", "score": 0}})
    e = entry(url="https://github.com/o/n", score=2)
    cat.put(e)
    assert e["score"] == 0


def test_put_judge_matched_through_rename() -> None:
    meta = {"gh:old/n": {"nameWithOwner": "New/N"}}
    cat = new_catalog(meta, {"gh:new/n": {"category": "j", "score": 1}})
    e = entry(url="https://github.com/old/n")
    cat.put(e)
    assert e["category"] == "j"
    assert list(cat.entries) == ["gh:new/n"]


def test_put_higher_tier_replaces_and_records_alias() -> None:
    cat = new_catalog()
    low = entry(slug="low", tier="source-audited", score=9, url="https://github.com/o/n")
    high = entry(slug="high", tier="verified", score=1, url="https://github.com/O/N/")
    cat.put(low)
    cat.put(high)
    assert cat.entries == {"gh:o/n": high}
    assert high["aliases"] == ["https://github.com/o/n"]
    assert "aliases" not in low


def test_put_lower_tier_loses_and_is_aliased() -> None:
    cat = new_catalog()
    high = entry(slug="high", tier="verified", score=1, url="https://github.com/o/n")
    low = entry(slug="low", tier="source-audited", score=9, url="https://github.com/o/n.git")
    cat.put(high)
    cat.put(low)
    assert cat.entries == {"gh:o/n": high}
    assert high["aliases"] == ["https://github.com/o/n.git"]
    assert "aliases" not in low


def test_put_same_tier_higher_score_wins() -> None:
    cat = new_catalog()
    a = entry(slug="a", score=3, url="https://github.com/o/n")
    b = entry(slug="b", score=4, url="https://github.com/o/n?b")
    cat.put(a)
    cat.put(b)
    assert cat.entries["gh:o/n"] is b
    assert b["aliases"] == ["https://github.com/o/n"]


def test_put_same_tier_equal_score_keeps_first() -> None:
    cat = new_catalog()
    a = entry(slug="a", score=3, url="https://github.com/o/n")
    b = entry(slug="b", score=3, url="https://github.com/o/n?b")
    cat.put(a)
    cat.put(b)
    assert cat.entries["gh:o/n"] is a
    assert a["aliases"] == ["https://github.com/o/n?b"]


def test_put_aliases_accumulate_across_collisions() -> None:
    cat = new_catalog()
    first = entry(slug="1", score=5, url="https://github.com/o/n")
    second = entry(slug="2", score=1, url="https://github.com/o/n#2")
    third = entry(slug="3", score=1, url="https://github.com/o/n#3")
    fourth = entry(slug="4", score=8, url="https://github.com/o/n#4")
    for e in (first, second, third, fourth):
        cat.put(e)
    assert first["aliases"] == ["https://github.com/o/n#2", "https://github.com/o/n#3"]
    assert fourth["aliases"] == ["https://github.com/o/n"]
    assert cat.entries == {"gh:o/n": fourth}


def test_put_distinct_repos_are_separate() -> None:
    cat = new_catalog()
    cat.put(entry(url="https://github.com/o/a"))
    cat.put(entry(url="https://github.com/o/b"))
    assert list(cat.entries) == ["gh:o/a", "gh:o/b"]


@pytest.mark.parametrize(
    ("new", "old", "expected"),
    [
        (("verified", 1), ("source-audited", 9), True),
        (("source-audited", 1), ("readme-triaged", 9), True),
        (("readme-triaged", 1), ("listed-only", 9), True),
        (("source-audited", 9), ("verified", 1), False),
        (("readme-triaged", 9), ("source-audited", 1), False),
        (("listed-only", 9), ("readme-triaged", 1), False),
        (("verified", 2), ("verified", 1), True),
        (("verified", 1), ("verified", 2), False),
        (("verified", 2), ("verified", 2), False),
        (("verified", 1), ("verified", None), True),
        (("verified", None), ("verified", 1), False),
        (("verified", None), ("verified", None), False),
        (("verified", 0), ("verified", None), False),
        (("verified", 0.5), ("verified", 0), True),
    ],
)
def test_beats(new: tuple[str, Any], old: tuple[str, Any], expected: bool) -> None:
    assert bc.Catalog._beats({"tier": new[0], "score": new[1]}, {"tier": old[0], "score": old[1]}) is expected


def test_beats_missing_score_key_counts_as_zero() -> None:
    assert bc.Catalog._beats({"tier": "verified", "score": 1}, {"tier": "verified"}) is True
    assert bc.Catalog._beats({"tier": "verified"}, {"tier": "verified", "score": 1}) is False


# ---------- unanalysed ----------


def test_unanalysed_lists_uncovered_repos() -> None:
    cat = new_catalog()
    cat.put(entry(url="https://github.com/Seen/Repo"))
    awesome = {
        "seen/repo": [["x", "Sec", "t"]],
        "new/repo": [("1", "Zed", "first   text\n with\tspaces"), ("2", "Alpha", "other"), ("3", "Zed", "again")],
        "long/one": [("1", "S", "w" * 400)],
    }
    assert bc.unanalysed(awesome, cat) == [
        {
            "name": "new/repo",
            "url": "https://github.com/new/repo",
            "category": "unanalyzed",
            "tier": "listed-only",
            "sections": ["Alpha", "Zed"],
            "what": "first text with spaces",
        },
        {
            "name": "long/one",
            "url": "https://github.com/long/one",
            "category": "unanalyzed",
            "tier": "listed-only",
            "sections": ["S"],
            "what": "w" * 300,
        },
    ]
    assert list(bc.unanalysed(awesome, cat)[0]) == ["name", "url", "category", "tier", "sections", "what"]


def test_unanalysed_empty() -> None:
    assert bc.unanalysed({}, new_catalog()) == []


def test_unanalysed_skips_repo_whose_literal_key_is_analysed() -> None:
    """The repo is renamed (canon key differs) but another entry already sits on its literal key."""
    meta = {"gh:old/x": {"nameWithOwner": "new/x"}, "gh:other/y": {"nameWithOwner": "old/x"}}
    cat = new_catalog(meta)
    cat.put(entry(url="https://github.com/other/y"))
    assert list(cat.entries) == ["gh:old/x"]
    assert bc.unanalysed({"old/x": [("1", "S", "w")]}, cat) == []


def test_unanalysed_follows_renames() -> None:
    cat = new_catalog({"gh:old/x": {"nameWithOwner": "new/x"}})
    cat.put(entry(url="https://github.com/new/x"))
    assert bc.unanalysed({"old/x": [("1", "S", "w")]}, cat) == []


# ---------- build_catalog ----------


def test_build_catalog_round_one_audits(tmp_path: Path) -> None:
    deep = {
        "a": deep_item(name="A", url="https://github.com/o/a", score=5, pros=["p1", "p2"], cons=["c1"]),
        "gone": {"exists": False, "name": "Gone", "url": "https://github.com/o/gone"},
        "off": {"exists": True, "isJevRelated": False, "name": "Off", "url": "https://github.com/o/off"},
        "nocat": deep_item(name="NC", url="https://github.com/o/nc", score=1),
        "blankcat": deep_item(name="BC", url="https://github.com/o/bc", score=1),
        "unrelated_flag": {"exists": True, "name": "NoFlag", "url": "https://github.com/o/nf"},
    }
    r2 = {
        "categorize": {
            "batch": {"items": [{"slug": "a", "category": "task-mcp"}, {"slug": "blankcat", "category": None}]}
        },
        "verify": {
            "a": {"refutedPros": ["p2"], "addedPros": ["p3"], "adjustedScore": 8, "confidence": "high", "note": "n"},
            "off": {"confidence": "ignored"},
            "nocat": None,
            "gone": {},
        },
    }
    result = bc.build_catalog(make_data(tmp_path, deep=deep, r2=r2))
    assert list(result) == ["generated", "categories", "entries", "listedOnly", "excluded"]
    assert result["generated"] == "2026-09-30"
    assert result["listedOnly"] == []
    assert result["excluded"] == [
        {"name": "Gone", "url": "https://github.com/o/gone", "reason": "nonexistent"},
        {"name": "Off", "url": "https://github.com/o/off", "reason": "not Jev-related"},
        {"name": "NoFlag", "url": "https://github.com/o/nf", "reason": "not Jev-related"},
    ]
    by_slug = {e["slug"]: e for e in result["entries"]}
    assert set(by_slug) == {"a", "nocat", "blankcat"}
    a = by_slug["a"]
    assert (a["category"], a["tier"], a["score"], a["pros"], a["cons"]) == (
        "task-mcp",
        "verified",
        8,
        ["p1", "p3"],
        ["c1"],
    )
    assert a["verification"] == {"confidence": "high", "corrections": 0, "note": "n"}
    assert (by_slug["nocat"]["category"], by_slug["nocat"]["tier"]) == ("uncategorized", "source-audited")
    assert by_slug["blankcat"]["category"] == "uncategorized"
    assert [e["slug"] for e in result["entries"]] == ["a", "nocat", "blankcat"]


def test_build_catalog_excluded_reasons(tmp_path: Path) -> None:
    deep = {
        "gone": {"exists": False, "isJevRelated": True, "name": "Gone", "url": "u1"},
        "off": {"exists": True, "isJevRelated": False, "name": "Off", "url": "u2"},
    }
    result = bc.build_catalog(make_data(tmp_path, deep=deep))
    assert result["excluded"] == [
        {"name": "Gone", "url": "u1", "reason": "nonexistent"},
        {"name": "Off", "url": "u2", "reason": "not Jev-related"},
    ]
    assert result["entries"] == []


def test_build_catalog_round_two_triage(tmp_path: Path) -> None:
    triage_items = [
        {"slug": "gone", "exists": False, "name": "Gone", "url": "https://github.com/o/gone"},
        {"slug": "off", "exists": True, "isJevRelated": False, "name": "Off", "url": "https://github.com/o/off"},
        deep_item(
            slug="withdeep", name="T1", url="https://github.com/o/t1", category="task-mcp", ccRelevant="yes", score=1
        ),
        deep_item(
            slug="badeep", name="T2", url="https://github.com/o/t2", category="dev-skill", ccRelevant="no", score=2
        ),
        deep_item(
            slug="plain", name="T3", url="https://github.com/o/t3", category="dev-skill", interface="x, y", score=3
        ),
        deep_item(slug="nulldeep", name="T4", url="https://github.com/o/t4", category="dev-skill", score=4),
        deep_item(slug="verified", name="T5", url="https://github.com/o/t5", category="dev-skill", score=5),
    ]
    r2 = {
        "triage": {"b1": {"items": triage_items}},
        "deep": {
            "withdeep": {
                "name": "D1",
                "url": "https://github.com/o/t1",
                "exists": True,
                "isJevRelated": True,
                "score": 6,
            },
            "badeep": {"name": "D2", "url": "https://github.com/o/t2", "exists": True, "isJevRelated": False},
            "nulldeep": None,
        },
        "verify": {"verified": {"adjustedScore": 9}, "plain": None},
    }
    result = bc.build_catalog(make_data(tmp_path, r2=r2))
    assert result["excluded"] == [
        {"name": "Gone", "url": "https://github.com/o/gone", "reason": "nonexistent"},
        {"name": "Off", "url": "https://github.com/o/off", "reason": "not Jev-related"},
    ]
    by_slug = {e["slug"]: e for e in result["entries"]}
    assert set(by_slug) == {"withdeep", "badeep", "plain", "nulldeep", "verified"}
    withdeep = by_slug["withdeep"]
    assert (withdeep["name"], withdeep["tier"], withdeep["category"], withdeep["score"]) == (
        "D1",
        "source-audited",
        "task-mcp",
        6,
    )
    assert withdeep["ccRelevant"] == "yes"
    assert "fit" in withdeep
    assert (by_slug["badeep"]["name"], by_slug["badeep"]["tier"], by_slug["badeep"]["ccRelevant"]) == (
        "T2",
        "readme-triaged",
        "no",
    )
    assert by_slug["plain"]["interface"] == ["x", "y"]
    assert by_slug["plain"]["ccRelevant"] is None
    assert by_slug["nulldeep"]["tier"] == "readme-triaged"
    assert (by_slug["verified"]["tier"], by_slug["verified"]["score"]) == ("verified", 9)


def test_build_catalog_collisions_meta_and_judge(tmp_path: Path) -> None:
    deep = {
        "audited": deep_item(name="Old", url="https://github.com/old/x", score=3, license="GPL"),
        "other": deep_item(name="O", url="https://github.com/o/other", score=2),
    }
    meta = {"gh:old/x": {"nameWithOwner": "New/X", "stargazerCount": 10, "url": "https://github.com/New/X"}}
    missing = {"gh:o/other": {"nameWithOwner": "o/other", "stargazerCount": 77, "licenseInfo": {"spdxId": "MIT"}}}
    r2 = {
        "triage": {
            "b": {"items": [deep_item(slug="tri", name="Tri", url="https://github.com/new/x", category="dev-skill")]}
        },
        "catjudge": {
            "model-router": {"summary": "S"},
            "judged": {"ranking": [{"url": "https://github.com/o/other", "score": 8.5}]},
        },
    }
    result = bc.build_catalog(make_data(tmp_path, deep=deep, meta=meta, missing=missing, r2=r2))
    by_slug = {e["slug"]: e for e in result["entries"]}
    assert set(by_slug) == {"audited", "other"}
    audited = by_slug["audited"]
    assert audited["stars"] == 10
    assert audited["url"] == "https://github.com/New/X"
    assert audited["aliases"] == ["https://github.com/New/X"]
    other = by_slug["other"]
    assert (other["stars"], other["license"], other["score"], other["category"]) == (77, "MIT", 8.5, "judged")
    assert other["judge"]["rank"] == 1
    assert result["categories"]["judged"]["summary"] is None
    assert result["categories"]["model-router"]["summary"] == "S"


def test_build_catalog_missing_meta_overrides_gh_meta(tmp_path: Path) -> None:
    deep = {"a": deep_item(url="https://github.com/o/a")}
    meta = {"gh:o/a": {"nameWithOwner": "o/a", "stargazerCount": 1}}
    missing = {"gh:o/a": {"nameWithOwner": "o/a", "stargazerCount": 2}}
    result = bc.build_catalog(make_data(tmp_path, deep=deep, meta=meta, missing=missing))
    assert result["entries"][0]["stars"] == 2


def test_build_catalog_routes_legend_and_listed_only(tmp_path: Path) -> None:
    deep = {
        "r": deep_item(url="https://github.com/o/r"),
        "n": deep_item(url="https://github.com/o/n"),
    }
    awesome = {"o/r": [("1", "Sec", "analysed")], "z/z": [("1", "Sec", "listed")]}
    routes = {"labels": {"r": "claude", "unknown": "other"}, "legend": {"claude": "Claude only"}}
    result = bc.build_catalog(make_data(tmp_path, deep=deep, awesome=awesome, routes=routes))
    by_slug = {e["slug"]: e for e in result["entries"]}
    assert by_slug["r"]["routes"] == "claude"
    assert "routes" not in by_slug["n"]
    assert result["categories"]["model-router"]["routesLegend"] == {"claude": "Claude only"}
    assert [x["name"] for x in result["listedOnly"]] == ["z/z"]


def test_build_catalog_sort_order(tmp_path: Path) -> None:
    def tri(slug: str, category: str | None, score: int | None, stars: int | None) -> Entry:
        return deep_item(slug=slug, url=f"https://x.io/{slug}", category=category, score=score, stars=stars)

    items = [
        tri("zero_stars", "a", 3, 0),
        tri("none_stars", "a", 3, None),
        tri("none_score", "a", None, 1),
        tri("zero_score", "a", 0, 5),
        tri("low_stars", "a", 5, 1),
        tri("high_stars", "a", 5, 9),
        tri("top_score", "a", 7, 0),
        tri("zzz", "zzz", 9, 9),
        tri("no_cat", None, 9, 9),
        tri("zy", "zy", 1, 1),
    ]
    result = bc.build_catalog(make_data(tmp_path, r2={"triage": {"b": {"items": items}}}))
    assert [e["slug"] for e in result["entries"]] == [
        "top_score",
        "high_stars",
        "low_stars",
        "zero_stars",
        "none_stars",
        "zero_score",
        "none_score",
        "zy",
        "no_cat",
        "zzz",
    ]


def test_build_catalog_requires_model_router_judge(tmp_path: Path) -> None:
    data = make_data(tmp_path, r2={"catjudge": {}})
    with pytest.raises(KeyError, match="model-router"):
        bc.build_catalog(data)


# ---------- markdown helpers ----------


def test_cell_collapses_whitespace_and_escapes_pipes() -> None:
    assert bc.cell("a  b\n\tc | d") == "a b c / d"
    assert bc.cell("  x  ") == " x "


@pytest.mark.parametrize("value", [None, "", 0, False])
def test_cell_falsy_is_empty(value: bc.Json) -> None:
    assert bc.cell(value) == ""


def test_cell_stringifies_non_strings() -> None:
    assert bc.cell(12) == "12"
    assert bc.cell(["a", "b"]) == "['a', 'b']"


def test_cell_truncation_boundaries() -> None:
    assert bc.cell("x" * 200) == "x" * 200
    assert bc.cell("x" * 201) == "x" * 199 + "…"
    assert bc.cell("x" * 5, 5) == "x" * 5
    assert bc.cell("x" * 6, 5) == "xxxx…"
    assert bc.cell("a b  c d", 4) == "a b…"


def test_bullets() -> None:
    assert bc.bullets(None) == "-"
    assert bc.bullets([]) == "-"
    assert bc.bullets(["a", "b"]) == "• a<br>• b"
    assert bc.bullets(["a", "b", "c", "d"]) == "• a<br>• b<br>• c"
    assert bc.bullets(["a", "b", "c", "d"], n=2) == "• a<br>• b"
    assert bc.bullets(["x" * 181, "y" * 180]) == "• " + "x" * 179 + "…<br>• " + "y" * 180
    assert bc.bullets(["x" * 10], w=5) == "• xxxx…"
    assert bc.bullets(["a|b", 7, None]) == "• a/b<br>• 7<br>• "


def test_by_category_groups_in_order() -> None:
    entries: list[Entry] = [
        {"category": "a", "n": 1},
        {"category": None, "n": 2},
        {"category": "b", "n": 3},
        {"category": "a", "n": 4},
        {"category": "", "n": 5},
    ]
    result = bc.by_category(entries)
    assert dict(result) == {
        "a": [{"category": "a", "n": 1}, {"category": "a", "n": 4}],
        "uncategorized": [{"category": None, "n": 2}, {"category": "", "n": 5}],
        "b": [{"category": "b", "n": 3}],
    }
    assert list(result) == ["a", "uncategorized", "b"]
    assert bc.by_category([]) == {}


def test_entry_row_full() -> None:
    e = entry(
        name="N" * 61,
        url="https://x.io/n",
        what="W" * 221,
        pros=["a", "b", "c", "d"],
        cons=["k"],
        score=7.5,
        stars=12,
    )
    assert bc.entry_row(3, e) == (
        "| 3 | [" + "N" * 59 + "…](https://x.io/n) | V | 7.5 | yes | beta | 12 | " + "W" * 219 + "… | "
        "• a<br>• b<br>• c | • k |"
    )


def test_entry_row_missing_values() -> None:
    e = {"name": "N", "url": "u", "tier": "readme-triaged", "score": None, "stars": None}
    assert bc.entry_row(1, e) == "| 1 | [N](u) | R | None | ? | ? | ? |  | - | - |"
    assert bc.entry_row(2, {"name": "N", "url": "u", "tier": "source-audited"}) == (
        "| 2 | [N](u) | S | None | ? | ? | ? |  | - | - |"
    )


def test_entry_row_zero_stars_not_unknown() -> None:
    e = entry(stars=0, score=0, openrouter="", maturity=None)
    assert bc.entry_row(1, e) == "| 1 | [N](https://x.io/n) | V | 0 | ? | ? | 0 | w | • p | • k |"


def test_entry_row_unknown_tier_raises() -> None:
    with pytest.raises(KeyError, match="listed-only"):
        bc.entry_row(1, entry(tier="listed-only"))


# ---------- render_markdown / summary ----------


def small_catalog() -> Entry:
    return {
        "entries": [
            entry(slug="1", name="One", category="task-mcp", tier="verified"),
            entry(slug="2", name="Two", category="decision-mcp", tier="source-audited", stars=None),
            entry(slug="3", name="Three", category="decision-mcp", tier="readme-triaged"),
            entry(slug="4", name="Four", category=None, tier="readme-triaged"),
            entry(slug="5", name="Five", category="zeta", tier="readme-triaged"),
            entry(slug="6", name="Six", category="alpha", tier="readme-triaged"),
        ],
        "categories": {
            "task-mcp": {
                "worthIt": "yes",
                "worthItReason": "because  it\nworks | well",
                "bestPick": {"name": "One", "reason": "best | one"},
            },
            "decision-mcp": {"worthIt": None},
            "alpha": {},
            "unused": {"worthIt": "never"},
        },
        "listedOnly": [
            {"name": "z/last", "url": "https://github.com/z/last", "sections": ["B", "A"]},
            {"name": "a/first", "url": "https://github.com/a/first", "sections": ["S|1"]},
        ],
        "excluded": [
            {"name": "gone | one", "url": "https://github.com/o/gone", "reason": "nonexistent"},
            {"name": "off", "url": "https://github.com/o/off", "reason": "not Jev-related"},
        ],
    }


HEADER = [
    "# Jev integration catalog (Claude Code relevance)",
    "",
    "Generated 2026-09-30. 6 analysed entries (1 adversarially verified, 1 source-audited, 4 README-triaged), 2 "
    "listed in community awesome-lists but not analysed (off-target), 2 excluded.",
    "",
    "Score: 0-10 for daily coding in Claude Code with OpenRouter-hosted Jev (verified or category-judge score when "
    "available). OR = OpenRouter support found in code. Tier: V = verified, S = source-audited, R = README-triaged.",
    "",
]
TABLE_HEAD = [
    "",
    "| # | Name | Tier | Score | OR | Maturity | Stars | What | Pros | Cons |",
    "|---|---|---|---|---|---|---|---|---|---|",
]


def row(i: int, name: str, tier: str, stars: str = "3") -> str:
    return f"| {i} | [{name}](https://x.io/n) | {tier} | 5 | yes | beta | {stars} | w | • p | • k |"


def test_render_markdown_full_layout() -> None:
    expected = [
        *HEADER,
        "## decision-mcp (2)",
        "",
        "**Worth it:** None. ",
        "**Best pick:** None: ",
        *TABLE_HEAD,
        row(1, "Two", "S", "?"),
        row(2, "Three", "R"),
        "",
        "## task-mcp (1)",
        "",
        "**Worth it:** yes. because it works / well",
        "**Best pick:** One: best / one",
        *TABLE_HEAD,
        row(1, "One", "V"),
        "",
        "## uncategorized (1)",
        *TABLE_HEAD,
        row(1, "Four", "R"),
        "",
        "## alpha (1)",
        *TABLE_HEAD,
        row(1, "Six", "R"),
        "",
        "## zeta (1)",
        *TABLE_HEAD,
        row(1, "Five", "R"),
        "",
        "## Listed in awesome-lists, not analysed (2)",
        "",
        "Off-target for Claude Code (apps, games, SDK experiments, domain tools) per their list section.",
        "",
        "| Name | List sections |",
        "|---|---|",
        "| [a/first](https://github.com/a/first) | S/1 |",
        "| [z/last](https://github.com/z/last) | B, A |",
        "",
        "## Excluded (2)",
        "",
        "| Name | Reason |",
        "|---|---|",
        "| gone / one (https://github.com/o/gone) | nonexistent |",
        "| off (https://github.com/o/off) | not Jev-related |",
    ]
    assert bc.render_markdown(small_catalog()) == "\n".join(expected) + "\n"


def test_render_markdown_follows_declared_category_order() -> None:
    cat = small_catalog()
    cat["entries"] = [
        entry(category=c) for c in ("other", "browser-os", "uncategorized", "decision-mcp", "model-router")
    ]
    cat["categories"] = {}
    headings = [line for line in bc.render_markdown(cat).splitlines() if line.startswith("## ")]
    assert headings[:5] == [
        "## decision-mcp (1)",
        "## model-router (1)",
        "## browser-os (1)",
        "## other (1)",
        "## uncategorized (1)",
    ]


def test_render_markdown_truncates_long_fields() -> None:
    cat = small_catalog()
    cat["entries"] = [entry(category="task-mcp")]
    cat["categories"] = {
        "task-mcp": {"worthIt": "y", "worthItReason": "r" * 601, "bestPick": {"name": "B", "reason": "b" * 401}}
    }
    cat["listedOnly"] = [{"name": "n", "url": "u", "sections": ["s" * 121]}]
    cat["excluded"] = [{"name": "e" * 81, "url": "u", "reason": "nonexistent"}]
    lines = bc.render_markdown(cat).splitlines()
    assert "**Worth it:** y. " + "r" * 599 + "…" in lines
    assert "**Best pick:** B: " + "b" * 399 + "…" in lines
    assert "| [n](u) | " + "s" * 119 + "… |" in lines
    assert "| " + "e" * 79 + "… (u) | nonexistent |" in lines


def test_render_markdown_without_entries() -> None:
    cat: Entry = {"entries": [], "categories": {}, "listedOnly": [], "excluded": []}
    assert (
        bc.render_markdown(cat)
        == "\n".join(
            [
                *HEADER[:2],
                "Generated 2026-09-30. 0 analysed entries (0 adversarially verified, 0 source-audited, "
                "0 README-triaged), 0 "
                "listed in community awesome-lists but not analysed (off-target), 0 excluded.",
                *HEADER[3:],
                "## Listed in awesome-lists, not analysed (0)",
                "",
                "Off-target for Claude Code (apps, games, SDK experiments, domain tools) per their list section.",
                "",
                "| Name | List sections |",
                "|---|---|",
                "",
                "## Excluded (0)",
                "",
                "| Name | Reason |",
                "|---|---|",
            ]
        )
        + "\n"
    )


def test_summary() -> None:
    cat = small_catalog()
    assert bc.summary(cat) == [
        "entries 6 {'verified': 1, 'source-audited': 1, 'readme-triaged': 4} listed-only 2 excluded 2",
        "by category {'decision-mcp': 2, 'task-mcp': 1, 'uncategorized': 1, 'zeta': 1, 'alpha': 1}",
    ]


def test_summary_empty() -> None:
    assert bc.summary({"entries": [], "listedOnly": [], "excluded": []}) == [
        "entries 0 {} listed-only 0 excluded 0",
        "by category {}",
    ]


# ---------- main ----------


def test_main_writes_outputs_and_prints_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = make_data(tmp_path, deep={"a": deep_item(url="https://github.com/o/a", score=4)})
    out_json, out_md = tmp_path / "o.json", tmp_path / "o.md"
    # Point the defaults into tmp_path too, so a regression can never write into the repository.
    monkeypatch.setattr(bc, "DATA", data)
    monkeypatch.setattr(bc, "HERE", tmp_path)
    assert bc.main([str(out_json), str(out_md)]) == 0
    catalog = bc.build_catalog(data)
    assert out_json.read_text(encoding="utf-8") == json.dumps(catalog, indent=1)
    assert out_md.read_text(encoding="utf-8") == bc.render_markdown(catalog)
    assert capsys.readouterr().out == (
        "entries 1 {'source-audited': 1} listed-only 0 excluded 0\nby category {'uncategorized': 1}\n"
    )


def test_main_defaults_and_sys_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = make_data(tmp_path)
    monkeypatch.setattr(bc, "DATA", data)
    monkeypatch.setattr(bc, "HERE", tmp_path)
    monkeypatch.setattr(sys, "argv", ["build_catalog.py"])
    assert bc.main() == 0
    assert (data / "catalog.json").is_file()
    assert (tmp_path / "JEV-CATALOG.md").is_file()
    assert capsys.readouterr().out.startswith("entries 0 {} listed-only 0 excluded 0\n")


def test_main_reads_sys_argv_and_defaults_markdown_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = make_data(tmp_path)
    out_json = tmp_path / "custom.json"
    monkeypatch.setattr(bc, "DATA", data)
    monkeypatch.setattr(bc, "HERE", tmp_path)
    monkeypatch.setattr(sys, "argv", ["build_catalog.py", str(out_json)])
    assert bc.main() == 0
    assert out_json.is_file()
    assert not (data / "catalog.json").exists()
    assert (tmp_path / "JEV-CATALOG.md").is_file()


def test_main_explicit_empty_argv_ignores_sys_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = make_data(tmp_path)
    monkeypatch.setattr(bc, "DATA", data)
    monkeypatch.setattr(bc, "HERE", tmp_path)
    monkeypatch.setattr(sys, "argv", ["build_catalog.py", str(tmp_path / "ignored.json")])
    assert bc.main([]) == 0
    assert (data / "catalog.json").is_file()
    assert not (tmp_path / "ignored.json").exists()


def test_generated_files_match_committed_outputs(root: Path, tmp_path: Path) -> None:
    out_json, out_md = tmp_path / "catalog.json", tmp_path / "JEV-CATALOG.md"
    assert bc.main([str(out_json), str(out_md)]) == 0
    assert out_json.read_bytes() == (root / "research" / "data" / "catalog.json").read_bytes()
    assert out_md.read_bytes() == (root / "research" / "JEV-CATALOG.md").read_bytes()


def test_script_entry_point(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_json, out_md = tmp_path / "catalog.json", tmp_path / "JEV-CATALOG.md"
    monkeypatch.setattr(sys, "argv", ["build_catalog.py", str(out_json), str(out_md)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(root / "research" / "build_catalog.py"), run_name="__main__")
    assert exc.value.code == 0
    assert out_json.stat().st_size > 0
    assert out_md.stat().st_size > 0
