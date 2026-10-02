#!/usr/bin/env node
'use strict';
// jev-doctor: run in YOUR terminal (never through Claude). Verifies key handling, policy, hook registration, deny rules, the secret guard,
// script integrity and (with --live) the endpoint. Never prints the key.   Usage: node doctor.cjs [--live] [--seal]
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const L = require('./lib/jevlib.cjs');
const HOME = L.HOME; const ROOT = __dirname; const res = [];
const add = (lvl, what, detail) => { res.push([lvl, what, detail || '']); };
const sha = (f) => crypto.createHash('sha256').update(fs.readFileSync(f)).digest('hex');
function files(d) { return fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => e.isDirectory() ? files(path.join(d, e.name)) : /\.(cjs|json)$/.test(e.name) && e.name !== 'MANIFEST.sha256' ? [path.join(d, e.name)] : []); }
const modeOf = (p) => { try { return (fs.statSync(p).mode & 0o777).toString(8); } catch { return 'missing'; } };

(async () => {
  const policy = L.loadPolicy();
  // 1 runtime
  add(Number(process.versions.node.split('.')[0]) >= 20 ? 'ok' : 'FAIL', 'node >= 20', process.execPath + ' v' + process.versions.node);
  add(process.execPath.includes('/.nvm/versions/') ? 'warn' : 'ok', 'node path stable', process.execPath.includes('/.nvm/versions/') ? 'versioned nvm path: hook commands must use ' + HOME + '/.nvm/current/bin/node (check the symlink after every node upgrade)' : 'ok');
  // 2 key (never printed)
  const k = L.readKey(); const kf = path.join(L.CFG, L.KEY_FILE);
  add(k.key ? 'ok' : 'FAIL', 'key file present, mode 0600, owned by you, line OPENROUTER_API_KEY=sk-or-...', k.key ? kf + ' mode ' + modeOf(kf) : kf + ': ' + String(k.err));
  // 3 policy
  const pf = path.join(L.CFG, 'policy.json');
  add(fs.existsSync(pf) ? (L.privateFile(pf) ? 'ok' : 'FAIL') : 'warn', 'policy.json (0600, owned by you)', fs.existsSync(pf) ? 'mode ' + modeOf(pf) + (L.privateFile(pf) ? '' : ' - IGNORED until chmod 600') : 'missing: defaults apply (no roots => no Jev egress, brake in shadow)');
  add(policy.roots.personal.length ? 'ok' : 'warn', 'roots.personal', policy.roots.personal.join(', ') || 'empty: T2 adjudication, Bash-read screening and triage will not run anywhere');
  add(policy.roots.client.length ? 'ok' : 'warn', 'roots.client', policy.roots.client.join(', ') || 'empty (client work would count as unknown: web text screened, everything else off)');
  add(/latest/.test(policy.model) ? 'FAIL' : 'ok', 'model pinned (no *-latest alias)', policy.model);
  add(policy.expected_model ? 'ok' : 'warn', 'expected_model set (drift detection)', policy.expected_model || 'not set: run doctor --live and copy the response model into policy.json');
  add(L.endpointOk(policy) ? 'ok' : 'FAIL', 'endpoint host allowlist + https', policy.endpoint);
  add(policy.provider && policy.provider.zdr && policy.provider.data_collection === 'deny' ? 'ok' : 'FAIL', 'provider preferences', JSON.stringify(policy.provider));
  add(fs.existsSync(path.join(L.CFG, 'OFF')) ? 'warn' : 'ok', 'kill switch', fs.existsSync(path.join(L.CFG, 'OFF')) ? 'OFF file present: everything disabled' : 'not set (touch ' + path.join(L.CFG, 'OFF') + ' to disable everything at once)');
  add('ok', 'modes', JSON.stringify(policy.mode) + ' t1_enforce=' + JSON.stringify(policy.t1_enforce) + ' t2_enforce=' + JSON.stringify(policy.t2_enforce));
  const taint = L.taintedEnv(policy);
  add(taint.length ? 'warn' : 'ok', 'environment taint (this shell; hooks see Claude Code\'s env)', taint.join(', ') || 'none');
  // 4 hooks registered with existing absolute paths, deny rules, secret guard
  try {
    const st = JSON.parse(fs.readFileSync(path.join(HOME, '.claude', 'settings.json'), 'utf8')); const seen = [];
    for (const [ev, arr] of Object.entries(st.hooks || {})) for (const m of arr) for (const h of m.hooks || []) {
      const a = (h.args || [])[0] || '';
      if (/hooks\/jev\/hooks\//.test(a)) {
        seen.push(ev + ':' + (m.matcher || '*') + (h.if ? '[' + h.if + ']' : '') + ' -> ' + path.basename(a) + (h.async ? ' (async)' : ''));
        if (!(path.isAbsolute(h.command) && fs.existsSync(h.command))) add('FAIL', 'hook interpreter exists (' + path.basename(a) + ')', h.command);
        if (!fs.existsSync(a)) add('FAIL', 'hook script exists', a);
      }
    }
    add(seen.length ? 'ok' : 'warn', 'hooks registered in settings.json', seen.length + ' handlers: ' + (seen.join(' | ') || 'none yet'));
    const deny = ((st.permissions || {}).deny || []).join('\n');
    for (const need of ['Read(~/.config/jev/', 'Edit(~/.config/jev/', 'Edit(~/.claude/hooks/', 'Edit(~/.claude/settings.json)', 'Edit(~/.claude/settings.local.json)', 'Edit(~/.local/state/jev/'])
      if (!deny.includes(need)) add('warn', 'deny rule ' + need + '**)', 'missing in permissions.deny');
    if (['Read(~/.config/jev/', 'Edit(~/.claude/hooks/'].every((n) => deny.includes(n))) add('ok', 'deny rules protect config dir and hook dir', '');
  } catch (e) { add('warn', 'settings.json', String(e.message).slice(0, 80)); }
  try {
    const g = fs.readFileSync(path.join(HOME, '.claude', 'hooks', 'no-secret-leak.cjs'), 'utf8');
    add(/OPENROUTER_API_KEY/.test(g) ? 'ok' : 'FAIL', 'secret-leak guard lists OPENROUTER_API_KEY', /OPENROUTER_API_KEY/.test(g) ? '' : 'add it to PROTECTED_VARS in ~/.claude/hooks/no-secret-leak.cjs');
    // behavioural check: does the guard really refuse Claude's Read/Bash on the key file and on the variable? (synthetic payloads, read-only)
    const { execFileSync } = require('child_process'); const gp = path.join(HOME, '.claude', 'hooks', 'no-secret-leak.cjs');
    const deny = (payload) => { try { const o = execFileSync(process.execPath, [gp], { input: JSON.stringify(payload), timeout: 5000 }).toString(); return JSON.parse(o).hookSpecificOutput.permissionDecision === 'deny'; } catch { return false; } };
    const kf2 = path.join(L.CFG, L.KEY_FILE);
    add(deny({ tool_name: 'Read', tool_input: { file_path: kf2 } }) && deny({ tool_name: 'Bash', tool_input: { command: 'cat ' + kf2 } }) ? 'ok' : 'FAIL', 'guard refuses Read/cat on the key file', kf2);
    add(deny({ tool_name: 'Bash', tool_input: { command: 'echo $' + 'OPENROUTER_API_' + 'KEY' } }) ? 'ok' : 'FAIL', 'guard refuses shell expansion of the variable', '');
  } catch { add('warn', 'secret-leak guard', 'not found'); }
  // 5 skill + CLAUDE.md pointer (informational)
  try {
    const sk = fs.readFileSync(path.join(HOME, '.claude', 'skills', 'typesafe-ai', 'SKILL.md'), 'utf8'); const d = (sk.match(/description: >\n((?:  .*\n)+)/) || [, ''])[1];
    add(d.length && d.length < 700 ? 'ok' : 'warn', 'skill typesafe-ai installed, short description', d.length + ' chars; confirm in /doctor or /context that the description (not just the name) is listed');
  } catch { add('warn', 'skill typesafe-ai', 'not installed at ~/.claude/skills/typesafe-ai/'); }
  try { add(/typesafe-ai/.test(fs.readFileSync(path.join(HOME, '.claude', 'CLAUDE.md'), 'utf8')) ? 'ok' : 'warn', 'CLAUDE.md pointer to the skill', ''); } catch { add('warn', 'CLAUDE.md', 'not found'); }
  // 6 integrity
  const man = path.join(ROOT, 'MANIFEST.sha256');
  if (process.argv.includes('--seal')) { fs.writeFileSync(man, files(ROOT).map((f) => `${sha(f)}  ${path.relative(ROOT, f)}`).sort().join('\n') + '\n'); add('ok', 'sealed manifest', man); }
  else if (fs.existsSync(man)) {
    const bad = []; for (const ln of fs.readFileSync(man, 'utf8').trim().split('\n')) { const [h, f] = ln.split(/\s{2}/); try { if (sha(path.join(ROOT, f)) !== h) bad.push(f); } catch { bad.push(f + ' (missing)'); } }
    add(bad.length ? 'FAIL' : 'ok', 'script integrity vs MANIFEST.sha256', bad.join(', '));
  } else add('warn', 'MANIFEST.sha256', 'not sealed: run doctor --seal after reviewing the scripts');
  // 7 live probe (one tiny request)
  if (process.argv.includes('--live')) {
    const q = { ping: { type: 'noul', instructions: 'The `text` is a friendly greeting.' } };
    const r1 = await L.ask(policy, 'doctor', { text: 'hello there' }, q, policy.timeouts_ms.doctor);
    add(r1.ok ? 'ok' : 'FAIL', 'live call (pinned model, provider zdr + data_collection=deny)', r1.ok ? `${r1.ms} ms, response model=${r1.model}, cost=$${r1.cost}` : String(r1.why));
    if (r1.ok) {
      add(policy.expected_model ? (r1.drift ? 'warn' : 'ok') : 'warn', 'response model vs expected_model', policy.expected_model ? `${r1.model} vs ${policy.expected_model}` : `set "expected_model": "${r1.model}" in policy.json`);
      add(r1.answers.ping.noul > 0.5 ? 'ok' : 'warn', 'sanity: a greeting scores > 0.5', String(r1.answers.ping.noul));
    } else {
      const p2 = Object.assign({}, policy, { provider: null }); const r2 = await L.ask(p2, 'doctor', { text: 'hello there' }, q, policy.timeouts_ms.doctor);
      add(r2.ok ? 'warn' : 'FAIL', 'diagnosis: retry WITHOUT provider preferences', r2.ok ? 'works only without zdr/data_collection: OpenRouter does not treat this route as no-retention. Decide before sending anything from client work (allow_non_zdr).' : String(r2.why));
      const p3 = Object.assign({}, policy, { endpoint: 'https://openrouter.ai/api/alpha/decisions' }); const r3 = await L.ask(p3, 'doctor', { text: 'hello there' }, q, policy.timeouts_ms.doctor);
      add(r3.ok ? 'warn' : 'FAIL', 'diagnosis: same request on /api/alpha/decisions', r3.ok ? 'the alpha Decisions route answers but /api/v1/systemone does not: set "endpoint" to the alpha URL in policy.json' : String(r3.why));
    }
  }
  const w = Math.max(...res.map((r) => r[1].length));
  for (const [l, a, d] of res) console.log(`${l.padEnd(4)} ${a.padEnd(w)}  ${d}`);
  process.exit(res.some((r) => r[0] === 'FAIL') ? 1 : 0);
})();
