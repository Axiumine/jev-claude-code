# jev_triage MCP server: pros, cons and trial log

Our own Jev MCP server: one tool, `jev_triage`, a cheap bulk yes/no screen for 30+ items against one criterion.

- Code: `reference-build/jev/mcp/jev-mcp.cjs` (135 lines) plus the shared `reference-build/jev/lib/jevlib.cjs` (275 lines), Node built-ins only.
- Installed at `~/.claude/hooks/jev/mcp/jev-mcp.cjs`, registered at user scope (`claude mcp get jev`).
- Why our own: integration report section 5 recommends no community server, only one self-hosted tool as a 3-week trial.
- Compared against the top community picks from report section 4.1: FrancoisChastel/jev-code, jkudish/jev-mcp, itsmostafa/system-one-connector.

## Pros

| Area | Ours | Community servers | Evidence |
|---|---|---|---|
| Client safety | Refuses unless the project root's egress is `full`; client and unknown roots never send | No egress control | e2e tests; `policy.json` roots |
| Key handling | Reads the guarded `.env` itself; no env block, key never in argv or `~/.claude.json` | system-one-connector writes the key to `~/.claude.json`; jkudish lets `TYPESAFE_API_KEY` silently override OpenRouter | Report 4.1 |
| Model | Pinned `jev-1.13`, drift flagged when the response model changes | Floating `jev-latest` or an env var | Live call 2026-10-02 returned `typesafe/jev-1.13-20260917` |
| Context cost | `items_file`: the server reads the file, about 300 tokens per call | Items pasted inline (about 10k tokens for 103 items) | Report section 11 |
| File safety | `items_file` must be inside the project, not a dotfile or dot-directory, symlinks resolved, root egress `full`; max 20 MB and 400 records | Not applicable | Refusals verified live 2026-10-02 |
| Bounded | 8 items per request, 8 in parallel, 6 s per request, 45 s total, $0.05 cap per call; fails open with error counts | jkudish caps exceed Jev's 32k context | Code; e2e tests |
| Output | clear_no / uncertain / clear_yes buckets (0.05 / 0.95), spot-check of dropped items, cost and model trailer | Raw probabilities per call | Live output |
| Instructions | Server instructions say when NOT to use it (fewer than 30 items, reasoning, code correctness, security, non-English) | Mostly "use me" | `INSTRUCTIONS` in `jev-mcp.cjs` |
| Shared plumbing | Same policy, breaker, budget and log as the hooks; `report.cjs` sees triage calls | Separate or none | `jevlib.cjs` |
| Tests | e2e suite includes an MCP handshake test; 194/194 offline tests pass | Mixed; system-one-connector has no CI test gate | Run 2026-10-02 |
| Cost | About $0.001 and 1 s per 100 items | Similar per call | Live calls |

## Cons

| Area | Problem | Mitigation |
|---|---|---|
| Discretion | Claude calls it only when it decides to; one published benchmark saw 0 unprompted calls in 150 | CLAUDE.md bullet; server instructions |
| Accuracy on subtle criteria | 73-91% of items come back uncertain when the text doesn't state the answer outright | Use only clear-cut criteria; label data (e.g. `routes.json`) for repeat questions |
| False clear_yes | Confident answers can still be wrong (1 of 16 clear_yes on 2026-10-02) | Treat clear_yes as "read next", never as verified |
| One verb | No classify, score or rank | Deliberate; add a verb to this server only on evidence |
| Third-party egress | Items go to OpenRouter and TypeSafe | Egress gate, personal roots only |
| Injection | Text inside items can sway Jev | Items go in `state` as data; output is advisory only |
| Guard blind spot | `no-secret-leak.cjs` does not inspect MCP arguments, and in bypass mode calls run silently | Egress gate and path checks inside the server |
| Portability | Tied to `~/.config/jev/policy.json` and the local Node path; not packaged | Personal use only |
| Upstream | Alpha endpoint; the pinned model can be retired (404) | Fails open; doctor and probe after model changes |

## Trial log

Add one row per real use or test. Keep counting real (unprompted or task-driven) calls separately from tests.

| Date | Kind | Task | Items | Result | Cost |
|---|---|---|---|---|---|
| 2026-10-02 | test | Model-router entries: routes between Claude models? (6 runs, report section 11) | 103 | 73-91% uncertain; 96 confident calls checked, none contradicted labels | ~$0.0008 per run |
| 2026-10-02 | test | Same criterion via `items_file`, checked against `routes.json` | 103 | 85 uncertain, 16 clear_yes (15 right; `gh-xinyao27-jevonian` labelled provider-pool), 2 clear_no (both right) | $0.00087, 1.0 s |
| 2026-10-02 | test | `items_file` = `.git/config`, and a path outside the project | 0 | Both refused locally, no network call | $0 |

## Keep or kill

- Kill rule: fewer than 1 real call per week by day 21, then `claude mcp remove jev --scope user`.
- Day 30 review: keep only with measurable savings (tokens or turns avoided) and no wrong drop that mattered.
- Real calls so far: 0. All 7 logged triage calls (2026-10-02) were tests. Count them by day from the `"triage"` rows in `~/.local/state/jev/log-*.jsonl`.
