#!/usr/bin/env python3
"""Merge round-1 source audits, round-2 triage/deep/verify/category-judge results
into one catalog (data/catalog.json + JEV-CATALOG.md).

Usage: python3 build_catalog.py [CATALOG_JSON [CATALOG_MD]]   (defaults: data/catalog.json, JEV-CATALOG.md)
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
GENERATED = "2026-09-30"
TIER_RANK = {"verified": 3, "source-audited": 2, "readme-triaged": 1, "listed-only": 0}
TIER_LETTER = {"verified": "V", "source-audited": "S", "readme-triaged": "R"}
CAT_META_KEYS = (
    "summary",
    "builtInAlternative",
    "worthIt",
    "worthItReason",
    "bestPick",
    "runnerUp",
    "recommendationForUser",
)
ORDER = [
    "decision-mcp",
    "task-mcp",
    "permission-gate",
    "security-guard",
    "stop-verifier",
    "review-ci",
    "code-search-nav",
    "dev-skill",
    "prompt-steering",
    "context-pruning",
    "compaction-memory",
    "model-router",
    "skill-tool-router",
    "suite-plugin",
    "library-harness",
    "browser-os",
    "eval-observability",
    "other",
    "uncategorized",
]

Entry = dict[str, Any]
Json = str | int | float | list[Any] | dict[str, Any] | None


def load(data_dir: Path, name: str) -> Any:
    path = data_dir / name
    text = path.read_text(encoding="utf-8")  # pragma: no mutate  (utf-8 == locale default: equivalent mutants)
    return json.loads(text)


def gh_key(url: str | None) -> str:
    u = str(url or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = re.sub(r"[?#].*$", "", u)
    u = re.sub(r"/+\Z", "", u)
    u = re.sub(r"\.git$", "", u)
    m = re.match(r"^github\.com/([^/]+)/([^/]+)", u)
    return ("gh:" + m.group(1) + "/" + m.group(2)) if m else u


class GitHubIndex:
    """GitHub metadata keyed by gh_key, with canonical names that follow repo renames."""

    def __init__(self, meta: dict[str, Any]) -> None:
        self.meta = meta
        self.canon = {k: (m["nameWithOwner"].lower() if m else None) for k, m in meta.items()}

    def canon_key(self, url: str | None) -> str:
        k = gh_key(url)
        c = self.canon.get(k)
        return ("gh:" + c) if c else k

    def meta_for(self, url: str | None) -> dict[str, Any] | None:
        m: dict[str, Any] | None = self.meta.get(gh_key(url))
        if m:
            return m
        want = self.canon_key(url)
        for mm in self.meta.values():
            if mm and ("gh:" + mm["nameWithOwner"].lower()) == want:
                found: dict[str, Any] = mm
                return found
        return None


def index_items(groups: dict[str, Any]) -> dict[str, Any]:
    """Flatten {batch: {items: [{slug, ...}]}} into {slug: item}."""
    return {it["slug"]: it for v in groups.values() for it in (v or {}).get("items", [])}


def judge_index(catjudge: dict[str, Any], gh: GitHubIndex) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (per-category judge summary, per-repo judge ranking keyed by canonical key)."""
    cat_meta: dict[str, Any] = {}
    judge_rank: dict[str, Any] = {}
    for cat, j in catjudge.items():
        if not j:
            continue
        cat_meta[cat] = {k: j.get(k) for k in CAT_META_KEYS}
        for i, r in enumerate(j.get("ranking") or [], start=1):
            judge_rank[gh.canon_key(r.get("url"))] = {
                "category": cat,
                "rank": i,
                "score": r.get("score"),
                "why": r.get("why"),
                "pros": r.get("pros"),
                "cons": r.get("cons"),
                "verified": r.get("verified"),
            }
    return cat_meta, judge_rank


def apply_verify(e: Entry, v: dict[str, Any] | None) -> Entry:
    if not v:
        return e
    rp = set(v.get("refutedPros") or [])
    rc = set(v.get("refutedCons") or [])
    e["pros"] = [p for p in e["pros"] if p not in rp] + list(v.get("addedPros") or [])
    e["cons"] = [c for c in e["cons"] if c not in rc] + list(v.get("addedCons") or [])
    if isinstance(v.get("adjustedScore"), (int, float)):
        e["score"] = v["adjustedScore"]
    e["openrouter"] = v.get("openrouterSupportVerified") or e["openrouter"]
    e["tier"] = "verified"
    e["verification"] = {
        "confidence": v.get("confidence"),
        "corrections": len(v.get("corrections") or []),
        "note": v.get("note"),
    }
    return e


def from_deep(d: dict[str, Any], slug: str, category: str | None, tier: str) -> Entry:
    ps = d.get("providerSupport") or {}
    return {
        "slug": slug,
        "name": d.get("name"),
        "url": d.get("url"),
        "category": category,
        "kind": d.get("kind"),
        "tier": tier,
        "score": d.get("score"),
        "openrouter": ps.get("openrouter"),
        "maturity": d.get("maturity"),
        "fit": d.get("dailyCodingFit"),
        "what": d.get("whatItDoes"),
        "interface": [i.get("name") for i in (d.get("interface") or [])][:20],
        "autoInvocation": d.get("autoInvocation"),
        "security": (d.get("security") or [])[:6],
        "dataEgress": d.get("dataEgress"),
        "failureMode": d.get("failureMode"),
        "contextCost": d.get("contextCost"),
        "pros": list(d.get("pros") or []),
        "cons": list(d.get("cons") or []),
        "verdict": d.get("verdict"),
        "license": d.get("license"),
        "stars": d.get("stars"),
        "created": (d.get("createdAt") or "")[:10],
        "pushed": (d.get("lastCommit") or "")[:10],
    }


def from_triage(t: dict[str, Any], slug: str) -> Entry:
    return {
        "slug": slug,
        "name": t.get("name"),
        "url": t.get("url"),
        "category": t.get("category"),
        "kind": t.get("kind"),
        "tier": "readme-triaged",
        "score": t.get("score"),
        "openrouter": t.get("openrouter"),
        "maturity": t.get("maturity"),
        "fit": None,
        "what": t.get("what"),
        "interface": [x.strip() for x in re.split(r"[,;]", t.get("interface") or "") if x.strip()][:20],
        "autoInvocation": t.get("autoInvocation"),
        "security": [],
        "pros": list(t.get("pros") or []),
        "cons": list(t.get("cons") or []),
        "verdict": None,
        "stars": t.get("stars"),
    }


def exclusion(d: dict[str, Any]) -> dict[str, Any] | None:
    """Exclusion record for a nonexistent or off-topic repo, None when it belongs in the catalog."""
    if d.get("exists") and d.get("isJevRelated"):
        return None
    return {
        "name": d.get("name"),
        "url": d.get("url"),
        "reason": "not Jev-related" if d.get("exists") else "nonexistent",
    }


class Catalog:
    """Analysed entries keyed by canonical repo key; the higher tier (then score) wins a collision."""

    def __init__(self, gh: GitHubIndex, judge_rank: dict[str, Any]) -> None:
        self.gh = gh
        self.judge_rank = judge_rank
        self.entries: dict[str, Entry] = {}

    def put(self, e: Entry) -> None:
        k = self.gh.canon_key(e["url"])
        m = self.gh.meta_for(e["url"])
        if m:
            e["stars"] = m.get("stargazerCount")
            e["created"] = (m.get("createdAt") or "")[:10]
            e["pushed"] = (m.get("pushedAt") or "")[:10]
            e["license"] = ((m.get("licenseInfo") or {}).get("spdxId")) or e.get("license")
            e["url"] = m.get("url") or e["url"]
            e["archived"] = m.get("isArchived")
            e["fork"] = m.get("isFork")
        jr = self.judge_rank.get(k)
        if jr:
            e["judge"] = jr
            if isinstance(jr.get("score"), (int, float)):
                e["score"] = jr["score"]
            e["category"] = jr["category"]
        prev = self.entries.get(k)
        if prev is None or self._beats(e, prev):
            if prev:
                e.setdefault("aliases", []).append(prev["url"])
            self.entries[k] = e
        else:
            prev.setdefault("aliases", []).append(e["url"])

    @staticmethod
    def _beats(e: Entry, prev: Entry) -> bool:
        new, old = TIER_RANK[e["tier"]], TIER_RANK[prev["tier"]]
        return new > old or (new == old and (e.get("score") or 0) > (prev.get("score") or 0))


def entry_order(e: Entry) -> tuple[str, float, float]:
    """Category (uncategorised last), then best score, then most stars."""
    return e["category"] or "zz", -(e.get("score") or 0), -(e.get("stars") or 0)


def unanalysed(awesome: dict[str, Any], catalog: Catalog) -> list[dict[str, Any]]:
    """Awesome-list repos that no audit or triage covered."""
    analyzed = {catalog.gh.canon_key(e["url"]) for e in catalog.entries.values()}
    out = []
    for repo, es in awesome.items():
        url = "https://github.com/" + repo
        k = catalog.gh.canon_key(url)
        if k in analyzed or ("gh:" + repo) in analyzed:
            continue
        out.append(
            {
                "name": repo,
                "url": url,
                "category": "unanalyzed",
                "tier": "listed-only",
                "sections": sorted({s for _, s, _ in es}),
                "what": re.sub(r"\s+", " ", es[0][2])[:300],
            }
        )
    return out


def build_catalog(data_dir: Path) -> dict[str, Any]:
    deep1 = load(data_dir, "deep_results.json")
    meta = load(data_dir, "gh_meta.json")
    meta.update(load(data_dir, "gh_meta_missing.json"))
    r2 = load(data_dir, "round2_results.json")
    awesome = load(data_dir, "awesome_entries.json")
    gh = GitHubIndex(meta)
    cat_of = index_items(r2["categorize"])
    verify = {k: v for k, v in r2["verify"].items() if v}
    deep2 = {k: v for k, v in r2["deep"].items() if v}
    triage = index_items(r2["triage"])
    cat_meta, judge_rank = judge_index(r2["catjudge"], gh)
    catalog = Catalog(gh, judge_rank)
    excluded = []
    # round 1 source audits
    for slug, d in deep1.items():
        if x := exclusion(d):
            excluded.append(x)
            continue
        e = from_deep(d, slug, cat_of.get(slug, {}).get("category") or "uncategorized", "source-audited")
        catalog.put(apply_verify(e, verify.get(slug)))
    # round 2 triage (+ new deep reads)
    for slug, t in triage.items():
        if x := exclusion(t):
            excluded.append(x)
            continue
        d = deep2.get(slug)
        if d and exclusion(d) is None:
            e = from_deep(d, slug, t.get("category"), "source-audited")
        else:
            e = from_triage(t, slug)
        e["ccRelevant"] = t.get("ccRelevant")
        catalog.put(apply_verify(e, verify.get(slug)))

    listed_only = unanalysed(awesome, catalog)
    routes = load(data_dir, "routes.json")
    for e in catalog.entries.values():
        if e["slug"] in routes["labels"]:
            e["routes"] = routes["labels"][e["slug"]]
    cat_meta["model-router"]["routesLegend"] = routes["legend"]

    entries = sorted(catalog.entries.values(), key=entry_order)
    return {
        "generated": GENERATED,
        "categories": cat_meta,
        "entries": entries,
        "listedOnly": listed_only,
        "excluded": excluded,
    }


# ---------- markdown ----------


def cell(s: Json, n: int = 200) -> str:
    t = re.sub(r"\s+", " ", str(s or "")).replace("|", "/")
    return t if len(t) <= n else t[: n - 1] + "…"


def bullets(xs: list[Any] | None, n: int = 3, w: int = 180) -> str:
    return "<br>".join("• " + cell(x, w) for x in (xs or [])[:n]) or "-"


def by_category(entries: list[Entry]) -> dict[str, list[Entry]]:
    by_cat: dict[str, list[Entry]] = collections.defaultdict(list)
    for e in entries:
        by_cat[e["category"] or "uncategorized"].append(e)
    return by_cat


def entry_row(i: int, e: Entry) -> str:
    stars = e.get("stars") if e.get("stars") is not None else "?"
    return (
        f"| {i} | [{cell(e['name'], 60)}]({e['url']}) | {TIER_LETTER[e['tier']]} | {e.get('score')} | "
        f"{e.get('openrouter') or '?'} | {e.get('maturity') or '?'} | {stars} | {cell(e.get('what'), 220)} | "
        f"{bullets(e.get('pros'))} | {bullets(e.get('cons'))} |"
    )


def render_markdown(catalog: dict[str, Any]) -> str:
    entries, cat_meta = catalog["entries"], catalog["categories"]
    listed_only, excluded = catalog["listedOnly"], catalog["excluded"]
    by_cat = by_category(entries)
    tiers = collections.Counter(e["tier"] for e in entries)
    lines = [
        "# Jev integration catalog (Claude Code relevance)",
        "",
        f"Generated {GENERATED}. {len(entries)} analysed entries ({tiers['verified']} adversarially verified, "
        f"{tiers['source-audited']} source-audited, {tiers['readme-triaged']} README-triaged), {len(listed_only)} "
        f"listed in community awesome-lists but not analysed (off-target), {len(excluded)} excluded.",
        "",
        "Score: 0-10 for daily coding in Claude Code with OpenRouter-hosted Jev (verified or category-judge score "
        "when available). OR = OpenRouter support found in code. Tier: V = verified, S = source-audited, "
        "R = README-triaged.",
        "",
    ]
    for cat in ORDER + sorted(set(by_cat) - set(ORDER)):
        es = by_cat.get(cat)
        if not es:
            continue
        cm = cat_meta.get(cat) or {}
        lines.append(f"## {cat} ({len(es)})")
        if cm:
            bp = cm.get("bestPick") or {}
            lines += [
                "",
                f"**Worth it:** {cm.get('worthIt')}. {cell(cm.get('worthItReason'), 600)}",
                f"**Best pick:** {bp.get('name')}: {cell(bp.get('reason'), 400)}",
            ]
        lines += [
            "",
            "| # | Name | Tier | Score | OR | Maturity | Stars | What | Pros | Cons |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        lines += [entry_row(i, e) for i, e in enumerate(es, start=1)]
        lines.append("")
    lines += [
        f"## Listed in awesome-lists, not analysed ({len(listed_only)})",
        "",
        "Off-target for Claude Code (apps, games, SDK experiments, domain tools) per their list section.",
        "",
        "| Name | List sections |",
        "|---|---|",
    ]
    lines += [
        f"| [{x['name']}]({x['url']}) | {cell(', '.join(x['sections']), 120)} |"
        for x in sorted(listed_only, key=lambda x: x["name"])
    ]
    lines += ["", f"## Excluded ({len(excluded)})", "", "| Name | Reason |", "|---|---|"]
    lines += [f"| {cell(x['name'], 80)} ({x['url']}) | {x['reason']} |" for x in excluded]
    return "\n".join(lines) + "\n"


def summary(catalog: dict[str, Any]) -> list[str]:
    entries = catalog["entries"]
    tiers = dict(collections.Counter(e["tier"] for e in entries))
    sizes = {c: len(v) for c, v in sorted(by_category(entries).items(), key=lambda x: -len(x[1]))}
    return [
        f"entries {len(entries)} {tiers} listed-only {len(catalog['listedOnly'])} excluded {len(catalog['excluded'])}",
        f"by category {sizes}",
    ]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    json_out = Path(args[0]) if args else DATA / "catalog.json"
    md_out = Path(args[1]) if len(args) > 1 else HERE / "JEV-CATALOG.md"
    catalog = build_catalog(DATA)
    text, md = json.dumps(catalog, indent=1), render_markdown(catalog)
    json_out.write_text(text, encoding="utf-8")  # pragma: no mutate
    md_out.write_text(md, encoding="utf-8")  # pragma: no mutate
    for line in summary(catalog):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
