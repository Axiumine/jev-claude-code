#!/usr/bin/env python3
"""Inject a slim, scrubbed copy of data/catalog.json into page-template.html.

Usage: python3 build_page.py [OUT_HTML]   (default: jev-in-claude-code.html)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
Json = str | int | float | list[Any] | dict[str, Any] | None
GENERATED = "30 Sep 2026"
EMAIL = r"[A-Za-z0-9._%+-]+@(?!(?:[\w-]+\.)*(?:example\.com|typesafe\.ai)\b)[A-Za-z0-9.-]+\.[a-z]{2,}"
SCRUB = [
    (re.compile(r"/home/gio"), "~"),
    (re.compile(r"/media/(?:nvme|hdd)/Clienti[^\s,;)]*"), "<client root>"),
    (re.compile(r"/media/nvme/[^\s,;)]*"), "<local path>"),
    (re.compile(r"/tmp/claude-1000/[^\s,;)]*"), "<scratch path>"),  # noqa: S108 - scrub pattern, not a temp file
    (re.compile(EMAIL), "[email removed]"),
]
LEAK = re.compile(r"/home/gio|Clienti|sk-or-[A-Za-z0-9]{8,}|" + EMAIL)
MD_LINK = re.compile(r"\[[^]]*]\([^)]*\)")
# Compact JSON that keeps non-ASCII text as is (the page is UTF-8).
PAYLOAD = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))


def clean(s: Json, n: int | None = None) -> str | None:
    if s is None:
        return None
    t = re.sub(r"\s+", " ", str(s)).strip()
    for rx, rep in SCRUB:
        t = rx.sub(rep, t)
    return t if n is None or len(t) <= n else t[: n - 1] + "…"


def clean_list(xs: list[Any] | None, k: int = 5, n: int = 260) -> list[str | None]:
    return [clean(x, n) for x in (xs or [])[:k] if x]


def slim_entry(e: dict[str, Any]) -> dict[str, Any]:
    ver = e.get("verification") or {}
    score = e.get("score")
    return {
        "n": clean(e.get("name"), 90),
        "u": e.get("url"),
        "c": e.get("category") or "other",
        "tier": e["tier"],
        "s": score if isinstance(score, (int, float)) else None,
        "o": e.get("openrouter") or "unknown",
        "m": e.get("maturity"),
        "st": e.get("stars"),
        "cr": e.get("created") or None,
        "w": clean(e.get("what"), 320),
        "p": clean_list(e.get("pros")),
        "k": clean_list(e.get("cons")),
        "v": clean(e.get("verdict"), 400),
        "a": clean(e.get("autoInvocation"), 260),
        "i": [clean(x, 60) for x in (e.get("interface") or [])[:12]],
        "sec": clean_list(e.get("security"), 4, 240),
        "vn": clean(ver.get("note"), 420),
        "mcp": e.get("kind") == "mcp-server" or e.get("category") in ("decision-mcp", "task-mcp"),
    }


def slim_listed(x: dict[str, Any]) -> dict[str, Any]:
    return {
        "n": x["name"],
        "u": x["url"],
        "tier": "listed-only",
        "w": clean(MD_LINK.sub("", x.get("what") or ""), 240),
        "sec": clean(", ".join(x.get("sections") or []), 80),
        "p": [],
        "k": [],
        "i": [],
    }


def page_data(cat: dict[str, Any]) -> dict[str, Any]:
    entries = [slim_entry(e) for e in cat["entries"]]
    listed = [slim_listed(x) for x in cat["listedOnly"]]
    cats = {
        k: {"worthIt": v.get("worthIt"), "bestPick": {"name": clean((v.get("bestPick") or {}).get("name"), 80)}}
        for k, v in cat["categories"].items()
    }
    stats = {
        "analysed": len(entries),
        "verified": sum(e["tier"] == "verified" for e in entries),
        "source": sum(e["tier"] in ("verified", "source-audited") for e in entries),
        "mcp": sum(e["mcp"] for e in entries),
    }
    return {"generated": GENERATED, "stats": stats, "categories": cats, "entries": entries, "listed": listed}


def render_page(template: str, data: dict[str, Any]) -> str:
    """Embed data as JSON in the template; '</' is escaped so the payload cannot close its <script> tag."""
    blob = PAYLOAD.encode(data).replace("</", "<\\/")
    return template.replace("__DATA__", blob)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    out_path = Path(args[0]) if args else HERE / "jev-in-claude-code.html"
    cat_path, template_path = HERE / "data" / "catalog.json", HERE / "page-template.html"
    cat_text = cat_path.read_text(encoding="utf-8")  # pragma: no mutate  (utf-8 == locale default: equivalent mutants)
    template = template_path.read_text(encoding="utf-8")  # pragma: no mutate
    data = page_data(json.loads(cat_text))
    page = render_page(template, data)
    out_path.write_text(page, encoding="utf-8")  # pragma: no mutate
    leaks = LEAK.findall(page)
    print(
        out_path,
        len(page.encode()),
        "bytes; entries",
        len(data["entries"]),
        "listed",
        len(data["listed"]),
        "stats",
        data["stats"],
        "leak-hits",
        len(leaks),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
