#!/usr/bin/env node
'use strict';
// jev-brake: PreToolUse hook, matcher "Bash". DENY-ONLY brake for unattended (bypassPermissions) sessions.
//   T0 -> silent pass (no network).  T1 -> deterministic deny (no Jev), enforced per rule id (policy.t1_enforce: 'core' | 'all' | [ids]) and, by default, only inside subagents (policy.t1_scope).
//   T2 -> Jev adjudicates; deny only if Jev is sure, the user did not ask for it, the rule id is in policy.t2_enforce and the model has not drifted.
//   Never emits "allow"; every error/timeout/unknown -> silent pass (== status quo). There is no agent-typable override: the human
//   override is the shell-mode `!` prefix, which does not go through the Bash tool or its hooks.
// Output contract: stdout is either empty or ONE JSON object (hookSpecificOutput.permissionDecision = "deny").
const L = require('../lib/jevlib.cjs');
const { classify, extractCreated, T1_CORE } = require('../lib/shellclass.cjs');
const { skeleton } = require('../lib/skeleton.cjs');

const { BRAKE_POLICY_LINE: POLICY_LINE, BRAKE_QUESTIONS: QUESTIONS } = require('../lib/questions.cjs');

function denyOut(reason) { return { hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: reason } }; }
function reasonText(kind, ids, extra) {
  return `jev-brake: blocked (${kind}: ${ids.join(', ')}${extra ? '; ' + extra : ''}). This looks irreversible or outside the project. Do not retry it, rephrase it or work around it (other flags, another tool, a script). Tell the user which rule fired and why you needed it; if they really want it they can run it themselves with the ! prefix in their prompt.`;
}

let pendingCreated = null, currentSession = null;
async function main() {
  const input = L.readStdinJson();
  if (!input || input.tool_name !== 'Bash' || !input.tool_input || typeof input.tool_input.command !== 'string') return null;
  currentSession = input.session_id;
  const policy = L.loadPolicy();
  if (L.killed(policy, 'brake_t1') && L.killed(policy, 'brake_t2')) return null;
  const cmd = input.tool_input.command;
  const cwd = input.cwd || process.cwd();
  const root = process.env.CLAUDE_PROJECT_DIR || cwd;
  let cls = classify(cmd, { root, cwd });
  if (cls.hits.some((h) => /outside|sibling/.test(h.id))) {   // second pass: repo top-level + sibling worktrees count as inside; dirs this session created may be deleted
    const fam = L.familyRoots(cwd); const created = L.loadCreated(input.session_id).concat(input.scratchpad_dir ? [input.scratchpad_dir] : []);
    if (fam.length || created.length) cls = classify(cmd, { root, cwd, roots: fam, created });
  }
  if (/\b(mkdir|worktree|clone)\b/.test(cmd)) pendingCreated = extractCreated(cmd, cwd);
  if (cls.tier === 'T0') return null;

  // resolve conditional T1 hits with verified local git facts (no network)
  let facts = null; const needUntracked = cls.hits.some((h) => h.needs === 'untracked' || h.needs === 'uncommitted_any');
  const ensureFacts = () => facts || (facts = L.gitFacts(cwd, { untracked: needUntracked }));
  let own = null;   // repo created by this session (clone / worktree add / scratchpad) is agent-owned: nothing of the user's to lose
  const ensureOwn = () => { if (own !== null) return own; const f = ensureFacts(); const list = L.loadCreated(input.session_id).concat(input.scratchpad_dir ? [input.scratchpad_dir] : []); own = !!(f.top && list.some((c) => f.top === c || f.top.startsWith(c + '/'))); return own; };
  let hits = [];
  for (const h of cls.hits) {
    if (!h.needs) { hits.push(h); continue; }
    const f = ensureFacts();
    if (!f.git) { if (h.needs === 'uncommitted_any') hits.push(Object.assign({}, h, { tier: 'T2', needs: null })); continue; }   // git-only rules vanish outside a repo
    if (ensureOwn()) continue;
    const unknown = (v) => v === null || v === undefined;
    if (h.needs === 'dirty_tracked' && (f.dirty_tracked || unknown(f.dirty_tracked))) hits.push(h);
    else if (h.needs === 'untracked' && (f.untracked || unknown(f.untracked))) hits.push(h);
    else if (h.needs === 'uncommitted_any' && (f.dirty_tracked || f.untracked || unknown(f.dirty_tracked) || unknown(f.untracked))) hits.push(h);
  }
  const t1 = hits.filter((h) => h.tier === 'T1'); const t2 = hits.filter((h) => h.tier === 'T2');
  const cls2 = L.rootClass(policy, [root, cwd]);
  const sk = skeleton(cmd, { root, cwd });
  const base = { rc: cls2, sk, permission_mode: input.permission_mode, agent: input.agent_id ? (input.agent_type || 'sub') : 'main' };

  if (t1.length) {
    const inSet = (h) => policy.t1_enforce === 'all' || (policy.t1_enforce === 'core' ? T1_CORE.has(h.id) : policy.t1_enforce.includes(h.id));
    const inScope = policy.t1_scope === 'all' || !!input.agent_id;                       // default: only unattended subagents are blocked; the human is the guard in the main thread
    const enforced = policy.mode.brake_t1 === 'enforce' && inScope ? t1.filter(inSet) : [];
    L.log('brake', Object.assign({ tier: 'T1', ids: t1.map((h) => h.id), enforced: enforced.map((h) => h.id), decision: enforced.length ? 'deny' : 'would_deny', scope_skipped: policy.mode.brake_t1 === 'enforce' && !inScope ? true : undefined }, base));
    return enforced.length ? denyOut(reasonText('deterministic rule', enforced.map((h) => h.id))) : null;
  }
  if (!t2.length) return null;
  if (L.killed(policy, 'brake_t2')) return null;
  // Jev only reads the command text: for `rm -rf "$VAR"` the target is unknowable (about a quarter of all T2 hits in the real corpus), so asking
  // would only burn egress and cost. Logged, not sent.
  if (t2.every((h) => h.id === 'rm_unresolved_target')) { L.log('brake', Object.assign({ tier: 'T2', ids: t2.map((h) => h.id), decision: 'pass', why: 'unjudgeable' }, base)); return null; }

  const mode = L.egressMode(policy, cls2);
  if (mode === 'off') { L.log('brake', Object.assign({ tier: 'T2', ids: t2.map((h) => h.id), decision: 'pass', why: 'egress_off' }, base)); return null; }
  const full = mode === 'full';
  const f = ensureFacts();
  const state = {
    policy: POLICY_LINE,
    project: '<project>',
    cwd: L.abbreviate(cwd, root),
    command: full ? L.clip(L.abbreviate(L.redact(cmd), root), 1500, 300) : sk,
    matched_rules: t2.map((h) => h.id),
    keywords: cls.kws,
    facts: { git_repo: !!f.git, branch: f.branch, uncommitted_tracked_changes: f.dirty_tracked, untracked_files: f.untracked },
  };
  if (full) {
    if (cls.windows.length) state.evidence_windows = cls.windows.map((w) => L.abbreviate(L.redact(w), root));
    const d = input.tool_input.description; if (typeof d === 'string') state.agent_description = L.redact(d).slice(0, 200);
    const u = input.transcript_path ? L.lastUserRequest(input.transcript_path, 400) : undefined; if (u) state.user_request = u;
  }
  const questions = Object.assign({}, QUESTIONS); if (!state.user_request) delete questions.wanted;
  const r = await L.ask(policy, 'brake', state, questions, policy.timeouts_ms.brake);
  if (!r.ok) { L.log('brake', Object.assign({ tier: 'T2', ids: t2.map((h) => h.id), decision: 'pass', why: r.why, ms: r.ms }, base)); return null; }
  let cmp = null;   // optional A/B during shadow on personal roots: does the privacy-preserving skeleton give the same verdict as the full command?
  if (policy.compare_skeleton && full && policy.mode.brake_t2 === 'shadow') {
    const st2 = { policy: POLICY_LINE, project: '<project>', cwd: L.abbreviate(cwd, root), command: sk, matched_rules: t2.map((h) => h.id), keywords: cls.kws, facts: state.facts };
    const q2 = Object.assign({}, QUESTIONS); delete q2.wanted;
    const r2 = await L.ask(policy, 'brake', st2, q2, policy.timeouts_ms.brake);
    if (r2.ok) cmp = { sk_irr: r2.answers.irreversible.noul, sk_con: r2.answers.contained.noul };
  }
  const T = policy.thresholds.brake; const a = r.answers;
  const authorized = a.wanted && a.wanted.noul >= T.wanted_min;
  const deny = a.irreversible.noul >= T.irreversible_min && a.contained.noul <= T.contained_max && !authorized;
  const inSet2 = policy.t2_enforce === '*' || t2.some((h) => policy.t2_enforce.includes(h.id));
  const enforce = policy.mode.brake_t2 === 'enforce' && inSet2 && !r.drift;      // model drift (response model != policy.expected_model) => shadow only
  L.log('brake', Object.assign({ tier: 'T2', ids: t2.map((h) => h.id), egress: mode, irr: a.irreversible.noul, con: a.contained.noul, wan: a.wanted && a.wanted.noul, decision: deny ? (enforce ? 'deny' : 'would_deny') : 'pass', ms: r.ms, model: r.model, drift: r.drift || undefined, cost: r.cost, sk_irr: cmp && cmp.sk_irr, sk_con: cmp && cmp.sk_con, cmd: cls2 === 'personal' ? L.clip(L.abbreviate(L.redact(cmd), root), 300, 0) : undefined }, base));
  return deny && enforce ? denyOut(reasonText('Jev-confirmed', t2.map((h) => h.id), `irreversible=${a.irreversible.noul.toFixed(2)}`)) : null;
}

L.watchdog(5200);                      // hook timeout is 6 s; exit silently (fail-open) before Claude Code cancels us
(async () => {
  let out = null;
  try { out = await main(); } catch (e) { try { L.log('brake', { err: String(e && e.message || e).slice(0, 120) }); } catch { /* ignore */ } }
  if (!out && pendingCreated) { try { L.saveCreated(currentSession, pendingCreated); } catch { /* ignore */ } }
  if (out) process.stdout.write(JSON.stringify(out));
  process.exit(0);
})();
