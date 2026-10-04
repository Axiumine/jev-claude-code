#!/usr/bin/env node
'use strict';
// jev-nudge: PostToolUse hook (matcher "Grep|Glob|Bash") and PreToolUse hook (matcher "Workflow|Agent"). Reminds the model that
// mcp__jev__jev_triage exists at the moments it fits: a listing of 30+ items just came back, or a fan-out over 30+ items starts.
// Server instructions and CLAUDE.md alone produced 0 calls in a 24h trial over 5 projects, so the reminder comes at the decision point.
// Advisory only: never blocks, never calls Jev, sends nothing anywhere. The text is static apart from a tool name from a fixed set and a
// count, so a grep hit can not smuggle instructions into it. Silent unless the project root's egress is "full" (elsewhere the tool refuses,
// and a nudge toward a refusal teaches the model to ignore nudges). At most policy.nudge.max_per_session per session, gap_s apart.
// Fail-open: any problem => exit 0, no output.
const fs = require('fs');
const path = require('path');
const L = require('../lib/jevlib.cjs');

// A command that prints a list: a listing tool at the start of the command or of any pipe stage, after env assignments, sudo or time.
// git log and jq count only in their one-record-per-line forms (git log -p or jq . print diffs and objects, not lists).
const LISTER = new RegExp('(?:^|\\|)\\s*(?:\\w+=\\S*\\s+|sudo\\s+|time\\s+|command\\s+)*' +
  '(grep|rg|ag|find|fd|ls|tree|git(?:\\s+-C\\s+\\S+)?\\s+(?:ls-files|grep|branch|tag|log(?=[^|]*--(?:oneline|format|pretty|name-only)))' +
  '|gh\\s+(?:\\S+\\s+list|search)|jq(?=[^|]*\\s(?:-[a-zA-Z]*[rc]\\b|--raw-output|--compact-output)))\\b');
// Commands that print nothing worth counting, so they may precede the listing.
const QUIET = /^(?:(?:cd|pushd|popd|export|unset|set|source|\.|mkdir|umask|true|:)(?:\s|$)|\w+=\S*$)/;

// Split a Bash command line into its sequential commands (; && || & newline), outside quotes; pipes stay inside one command.
function commands(line) {
  const out = []; let cur = '', q = null;
  const cut = () => { out.push(cur.trim().replace(/^[({]\s*/, '').replace(/\s*[)}]$/, '')); cur = ''; };
  for (let i = 0; i < line.length; i++) {
    const c = line[i], n = line[i + 1];
    if (q) { cur += c; if (c === '\\' && q === '"' && n !== undefined) cur += line[++i]; else if (c === q) q = null; continue; }
    if (c === '\\' && n !== undefined) { cur += c + line[++i]; continue; }
    if (c === "'" || c === '"') { q = c; cur += c; continue; }
    if ((c === '&' && n === '&') || (c === '|' && n === '|')) { cut(); i++; continue; }
    if (c === ';' || c === '\n' || (c === '&' && n !== '>' && line[i - 1] !== '>')) { cut(); continue; }
    cur += c;
  }
  cut();
  return out.filter(Boolean);
}
// The output counts as a list only when the last command lists something and every command before it is quiet or a listing too:
// in `git commit ... && git log --oneline -2` most of the output is the commit's own, not a list.
function isListing(line) {
  const cs = commands(line);
  return cs.length > 0 && LISTER.test(cs[cs.length - 1]) && cs.slice(0, -1).every((c) => QUIET.test(c) || LISTER.test(c));
}
// "120 files", "45 open issues": a count the model wrote into a fan-out prompt or script. Counted only in a sentence that also says
// the work goes per item ("each", "every", "per", "across", ...), so "read the first 200 rows" is not a fan-out.
const NUM_NOUN = /\b(\d{2,4})\s+(?:[a-z-]+\s+)?(files|items|hits|matches|issues|prs|pull requests|repos|repositories|entries|records|results|candidates|rows|stories|urls|packages|endpoints)\b/gi;
const PER_ITEM = /\b(each|every|per|all|across|over|in parallel|fan(?:s|ning)?[ -]?out)\b/i;

const lines = (s) => String(s || '').split('\n').filter((l) => l.trim()).length;
function maxArray(v, depth) {
  if (depth > 4 || !v || typeof v !== 'object') return 0;
  let m = Array.isArray(v) ? v.length : 0;
  for (const x of Object.values(v)) m = Math.max(m, maxArray(x, depth + 1));
  return m;
}
function statedCount(text) {
  let best = 0;
  for (const s of String(text).split(/[.;!?\n]+(?:\s|$)|\n/)) {
    if (!PER_ITEM.test(s)) continue;
    let m; NUM_NOUN.lastIndex = 0; while ((m = NUM_NOUN.exec(s))) best = Math.max(best, Number(m[1]));
  }
  return best;
}

// How many items the call is about; 0 when it is not a listing or a fan-out.
function count(input) {
  const t = input.tool_name, ti = input.tool_input || {}, tr = input.tool_response;
  if (input.hook_event_name === 'PreToolUse') {
    if (t === 'Workflow') return Math.max(maxArray(ti.args, 0), statedCount(String(ti.script || '')));
    if (t === 'Agent') return statedCount(`${ti.description || ''} ${ti.prompt || ''}`);
    return 0;
  }
  if (input.hook_event_name !== 'PostToolUse') return 0;
  if (t === 'Bash') {
    if (!isListing(String(ti.command || '')) || (tr && tr.interrupted)) return 0;
    return lines(typeof tr === 'string' ? tr : tr && tr.stdout);
  }
  if (t === 'Grep' || t === 'Glob') {
    if (typeof tr === 'string') return lines(tr);
    const r = tr || {};
    return Math.max(Array.isArray(r.filenames) ? r.filenames.length : 0, Number(r.numFiles) || 0, r.mode === 'content' ? Number(r.numLines) || lines(r.content) : 0);
  }
  return 0;
}

// Per-session budget, shared by the main thread and its subagents (same session_id).
function allowed(policy, session) {
  const d = path.join(L.STATE, 'nudge'); const f = path.join(d, String(session || 'nosession').replace(/[^\w-]/g, '').slice(0, 64) + '.json');
  const s = L.readJson(f, { n: 0, t: 0 }); const now = Date.now(); const dt = now - (Number(s.t) || 0);
  if ((Number(s.n) || 0) >= policy.nudge.max_per_session || (dt >= 0 && dt < policy.nudge.gap_s * 1000)) return false;   // a stamp in the future (clock stepped back) never mutes the hook
  L.writeJson(f, { n: (Number(s.n) || 0) + 1, t: now });
  if (Math.random() < 0.02) try { for (const n of fs.readdirSync(d)) if (now - fs.statSync(path.join(d, n)).mtimeMs > 7 * 864e5) fs.unlinkSync(path.join(d, n)); } catch { /* ignore */ }
  return true;
}

function main() {
  const input = L.readStdinJson();
  if (!input || !/^(Grep|Glob|Bash|Workflow|Agent)$/.test(input.tool_name)) return null;
  const policy = L.loadPolicy();
  if (L.killed(policy, 'nudge') || L.killed(policy, 'triage')) return null;
  const root = process.env.CLAUDE_PROJECT_DIR || input.cwd || process.cwd();
  const rc = L.rootClass(policy, [root, input.cwd]);
  if (L.egressMode(policy, rc) !== 'full') return null;
  const n = count(input);
  if (n < policy.nudge.min_items) return null;
  const pre = input.hook_event_name === 'PreToolUse';
  const ok = allowed(policy, input.session_id);
  L.log('nudge', { decision: ok ? 'nudge' : 'capped', ev: pre ? 'pre' : 'post', tool: input.tool_name, items: n, root_class: rc, agent: input.agent_id ? (input.agent_type || 'sub') : 'main' });
  if (!ok) return null;
  const msg = pre
    ? `[jev-nudge] This ${input.tool_name} call fans out over about ${n} items. When the first job per item is "is this one relevant?", screen the list with mcp__jev__jev_triage before the fan-out (one yes/no criterion; items inline as {id, text}, or items_file for a .json/.jsonl) and fan out only over clear_yes + uncertain. Not for code-correctness or security verdicts.`
    : `[jev-nudge] That ${input.tool_name} result has ${n} items. If you are about to pick the relevant ones by reading them or by spawning agents over them, screen them first with mcp__jev__jev_triage (one yes/no criterion; items inline as {id, text}, or items_file for a .json/.jsonl). Drop clear_no after the spot-check, read uncertain yourself. Not for code-correctness or security verdicts.`;
  return { hookSpecificOutput: { hookEventName: pre ? 'PreToolUse' : 'PostToolUse', additionalContext: msg } };
}
L.watchdog(2000);
let out = null;
try { out = main(); } catch (e) { try { L.log('nudge', { err: String(e && e.message || e).slice(0, 120) }); } catch { /* ignore */ } }
if (out) process.stdout.write(JSON.stringify(out));
process.exit(0);
