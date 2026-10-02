# jev-claude-code

Research and a reference build for using **Jev**, TypeSafe's typed yes/no judge model, inside **Claude Code** through **OpenRouter**.

The repo contains:

- a catalog of 983 community Jev integrations, scored for daily use in Claude Code;
- an integration report with a staged, test-gated plan;
- `jev_triage`, a small hardened MCP server with one tool: a cheap bulk yes/no screen that Claude can run before spending its own tokens on 30+ items.

> Unofficial. Not affiliated with TypeSafe AI, OpenRouter or Anthropic. Jev runs on an alpha endpoint; treat everything here as research-grade.

## What is Jev

Jev (TypeSafe "System One") returns typed judgments instead of free text: **Noul** (yes/no probability), **Choice** (pick one option) and **Score**. A call takes about 0.2 s and costs fractions of a cent. On OpenRouter the model is `typesafe/jev-1.13`.

It is good at bounded decisions on short text: routing, triage, moderation, gating, relevance. It is not a reasoner. It is weak at math, dates, counting and code-correctness verdicts (about 72% on vulnerable-code checks in independent evals), it is overconfident on unanswerable input, and text inside its input can sway it.

## What's in this repo

| Path | What |
|---|---|
| `research/JEV-INTEGRATION-REPORT.md` | Main report: ecosystem, 15 categories, recommended architecture, staged plan, test plan, risks, trial results |
| `research/JEV-CATALOG.md` | The catalog as Markdown tables, one per category, with scores, pros and cons |
| `research/jev-in-claude-code.html` | Same catalog as a single self-contained page, with local paths scrubbed |
| `research/JEV-MCP-PROS-CONS.md` | Pros, cons and trial log of our `jev_triage` server |
| `research/data/catalog.json` | Machine-readable catalog (`.entries[]`); model-router entries carry a hand-labelled `routes` field |
| `research/data/routes.json` | Hand labels for the 103 model-router entries, merged by `build_catalog.py` |
| `research/reference-build/jev/` | The code: MCP server, shared lib, optional hooks, doctor, probe, report (Node built-ins only) |
| `research/reference-build/install/` | Install files: [QUICKSTART.md](research/reference-build/install/QUICKSTART.md), policy example, deny-rule snippets, settings merge script |
| `research/reference-build/skill/` | Vendored official `typesafe-ai` skill (MIT, TypeSafe AI) with a local safety addendum |
| `research/reference-build/test/` | Offline test suites (mock OpenRouter endpoint, temporary git repos) |
| `research/reference-build/shelf/` | Experimental gates that were evaluated and not installed |
| `research/reference-build/backtest/` | Replay of past Claude Code sessions against the brake rules (counts only) |

## The `jev_triage` MCP server

One tool. Claude passes a criterion and up to 400 items, inline or as a `.json`/`.jsonl` file the server reads itself. Jev judges every item, and the tool returns three buckets:

```text
jev_triage: judged=103 errors=0 | clear_no=2 uncertain=85 clear_yes=16 | thresholds 0.05/0.95 | 1.0s $0.00087 typesafe/jev-1.13-20260917
source: items_file research/data/catalog.json, 103 of 983 records matched
clear_yes: ...
uncertain: ...
clear_no: ...
spot_check_no (random clear_no items; if any looks relevant your criterion is off): ...
```

How Claude uses it: drop `clear_no` after the spot-check looks sane, read `uncertain` itself, and treat `clear_yes` as "read next", never as verified.

**Use it for** 30+ short items (repo blurbs, issue titles, log lines, grep hits, test names) against one crisp yes/no question, before a subagent or workflow fan-out over those items.

**Don't use it for** fewer than 30 items, reasoning, code correctness, security verdicts, math/dates/counting, non-English text, or anything where a wrong drop is costly.

### How it differs from community Jev MCP servers

| Area | `jev_triage` |
|---|---|
| Egress control | Refuses unless the project sits in a root whose policy egress is `full`; client and unknown roots never send |
| Key handling | Reads a `0600` key file itself; no env block, key never in argv or `~/.claude.json` |
| Model | Pinned `jev-1.13`; drift is flagged when the response model changes |
| Context cost | `items_file`: the server reads the file, so a call costs about 300 tokens instead of pasting every item |
| File safety | `items_file` must be inside the project, not a dotfile or dot-directory, symlinks resolved; max 20 MB and 400 records |
| Bounds | 8 items per request, 8 in parallel, 6 s per request, 45 s total, $0.05 per call; fails open with error counts |
| Injection posture | Items go in `state` as data, never in instructions; output is advisory only |
| Size | About 135 lines plus a 275-line shared lib, no dependencies |

Full comparison and the trial log: [research/JEV-MCP-PROS-CONS.md](research/JEV-MCP-PROS-CONS.md).

## Quick start

See [research/reference-build/install/QUICKSTART.md](research/reference-build/install/QUICKSTART.md). In short:

1. Add deny rules so Claude's file tools can't read the key or edit your hooks and settings.
2. Install the skill and copy the server to `~/.claude/hooks/jev/`.
3. Write `~/.config/jev/policy.json` with your own roots, and the key file.
4. Register the server with `claude mcp add-json --scope user jev ...`.
5. Verify with `doctor.cjs --live` and `claude mcp get jev`.

Requirements: Claude Code, Node.js (tested on 24), `jq`, an OpenRouter key with a low credit limit.

The full staged plan, including the optional Bash brake and web tripwire hooks, is in section 7 of the report and in `install/INSTALL.txt`.

## Key findings

- **Where Jev pays off:** when Claude writes application code that needs a fast, cheap, typed decision. Jev is newer than Claude's training data, so the vendored skill is what gets Claude to wire it correctly: pinned model, a none option, a 2 s timeout and a fallback.
- **Inside Claude Code's own loop it adds little.** MCP tools are called at the model's discretion (one published benchmark saw 0 unprompted calls in 150), and permission auto-approvers do nothing in bypass mode.
- **Run one server, not several.** At most one self-hosted server with one tool, as a time-boxed trial. Most community servers add overlapping verbs, model-composed egress the secret guard can't inspect, and key-handling problems.
- **Triage trial (2026-10-02):** on 103 model-router blurbs, Jev's confident calls were almost always right (96 of 96 in earlier runs, 17 of 18 in a later run), but 73-91% of items came back uncertain because the blurbs rarely state the answer outright. `items_file` cut a call from about 10k to about 300 tokens. For repeat questions, a labelled field in the data beat triage (3 turns and $0.23 vs 14 turns and $0.79).
- **Most protection doesn't need Jev.** Deny rules, a deterministic Bash brake and a hardened secret guard cover the dangerous cases without a model.

## Tests

```bash
cd research/reference-build
HOME=/nonexistent-home/x node --test test/classify.test.cjs test/e2e.test.cjs   # 194 tests
(cd shelf && HOME=/nonexistent-home/x node --test jev-gate.test.cjs)            # 28 tests
```

`HOME` must not be under `/tmp`, because the classifier treats `/tmp` as scratch space. The e2e suite uses a mock OpenRouter endpoint and covers an MCP handshake, `items_file` path guards, fail-open on timeouts and bad responses, the kill switch, key and policy permissions, env taint, model drift and egress by root class.

## Rebuilding the catalog

```bash
python3 research/build_catalog.py   # merges data/*.json and routes.json into data/catalog.json and JEV-CATALOG.md
python3 research/build_page.py      # injects a scrubbed copy into jev-in-claude-code.html
```

Edit labels in `research/data/routes.json`, not in `catalog.json`.

## Security and privacy

- Items sent to `jev_triage` leave your machine (OpenRouter, then TypeSafe). The egress policy is the only thing deciding which projects may send, so keep client work out of `full` roots.
- Use a dedicated OpenRouter key with a small credit limit, and keep prompt/response logging off.
- Deny rules cover Claude's file tools. Stopping shell reads of the key (`cat`, `grep -r`, globbing) takes a `PreToolUse` guard hook; see the caveat in QUICKSTART.
- Everything fails open: if Jev is down, slow or misconfigured, the tool returns errors and Claude screens by hand.

## Status

This is a research snapshot from late September and early October 2026, built against Claude Code 2.1.285 and `typesafe/jev-1.13`. The `jev_triage` server is in a trial with an explicit kill rule: fewer than one real call per week by day 21 means it gets removed. The ecosystem numbers (stars, ages, versions) were accurate at research time and will drift.

## License

Copyright (C) 2026 [Giovanni Manzoni](https://github.com/giovannimanzoni)

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

The full license text is in [LICENSE](LICENSE).

Exception: the vendored `typesafe-ai` skill in `research/reference-build/skill/typesafe-ai/` keeps its own MIT license, © TypeSafe AI; see its `LICENSE`.
