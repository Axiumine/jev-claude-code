'use strict';
// jevlib.cjs - shared core for the Jev hooks and the Jev MCP server. Zero dependencies, Node >= 20 (global fetch).
// Invariants: (1) never throws to the caller for network/parse problems, (2) never emits "allow", (3) key only from a 0600 file owned
// by the user, (4) endpoint host is pinned to openrouter.ai and redirects are refused, (5) everything fails OPEN to the status quo,
// (6) every decision is logged, (7) trust boundary: config, key and state come ONLY from the passwd home. Nothing in the environment
// or in a repo (HOME, JEV_HOME, NODE_OPTIONS, proxies ... can all be set by a project's .claude/settings.json `env` block) can redirect them.
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

// argv is fixed by the user's own settings.json, so tests opt in to path overrides with an argv flag; no environment variable can.
const TEST_MODE = process.argv.includes('--jev-test');
function passwdHome() { try { return os.userInfo().homedir; } catch { return os.homedir(); } }
const HOME = passwdHome();
const CFG = (TEST_MODE && process.env.JEV_HOME) || path.join(HOME, '.config', 'jev');
const STATE = (TEST_MODE && process.env.JEV_STATE) || path.join(HOME, '.local', 'state', 'jev');

const DEFAULTS = {
  endpoint: 'https://openrouter.ai/api/v1/systemone',   // System One route: same request schema as /api/alpha/decisions, not tagged alpha in OpenRouter's OpenAPI
  model: 'typesafe/jev-1.13',                            // family pin (documented in OpenRouter's example). The response `model` is the dated snapshot.
  expected_model: null,                                  // after `doctor --live` set e.g. 'typesafe/jev-1.13-20260917'. A different response model = drift = no enforcement.
  provider: { data_collection: 'deny', zdr: true, allow_fallbacks: false },   // ProviderPreferences fields per OpenRouter OpenAPI; forced unless allow_non_zdr
  allow_non_zdr: false,
  env_allow: [],                                         // names from taintedEnv() the user has inspected and accepts (e.g. a corporate proxy)
  mode: { brake_t1: 'shadow', brake_t2: 'shadow', webscreen: 'log', triage: 'on' },
  t1_enforce: 'core',                                    // 'core' (catastrophic, rarely legitimate) | 'all' | ['rule_id', ...]
  t1_scope: 'subagents',                                 // 'subagents' = deny only where no human is watching (hook input has agent_id); main thread stays shadow. 'all' = also the main thread
  t2_enforce: [],                                        // [] = never; ['rule_id', ...] or '*'. Only after labelled shadow data.
  roots: { personal: [], client: [] },                   // absolute path prefixes (realpath-compared)
  egress: { personal: 'full', client: 'off', unknown: 'off' },   // full | skeleton | off  (project-derived text only)
  web_root_classes: ['personal', 'unknown'],             // WebFetch/WebSearch text is public: screened unless the root is 'client'
  web_trusted_hosts: [],                                 // hosts (or *.suffix) never screened, e.g. docs sites that ship 'for AI agents' text on purpose
  thresholds: {
    brake: { irreversible_min: 0.90, contained_max: 0.25, wanted_min: 0.60 },
    web: { steers_min: 0.85, plain_max: 0.50 },
    triage: { no_max: 0.05, yes_min: 0.95 },
  },
  timeouts_ms: { brake: 2500, web: 4000, triage_req: 6000, triage_total: 45000, doctor: 8000 },
  budget_usd_day: 0.50,
  nudge: { min_items: 30, max_per_session: 3, gap_s: 600 },   // jev-nudge hook: when to remind the model of jev_triage (advisory, sends nothing)
  breaker: { failures: 5, cooldown_s: 300 },
  allow_loopback_for_tests: false,
};
function deepMerge(a, b) {
  if (!b || typeof b !== 'object' || Array.isArray(b)) return a;
  const o = Array.isArray(a) ? a.slice() : Object.assign({}, a);
  for (const k of Object.keys(b)) o[k] = (a && typeof a[k] === 'object' && a[k] !== null && !Array.isArray(a[k]) && typeof b[k] === 'object' && b[k] !== null && !Array.isArray(b[k])) ? deepMerge(a[k], b[k]) : b[k];
  return o;
}
function privateFile(p) { try { const s = fs.statSync(p); return s.isFile() && (s.mode & 0o077) === 0 && s.uid === process.getuid(); } catch { return false; } }
const oneOf = (v, list, d) => (list.includes(v) ? v : d);
function loadPolicy() {
  let p = {};
  const f = path.join(CFG, 'policy.json');
  if (privateFile(f)) { try { p = JSON.parse(fs.readFileSync(f, 'utf8')); } catch { /* defaults */ } }   // not 0600 / not ours => defaults (no roots => no egress)
  const pol = deepMerge(DEFAULTS, p);
  pol.mode.brake_t1 = oneOf(pol.mode.brake_t1, ['off', 'shadow', 'enforce'], 'shadow');
  pol.mode.brake_t2 = oneOf(pol.mode.brake_t2, ['off', 'shadow', 'enforce'], 'shadow');
  pol.mode.webscreen = oneOf(pol.mode.webscreen, ['off', 'log', 'warn'], 'log');
  pol.mode.triage = oneOf(pol.mode.triage, ['off', 'on'], 'on');
  if (!Array.isArray(pol.t1_enforce)) pol.t1_enforce = oneOf(pol.t1_enforce, ['core', 'all'], 'core');
  if (!Array.isArray(pol.t2_enforce)) pol.t2_enforce = pol.t2_enforce === '*' ? '*' : [];
  pol.t1_scope = oneOf(pol.t1_scope, ['subagents', 'all'], 'subagents');
  if (!Array.isArray(pol.env_allow)) pol.env_allow = [];
  if (!Array.isArray(pol.web_root_classes)) pol.web_root_classes = ['personal', 'unknown'];
  if (!Array.isArray(pol.web_trusted_hosts)) pol.web_trusted_hosts = [];
  const nd = pol.nudge && typeof pol.nudge === 'object' ? pol.nudge : {};
  const num = (v, d, lo, hi) => (typeof v === 'number' && v >= lo && v <= hi ? v : d);
  pol.nudge = { min_items: num(nd.min_items, 30, 5, 400), max_per_session: num(nd.max_per_session, 3, 0, 50), gap_s: num(nd.gap_s, 600, 0, 86400) };
  if (!pol.allow_non_zdr) pol.provider = Object.assign({}, pol.provider, { data_collection: 'deny', zdr: true });   // privacy floor
  if (typeof pol.model !== 'string' || !/^[~\w./-]{3,80}$/.test(pol.model)) pol.model = DEFAULTS.model;
  return pol;
}
function killed(policy, component) {
  try { if (fs.existsSync(path.join(CFG, 'OFF'))) return true; } catch { /* ignore */ }
  const m = policy.mode || {};
  return m[component] === 'off';
}
function real(p) { try { return fs.realpathSync(p); } catch { return path.resolve(p); } }
// dir may be one path or a list (project dir from the environment + cwd from the hook input). Conservative combination: any client dir => client,
// all personal => personal, otherwise unknown. So an env value forged to look personal can never turn a client cwd into a personal one.
function rootClass(policy, dir) {
  const hit = (arr, d) => (arr || []).some((pfx) => { const q = real(pfx); return d === q || d.startsWith(q + path.sep); });
  const one = (x) => { const d = real(x || '/'); return hit(policy.roots.client, d) ? 'client' : hit(policy.roots.personal, d) ? 'personal' : 'unknown'; };
  const cs = (Array.isArray(dir) ? dir.filter(Boolean) : [dir]).map(one);
  if (cs.includes('client')) return 'client';
  return cs.length && cs.every((c) => c === 'personal') ? 'personal' : 'unknown';
}
function egressMode(policy, cls) { return (policy.egress && policy.egress[cls]) || 'off'; }

// ---------- redaction ----------
const RED = [
  [/sk-or-v1-[A-Za-z0-9]{16,}/g, '<key>'], [/sk-ant-[A-Za-z0-9_-]{16,}/g, '<key>'], [/\bsk-[A-Za-z0-9_-]{20,}/g, '<key>'],
  [/\bgh[pousr]_[A-Za-z0-9]{20,}/g, '<key>'], [/\bgithub_pat_[A-Za-z0-9_]{20,}/g, '<key>'], [/\bAKIA[0-9A-Z]{16}\b/g, '<key>'],
  [/\bxox[abprs]-[A-Za-z0-9-]{10,}/g, '<key>'], [/\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g, '<jwt>'],
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|$)/g, '<private-key>'],
  [/(Authorization:\s*(?:Bearer|Basic|Token)?\s*)[^\s'"]+/gi, '$1<redacted>'],
  [/(:\/\/[^\s:@/'"]+:)[^\s@/'"]+@/g, '$1<redacted>@'],
  [/(\b[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL)[A-Za-z0-9_]*=)(?:"[^"]*"|'[^']*'|\S+)/gi, '$1<redacted>'],
  [/(--?(?:password|passwd|pwd|token|secret|api-?key|apikey|auth)[= ])(?:"[^"]*"|'[^']*'|\S+)/gi, '$1<redacted>'],
  [/[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]{2,}/g, '<email>'],
  [/\b[A-Za-z0-9+/_-]{40,}={0,2}(?=$|[\s'"),;])/g, (m) => (/\d/.test(m) && /[A-Za-z]/.test(m) && !/\//.test(m.slice(1, -1)) ? '<blob>' : m)],
];
function redact(text) { let s = String(text == null ? '' : text); for (const [re, to] of RED) s = s.replace(re, to); return s; }
function abbreviate(text, root) {
  let s = String(text);
  if (root) s = s.split(root).join('<project>');
  return s.split(HOME).join('~');
}
function clip(text, head, tail) {
  const s = String(text); if (s.length <= head + (tail || 0) + 20) return s;
  return s.slice(0, head) + `\n…[${s.length - head - (tail || 0)} chars elided]…\n` + (tail ? s.slice(-tail) : '');
}

// ---------- logging / breaker / budget ----------
function ensureDir(d) { try { fs.mkdirSync(d, { recursive: true, mode: 0o700 }); } catch { /* ignore */ } }
function today() { return new Date().toISOString().slice(0, 10); }
function log(component, rec) {
  try {
    ensureDir(STATE);
    const f = path.join(STATE, `log-${today()}.jsonl`);
    fs.appendFileSync(f, JSON.stringify(Object.assign({ ts: new Date().toISOString(), c: component }, rec)) + '\n', { mode: 0o600 });
    if (Math.random() < 0.02) for (const n of fs.readdirSync(STATE)) if (/^(log|spend)-/.test(n) && Date.now() - fs.statSync(path.join(STATE, n)).mtimeMs > 30 * 864e5) fs.unlinkSync(path.join(STATE, n));
  } catch { /* logging must never break a hook */ }
}
function readJson(f, dflt) { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch { return dflt; } }
function writeJson(f, o) { try { ensureDir(path.dirname(f)); const t = f + '.' + process.pid + '.tmp'; fs.writeFileSync(t, JSON.stringify(o), { mode: 0o600 }); fs.renameSync(t, f); } catch { /* ignore */ } }
function breakerOpen() { const b = readJson(path.join(STATE, 'breaker.json'), {}); return b.until && Date.now() < b.until; }
function recordResult(policy, ok) {
  const f = path.join(STATE, 'breaker.json'); const b = readJson(f, { fails: 0 });
  if (ok) { if (b.fails) writeJson(f, { fails: 0 }); return; }
  b.fails = (b.fails || 0) + 1;
  if (b.fails >= policy.breaker.failures) { b.until = Date.now() + policy.breaker.cooldown_s * 1000; b.fails = 0; log('breaker', { open_s: policy.breaker.cooldown_s }); }
  writeJson(f, b);
}
function spendToday() { return readJson(path.join(STATE, `spend-${today()}.json`), { usd: 0 }).usd || 0; }
function addSpend(usd) { const f = path.join(STATE, `spend-${today()}.json`); const s = readJson(f, { usd: 0 }); s.usd = (s.usd || 0) + (Number(usd) || 0); writeJson(f, s); }
// true the first time an id is seen: several `if` handlers of one PostToolUse group can match the same tool call
function once(id) {
  const safe = String(id || '').replace(/[^\w-]/g, '').slice(0, 64); if (!safe) return true;
  try {
    const d = path.join(STATE, 'seen'); ensureDir(d);
    fs.closeSync(fs.openSync(path.join(d, safe), 'wx', 0o600));
    if (Math.random() < 0.02) for (const n of fs.readdirSync(d)) if (Date.now() - fs.statSync(path.join(d, n)).mtimeMs > 3600e3) fs.unlinkSync(path.join(d, n));
    return true;
  } catch (e) { return !(e && e.code === 'EEXIST'); }
}

// ---------- key + endpoint + environment ----------
// The key lives in ~/.config/jev/ + a dotfile name the user's secret-leak guard and deny rules already protect (verified: the guard blocks
// Read/Grep/cat on a file named like the dotenv convention but NOT on 'openrouter.key'). Line format: OPENROUTER_API_KEY=sk-or-...
const KEY_FILE = '.' + 'env';
function readKey() {
  const f = path.join(CFG, KEY_FILE);
  try {
    const st = fs.statSync(f);
    if ((st.mode & 0o077) || st.uid !== process.getuid()) return { err: 'key_perms' };
    const m = /^[ \t]*(?:export[ \t]+)?OPENROUTER_API_KEY[ \t]*=[ \t]*["']?(sk-or-[A-Za-z0-9_-]{20,})["']?[ \t]*$/m.exec(fs.readFileSync(f, 'utf8'));
    return m ? { key: m[1] } : { err: 'key_format' };
  } catch { return { err: 'nokey' }; }
}
function endpointOk(policy) {
  try {
    const u = new URL(policy.endpoint);
    if (u.protocol === 'https:' && u.hostname === 'openrouter.ai') return true;
    if (TEST_MODE && policy.allow_loopback_for_tests && u.protocol === 'http:' && u.hostname === '127.0.0.1') return true;
  } catch { /* fallthrough */ }
  return false;
}
// A repo-controlled settings `env` block can point Node at attacker code, CAs or proxies. If one of these is set, send nothing.
// (Node's fetch ignores HTTP(S)_PROXY unless NODE_USE_ENV_PROXY is set, so the proxy names only count together with it.)
const TAINT_ANY = ['NODE_PATH', 'NODE_EXTRA_CA_CERTS', 'NODE_REPL_EXTERNAL_MODULE', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NODE_USE_ENV_PROXY'];
const TAINT_PROXY = ['HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy'];
const NODE_OPT_BAD = /(?:^|\s)(?:-r\b|--require\b|--import\b|--loader\b|--experimental-loader\b|--env-file\b|--openssl|--use-openssl-ca|--use-system-ca|--use-env-proxy|--tls-)/;
function taintedEnv(policy) {
  const allow = (policy && policy.env_allow) || []; const e = process.env; const t = [];
  for (const k of TAINT_ANY) if (e[k] && !allow.includes(k)) t.push(k);
  if (e.NODE_USE_ENV_PROXY || /--use-env-proxy/.test(e.NODE_OPTIONS || '')) for (const k of TAINT_PROXY) if (e[k] && !allow.includes(k)) t.push(k);
  if (e.NODE_TLS_REJECT_UNAUTHORIZED === '0' && !allow.includes('NODE_TLS_REJECT_UNAUTHORIZED')) t.push('NODE_TLS_REJECT_UNAUTHORIZED');
  if (e.NODE_OPTIONS && NODE_OPT_BAD.test(e.NODE_OPTIONS) && !allow.includes('NODE_OPTIONS')) t.push('NODE_OPTIONS');
  return [...new Set(t)];
}
const isProb = (x) => typeof x === 'number' && x >= 0 && x <= 1 && Number.isFinite(x);

// ask(): one Decisions/System One request. Returns {ok:true, answers, model, drift, cost, ms} or {ok:false, why}. Never throws.
async function ask(policy, component, state, questions, deadlineMs, extra) {
  const t0 = Date.now();
  try {
    if (!endpointOk(policy)) return { ok: false, why: 'endpoint_not_allowed' };
    const tainted = taintedEnv(policy); if (tainted.length) return { ok: false, why: 'env_tainted:' + tainted.join(',') };
    if (breakerOpen()) return { ok: false, why: 'breaker_open' };
    if (spendToday() >= policy.budget_usd_day) return { ok: false, why: 'budget_exhausted' };
    const k = readKey(); if (!k.key) return { ok: false, why: k.err };
    const body = Object.assign({ model: policy.model, state, questions }, policy.provider ? { provider: policy.provider } : {}, extra || {});
    const ctl = new AbortController(); const timer = setTimeout(() => ctl.abort(), deadlineMs);
    try {
      const res = await fetch(policy.endpoint, { method: 'POST', redirect: 'error', headers: { Authorization: `Bearer ${k.key}`, 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: ctl.signal });
      if (!res.ok) { try { await res.body.cancel(); } catch { /* ignore */ } recordResult(policy, false); return { ok: false, why: 'http_' + res.status, ms: Date.now() - t0 }; }
      const j = await res.json();
      const answers = {};
      for (const [id, q] of Object.entries(questions)) {
        const a = j && j.answers && j.answers[id];
        if (!a) return { ok: false, why: 'missing_answer_' + id, ms: Date.now() - t0 };
        if (q.type === 'noul') { if (!isProb(a.noul)) return { ok: false, why: 'bad_noul_' + id, ms: Date.now() - t0 }; answers[id] = { noul: a.noul }; }
        else if (q.type === 'choice') {
          const opts = Object.keys(q.criteria || {});
          if (typeof a.choice !== 'string' || !opts.includes(a.choice) || !a.probabilities || !opts.every((o) => isProb(a.probabilities[o] === undefined ? 0 : a.probabilities[o]))) return { ok: false, why: 'bad_choice_' + id, ms: Date.now() - t0 };
          answers[id] = { choice: a.choice, probabilities: a.probabilities, confidence: a.confidence };
        }
      }
      const cost = j && j.usage && typeof j.usage.cost === 'number' ? j.usage.cost : ((j && j.usage && j.usage.input_tokens) || 0) * 0.042e-6;
      addSpend(cost); recordResult(policy, true);
      const drift = !!(policy.expected_model && j.model !== policy.expected_model);
      return { ok: true, answers, model: j.model, drift, cost, ms: Date.now() - t0, tokens: j.usage && j.usage.input_tokens };
    } finally { clearTimeout(timer); }
  } catch (e) {
    recordResult(policy, false);
    return { ok: false, why: e && e.name === 'AbortError' ? 'timeout' : 'error:' + String(e && e.message).slice(0, 60), ms: Date.now() - t0 };
  }
}

// ---------- context helpers ----------
function watchdog(ms) { const t = setTimeout(() => process.exit(0), ms); if (t.unref) t.unref(); }
function readStdinJson() { try { return JSON.parse(fs.readFileSync(0, 'utf8')); } catch { return null; } }
function tailLines(file, bytes) {
  try {
    const st = fs.statSync(file); const fd = fs.openSync(file, 'r'); const len = Math.min(st.size, bytes); const b = Buffer.alloc(len);
    fs.readSync(fd, b, 0, len, st.size - len); fs.closeSync(fd);
    const lines = b.toString('utf8').split('\n'); if (st.size > len) lines.shift(); return lines;
  } catch { return []; }
}
function lastUserRequest(transcriptPath, maxChars) {
  const lines = tailLines(transcriptPath, 400 * 1024);
  for (let i = lines.length - 1; i >= 0; i--) {
    if (!lines[i]) continue; let o; try { o = JSON.parse(lines[i]); } catch { continue; }
    if (o.type !== 'user' || o.isMeta || o.isSidechain || !o.message) continue;
    const c = o.message.content; let text = '';
    if (typeof c === 'string') text = c;
    else if (Array.isArray(c)) { if (c.some((b) => b && b.type === 'tool_result')) continue; text = c.filter((b) => b && b.type === 'text').map((b) => b.text).join('\n'); }
    text = text.trim(); if (!text || text.startsWith('<') || text.startsWith('[Request interrupted')) continue;
    return redact(text).slice(0, maxChars || 400);
  }
  return undefined;
}
function gitBin() { for (const p of ['/usr/bin/git', '/usr/local/bin/git', '/bin/git']) if (fs.existsSync(p)) return p; return 'git'; }
// git runs in untrusted repos with a scrubbed environment: no GIT_CONFIG_*/GIT_DIR from a project env block, no fsmonitor or hooks from the
// repo's own config, and no optional index lock (a hook must never make a concurrent `git commit` of the agent fail with index.lock).
const GIT_ENV = { PATH: '/usr/bin:/bin:/usr/local/bin', HOME, LC_ALL: 'C', GIT_OPTIONAL_LOCKS: '0', GIT_TERMINAL_PROMPT: '0' };
function git(cwd, args, timeout) {
  return spawnSync(gitBin(), ['--no-optional-locks', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null', '-C', cwd].concat(args), { encoding: 'utf8', timeout, env: GIT_ENV });
}
function gitFacts(cwd, opt) {
  const run = (args) => git(cwd, args, 1200);
  const top = run(['rev-parse', '--show-toplevel', '--abbrev-ref', 'HEAD']);
  if (top.status !== 0) return { git: false };
  const [topdir, branch] = top.stdout.trim().split('\n');
  const dirty = run(['status', '--porcelain=v1', '--untracked-files=no']);
  const f = { git: true, top: topdir || '', branch: branch || '', dirty_tracked: dirty.status === 0 ? dirty.stdout.trim().length > 0 : null, untracked: null };
  if (opt && opt.untracked) { const unt = run(['ls-files', '--others', '--exclude-standard', '--directory', '--no-empty-directory']); f.untracked = unt.status === 0 ? unt.stdout.trim().length > 0 : null; }
  return f;
}
function familyRoots(cwd) {
  try {
    const out = []; const run = (a) => git(cwd, a, 1500);
    const top = run(['rev-parse', '--show-toplevel']); if (top.status === 0) out.push(top.stdout.trim());
    const wt = run(['worktree', 'list', '--porcelain']);
    if (wt.status === 0) for (const ln of wt.stdout.split('\n')) if (ln.startsWith('worktree ')) out.push(ln.slice(9).trim());
    return [...new Set(out)].filter(Boolean);
  } catch { return []; }
}
function createdFile(session) { return path.join(STATE, 'created', String(session || 'nosession').replace(/[^\w-]/g, '').slice(0, 64) + '.json'); }
function loadCreated(session) { return readJson(createdFile(session), []).filter((x) => typeof x === 'string'); }
function saveCreated(session, dirs) {
  if (!dirs || !dirs.length) return;
  const cur = loadCreated(session); const merged = [...new Set(cur.concat(dirs))].slice(-300); writeJson(createdFile(session), merged);
  if (Math.random() < 0.02) try { const d = path.join(STATE, 'created'); for (const n of fs.readdirSync(d)) if (Date.now() - fs.statSync(path.join(d, n)).mtimeMs > 7 * 864e5) fs.unlinkSync(path.join(d, n)); } catch { /* ignore */ }
}
module.exports = { KEY_FILE, TEST_MODE, HOME, watchdog, loadCreated, saveCreated, familyRoots, CFG, STATE, DEFAULTS, loadPolicy, killed, rootClass, egressMode, redact, abbreviate, clip, log, ask, readKey, readStdinJson, lastUserRequest, gitFacts, isProb, readJson, writeJson, spendToday, tailLines, endpointOk, privateFile, taintedEnv, once, real };
