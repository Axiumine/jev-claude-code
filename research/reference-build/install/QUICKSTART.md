# Quick start: the jev_triage MCP server

Installs the `jev_triage` MCP tool, the `typesafe-ai` skill and the deny rules that keep Claude away from your key, plus an optional reminder hook. It does not install the Bash brake or the web tripwire hooks; for those, see `INSTALL.txt` and section 7 of `research/JEV-INTEGRATION-REPORT.md`.

Run every step in your own terminal, not through Claude. Nothing here needs Claude to touch `~/.claude`, `~/.config/jev` or the key.

## Requirements

- Claude Code
- Node.js with `node --test` (tested on Node 24). With nvm, prefer a stable symlink such as `~/.nvm/current/bin/node` over a versioned path, or the server breaks on the next Node upgrade.
- `jq` (for `merge-settings.sh`), `patch` and `python3` (only for the optional guard step)
- An OpenRouter account

## Steps

```bash
git clone https://github.com/Axiumine/jev-claude-code && cd jev-claude-code
B="$PWD/research/reference-build"
R="$B/install"

# 0. Test before trusting (HOME must not be under /tmp)
(cd "$B" && HOME=/nonexistent-home/x node --test test/classify.test.cjs test/e2e.test.cjs)

# 1. Deny rules: Claude's Read/Edit tools can't touch the key folder, your hooks or settings.json, even in bypass mode
bash "$R/merge-settings.sh" --snippet "$R/settings-deny-key-only.json"            # dry run: read the diff
bash "$R/merge-settings.sh" --snippet "$R/settings-deny-key-only.json" --apply    # writes a backup first
#    settings-deny-stage-a.json instead also denies 12 Bash commands (git push --force*, git clean -fdx*, mkfs*, ...)
#    for Claude everywhere, main thread included; you can still run them yourself with `!`.

# 2. Skill (teaches Claude the Jev API when it writes app code)
mkdir -p ~/.claude/skills/typesafe-ai && cp "$B"/skill/typesafe-ai/{SKILL.md,LICENSE} ~/.claude/skills/typesafe-ai/

# 3. Server code
rm -rf ~/.claude/hooks/jev && mkdir -p ~/.claude/hooks/jev && cp -a "$B/jev/." ~/.claude/hooks/jev/

# 4. Config folders and policy
mkdir -p ~/.config/jev ~/.local/state/jev && chmod 700 ~/.config/jev ~/.local/state/jev
cp "$R/policy.example.json" ~/.config/jev/policy.json && chmod 600 ~/.config/jev/policy.json
$EDITOR ~/.config/jev/policy.json    # REQUIRED: replace the example roots with your own (see below)

# 5. Key. First, on OpenRouter: create a dedicated key with a $5 credit limit and keep
#    prompt/response logging off at openrouter.ai/settings/privacy. Then:
read -rsp 'OpenRouter key: ' K; echo
( umask 077; printf 'OPENROUTER_API_KEY=%s\n' "$K" > ~/.config/jev/.env ); unset K

# 6. Register the MCP server (no env block: the server reads the key file itself)
NODE="$(command -v node)"
claude mcp add-json --scope user jev "{\"command\":\"$NODE\",\"args\":[\"$HOME/.claude/hooks/jev/mcp/jev-mcp.cjs\"],\"timeout\":60000}"

# 7. Verify
node ~/.claude/hooks/jev/doctor.cjs --live   # copy the printed response model into policy.json as "expected_model"
                                             # (FAILs for the brake/webscreen hooks are expected: they are not installed)
node ~/.claude/hooks/jev/doctor.cjs --seal   # after reviewing the scripts: seals MANIFEST.sha256
claude mcp get jev                           # Status: ✔ Connected
```

## Policy: which projects may send data

`jev_triage` refuses unless the project directory sits in a root whose egress is `full`. Anything not listed is `unknown`, and unknown roots never send. Edit `roots` in `~/.config/jev/policy.json`:

```json
"roots": {
  "personal": ["/home/you/code/personal"],
  "client": ["/home/you/code/clients"]
},
"egress": { "personal": "full", "client": "off", "unknown": "off" }
```

The file must be `chmod 600` and owned by you. A looser file is ignored, the defaults apply, and the defaults have no roots, so every call is refused.

## CLAUDE.md

Append this to `~/.claude/CLAUDE.md` by hand:

```markdown
## Jev (typed yes/no judge via OpenRouter)
- Feature needs classify/route/moderate/triage/gate/score decision: load skill typesafe-ai BEFORE writing an LLM prompt-and-parse step. State the fit-check result first. Not bounded + cheap + fail-open -> plain code or LLM. Client repo: state the data flow, ask first.
- Never send keys or client data to Jev. Never read ~/.config/jev. Live Jev calls during development: the user runs them.
- 30+ items vs ONE criterion, or before a Workflow/subagent fan-out over items: call mcp__jev__jev_triage first. Read `uncertain`; `clear_yes` = read next, unverified; drop `clear_no` only if the spot-check looks sane. "Refused" = screen by hand. Items already in a .json/.jsonl file: pass `items_file` (+ `where`, `id_field`, `text_fields`), never read and paste them; a call costs ~300 tokens, so always worth a try. Inline items only: skip Jev when most items mention both sides of the criterion (e.g. every blurb names Claude AND OpenRouter); it leaves ~80% `uncertain`, so pasting them costs more than it saves.
```

## Check it in Claude Code

Restart Claude Code, then:

1. Ask Claude to "read ~/.config/jev/.env". It must be blocked.
2. In a project under a `personal` root, ask Claude to screen a JSON file of 30+ records against one yes/no question. Expect a line like `jev_triage: judged=103 errors=0 | clear_no=2 uncertain=85 clear_yes=16 | ... 1.0s $0.00087`.
3. The same request in an unlisted directory must be refused, and `node ~/.claude/hooks/jev/report.cjs 1` must show a `triage//refused_egress_off` row.

## Optional: reminder hook

The tool's schema is always loaded, but Claude still decides on its own whether to call it. In a 24-hour trial, server instructions and the CLAUDE.md bullet alone produced no calls. The `jev-nudge` hook adds a short reminder at the moments the tool fits:

- after a Bash listing (`grep`, `rg`, `find`, `ls`, `git ls-files`/`grep`, `git log --oneline`, `gh ... list`, `gh search`, `jq -r`) or a Grep or Glob result with 30+ items;
- before a Workflow or Agent call over 30+ items (a 30+ item array in the Workflow `args`, or a sentence such as "for each of the 120 files" in the script or prompt).

It never blocks and never calls Jev. The reminder text is fixed, so no tool output gets into it. It stays silent outside personal roots, and fires at most 3 times per session, 10 minutes apart.

```bash
bash "$R/merge-settings.sh" --snippet "$R/settings-snippet-nudge.json"            # dry run: read the diff
bash "$R/merge-settings.sh" --snippet "$R/settings-snippet-nudge.json" --apply    # writes a backup first
```

Tune it in `policy.json` with `"nudge": {"min_items": 30, "max_per_session": 3, "gap_s": 600}`, or turn it off with `"mode": {"nudge": "off"}`. Every reminder, and every one the cap held back, is a `nudge` row in the log.

## Secret guard caveat

Deny rules cover Claude's file tools, not every shell command. A Bash `cat ~/.config/jev/.env` is only reliably stopped by a `PreToolUse` guard hook. `guard-patch.diff` and `guard-test-cases.py` target the author's own `~/.claude/hooks/no-secret-leak.cjs`, which is not part of this repo. If you run a similar hook, make it deny any command that names `~/.config/jev` and any expansion of `$OPENROUTER_API_KEY`. If you don't, keep the OpenRouter key's credit limit low.

## Rollback

```bash
claude mcp remove jev --scope user
rm -rf ~/.claude/hooks/jev ~/.config/jev ~/.local/state/jev ~/.claude/skills/typesafe-ai
cp -p ~/.claude/settings.json.bak-jev-<timestamp> ~/.claude/settings.json
# remove the Jev section from ~/.claude/CLAUDE.md, then revoke the key on OpenRouter
```

To pause without uninstalling: `touch ~/.config/jev/OFF`.
