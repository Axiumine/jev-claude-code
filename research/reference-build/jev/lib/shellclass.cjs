'use strict';
// shellclass.cjs - deterministic Bash command classifier for the jev-brake hook.
// Pure functions, zero deps. Tiers:
//   T0  nothing risky recognised            -> pass, no network call
//   T1  catastrophic / never-unattended      -> deny (no Jev call; ack path exists)
//   T2  gray: might be destructive           -> Jev adjudicates (deny only if Jev is sure)
// A hit may be conditional (needs: 'dirty_tracked' | 'untracked'): the hook resolves it with local git facts.
// Design rule: unknown programs are T0. Only patterns we recognise can raise a tier (default = status quo).
const path = require('path');
const os = require('os');
const fs = require('fs');

const REGEN = new Set(['node_modules', 'dist', 'build', 'out', '.next', '.nuxt', '.turbo', '.cache', 'coverage', 'target',
  '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.venv', 'venv', '.parcel-cache', '.svelte-kit', 'tmp', 'temp',
  '.tmp', '.gradle', '.angular', 'storybook-static', 'playwright-report', 'test-results', '.vite', '.expo', '.dart_tool', '.nyc_output']);
const PROTECTED_BRANCH = /^(main|master|prod|production|release[\w./-]*|trunk|live|stable)$/i;
const WRAPPERS = new Set(['command', 'builtin', 'exec', 'nohup', 'time', 'nice', 'setsid', 'stdbuf', 'ionice', 'chrt', 'unbuffer']);
const RUNNERS = new Set(['npx', 'bunx', 'pnpx', 'pnpm', 'yarn', 'uv', 'poetry', 'pipenv', 'rye', 'pdm', 'bundle', 'dotenv', 'doppler', 'op']);
const INTERP = new Set(['python', 'python3', 'node', 'bun', 'deno', 'ruby', 'perl', 'php', 'bash', 'sh', 'zsh', 'dash', 'tsx', 'ts-node']);
const SQL_CLI = new Set(['psql', 'mysql', 'mariadb', 'sqlite3', 'mongosh', 'mongo', 'redis-cli', 'clickhouse-client', 'cqlsh', 'pgcli', 'usql']);
const CLOUD = new Set(['aws', 'gcloud', 'az', 'doctl', 'heroku', 'fly', 'flyctl', 'vercel', 'wrangler', 'netlify', 'firebase', 'supabase',
  'terraform', 'tofu', 'pulumi', 'kubectl', 'helm', 'k3s', 'oc', 'ansible', 'ansible-playbook', 'cdk', 'sam', 'serverless', 'sls', 'patronictl', 'patroni']);
const CLOUD_DESTRUCT = /\b(delete|destroy|terminate|remove|rm|rb|rmdir|purge|deploy|publish|release|promote|rollback|reset|uninstall|drop|apply|drain|reinit|failover|switchover|force-reset|db\s+push)\b/i;

const CODE_DESTRUCT = [
  /shutil\.rmtree|\brmtree\(/, /os\.(remove|unlink|rmdir|removedirs)\(/, /\.unlink\(/, /Path\([^)]*\)\.(unlink|rmdir)/,
  /\.(rm|rmSync|unlink|unlinkSync|rmdir|rmdirSync)\s*\(/, /requests\.delete\(/, /method\s*[:=]\s*['"]DELETE['"]/i,
  /\brm\s+(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b/, /git\s+(push\s+(-f|--force)|reset\s+--hard|clean\s+-\w*f)/, /\bkubectl\s+delete\b/, /\bterraform\s+destroy\b/,
  /\bDROP\s+(TABLE|DATABASE|SCHEMA|INDEX|USER|ROLE)\b/i, /\bTRUNCATE\b/i, /\.drop_all\(|\.dropDatabase\(|\bFLUSHALL\b|\bFLUSHDB\b/i,
];
const SQL_DROP = [/\bDROP\s+(DATABASE|SCHEMA)\b/i, /\bFLUSHALL\b/i, /\bFLUSHDB\b/i, /\bdropDatabase\s*\(/i];
const SQL_T2 = [/\bTRUNCATE\b/i, /\bDROP\s+(TABLE|INDEX|VIEW|COLUMN|CONSTRAINT|EXTENSION|TRIGGER|FUNCTION|USER|ROLE)\b/i, /\bALTER\s+TABLE\s+\S+\s+DROP\b/i,
  /\b(deleteMany|remove)\s*\(\s*\{\s*\}\s*\)/, /\.drop\s*\(\s*\)/];
const REDIS_T2 = [/^\s*(UNLINK|CONFIG\s+SET|SHUTDOWN|SLAVEOF|REPLICAOF|DEL)\b/im, /\b(redis-cli|valkey-cli)\b[^\n]*\b(UNLINK|CONFIG\s+SET|SHUTDOWN|SLAVEOF|REPLICAOF)\b/i];
function sqlNoWhere(text) { // linear scan of ';'-separated statements: DELETE FROM x / UPDATE x SET without WHERE
  if (text.length > 60000) text = text.slice(0, 60000);
  for (const st of text.split(';')) {
    if (/\bDELETE\s+FROM\s+[\w."`\[\]-]+/i.test(st) && !/\bWHERE\b/i.test(st)) return 'DELETE FROM (no WHERE)';
    if (/\bUPDATE\s+[\w."`\[\]-]+\s+SET\b/i.test(st) && !/\bWHERE\b/i.test(st)) return 'UPDATE (no WHERE)';
  }
  return null;
}
// ---------- lexer ----------
function balanced(s, openIdx) { // s[openIdx] === '('
  let depth = 0;
  for (let i = openIdx; i < s.length; i++) {
    if (s[i] === '(') depth++;
    else if (s[i] === ')') { depth--; if (depth === 0) return { inner: s.slice(openIdx + 1, i), end: i }; }
  }
  return { inner: s.slice(openIdx + 1), end: s.length };
}

function lex(cmd, depth = 0) {
  const heredocs = [];
  const src = String(cmd).replace(/(?<!<)(<<-?\s*(['"]?)([A-Za-z_]\w*)\2)([^\n]*)\n([\s\S]*?)(?:\n[ \t]*\3[ \t]*(?=\n|$)|$)/g,
    (m, hd, q, tag, rest, body) => { heredocs.push(body); return ' __HEREDOC__' + rest + '\n'; });
  const segs = []; const subs = [];
  let toks = [], cur = '', has = false, q = null;
  const pushTok = () => { if (has) toks.push(cur); cur = ''; has = false; };
  const pushSeg = () => { pushTok(); if (toks.length) segs.push(toks); toks = []; };
  for (let i = 0; i < src.length; i++) {
    const c = src[i], n = src[i + 1];
    if (q === "'") { if (c === "'") q = null; else { cur += c; has = true; } continue; }
    if (q === '"') {
      if (c === '\\' && n) { cur += n; has = true; i++; continue; }
      if (c === '"') { q = null; continue; }
      if (c === '$' && n === '(') { const b = balanced(src, i + 1); subs.push(b.inner); i = b.end; cur += '$SUB'; has = true; continue; }
      if (c === '`') { const e = src.indexOf('`', i + 1); if (e > 0) { subs.push(src.slice(i + 1, e)); i = e; cur += '$SUB'; has = true; continue; } }
      cur += c; has = true; continue;
    }
    if (c === '\\' && n) { if (n === '\n') { i++; continue; } cur += n; has = true; i++; continue; }
    if (c === "'" || c === '"') { q = c; has = true; continue; }
    if (c === '$' && n === '(') { const b = balanced(src, i + 1); subs.push(b.inner); i = b.end; cur += '$SUB'; has = true; continue; }
    if (c === '`') { const e = src.indexOf('`', i + 1); if (e > 0) { subs.push(src.slice(i + 1, e)); i = e; cur += '$SUB'; has = true; continue; } }
    if ((c === '<' || c === '>') && n === '(') { const b = balanced(src, i + 1); subs.push(b.inner); i = b.end; cur += '$SUB'; has = true; continue; }
    if (c === '#' && !has && (i === 0 || /\s/.test(src[i - 1]))) { while (i < src.length && src[i] !== '\n') i++; pushSeg(); continue; }
    if (c === ' ' || c === '\t' || c === '\r') { pushTok(); continue; }
    if (c === '\n' || c === ';' || c === '(' || c === ')' || c === '{' || c === '}') { pushSeg(); continue; }
    if (c === '&' || c === '|') {
      if (c === '&' && (n === '>' )) { pushTok(); toks.push('>'); i += (src[i + 2] === '>') ? 2 : 1; continue; }
      if (c === '&' && (has && /^\d*[<>]$/.test(cur))) { cur += c; continue; }
      pushSeg(); if (src[i + 1] === c) i++; continue;
    }
    if (c === '>') {
      if (n === '&') { cur += '>&'; has = true; i++; continue; } // 2>&1 style fd dup, keep in token
      if (/^\d+$/.test(cur)) { cur = ''; has = false; }
      pushTok(); toks.push(n === '>' ? '>>' : '>'); if (n === '>') i++; continue;
    }
    if (c === '<') { pushTok(); toks.push('<'); continue; }
    cur += c; has = true;
  }
  pushSeg();
  if (depth < 4) for (const s of subs) { const r = lex(s, depth + 1); segs.push(...r.segs); heredocs.push(...r.heredocs); }
  return { segs, heredocs };
}

// ---------- helpers ----------
function stripPrefix(toks, hits) {
  let t = toks.slice(); let sudo = false; let vcwdCd = null;
  for (let guard = 0; guard < 12 && t.length; guard++) {
    const first = t[0];
    const base = path.basename(first);
    if (/^(do|then|else|elif|if|while|until|!)$/.test(first)) { t.shift(); continue; }
    if (/^(for|case|select|function|done|fi|esac|in)$/.test(first)) return { toks: [], sudo };
    if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(first)) { t.shift(); continue; }
    if (base === 'sudo' || base === 'doas') { sudo = true; t.shift(); while (t[0] && /^-/.test(t[0])) t.shift(); continue; }
    if (base === 'env') { t.shift(); while (t[0] && (/^-/.test(t[0]) || /^[A-Za-z_][A-Za-z0-9_]*=/.test(t[0]))) t.shift(); continue; }
    if (base === 'timeout') { t.shift(); while (t[0] && /^-/.test(t[0])) t.shift(); if (t[0] && /^\d/.test(t[0])) t.shift(); continue; }
    if (WRAPPERS.has(base)) { t.shift(); while (t[0] && /^-/.test(t[0])) t.shift(); continue; }
    if (base === 'xargs') { t.shift(); while (t[0] && /^-/.test(t[0])) { if (/^-(I|n|P|L|d|E|s)$/.test(t[0])) t.shift(); t.shift(); } hits.xargs = true; continue; }
    if (RUNNERS.has(base) && t[1] && (t[1] === 'run' || t[1] === 'exec' || base === 'npx' || base === 'bunx' || base === 'pnpx' || t[1] === 'dlx' || t[1] === 'x')) {
      if (base === 'pnpm' && t[1] === 'exec') { t = t.slice(2); continue; }
      if (t[1] === 'run' && (base === 'pnpm' || base === 'yarn')) break; // package script, opaque: leave as is
      t.shift(); if (t[0] === 'run' || t[0] === 'exec' || t[0] === 'dlx' || t[0] === 'x') t.shift(); while (t[0] && /^-/.test(t[0])) t.shift(); continue;
    }
    break;
  }
  return { toks: t, sudo };
}

function expandHome(p) {
  const home = os.homedir();
  if (p === '~' || p === '$HOME' || p === '${HOME}') return home;
  if (p.startsWith('~/')) return path.join(home, p.slice(2));
  if (p.startsWith('$HOME/')) return path.join(home, p.slice(6));
  if (p.startsWith('${HOME}/')) return path.join(home, p.slice(8));
  return p;
}
function resolveTarget(tok, vcwd, ctx) {
  const roots = ctx.roots; const created = ctx.created || [];
  if (tok.includes('$SUB') || (/\$/.test(tok) && !/^(\$HOME|\$\{HOME\})(\/|$)/.test(tok))) return { unknown: true };
  const glob = /[*?[]/.test(tok);
  const abs = path.resolve(vcwd, expandHome(tok));
  const home = os.homedir();
  const inProject = roots.some((r) => abs === r || abs.startsWith(r + path.sep));
  const atRoot = roots.includes(abs);
  const inCreated = created.some((c) => abs === c || abs.startsWith(c + path.sep));
  const uid = typeof process.getuid === 'function' ? process.getuid() : -1;
  const safeTmp = inCreated || abs.startsWith('/tmp/') || abs.startsWith('/var/tmp/') || abs.startsWith('/dev/shm/') || abs.startsWith('/run/user/' + uid + '/') || abs.startsWith(os.tmpdir() + path.sep) || abs.startsWith(path.join(home, '.cache') + path.sep) || abs === '/dev/null';
  return { abs, glob, inProject, atRoot, safeTmp, unknown: false, isHome: abs === home, isRootish: abs === '/' || /^\/(home|media|mnt|etc|usr|var|opt|boot|bin|sbin|lib\d*|root|srv|run|sys|proc|dev)?\/?$/.test(abs) };
}
function insideRegen(abs, roots) {
  const root = roots.find((r) => abs === r || abs.startsWith(r + path.sep)) || roots[0];
  const rel = path.relative(root, abs).split(path.sep);
  return rel.some((seg) => REGEN.has(seg));
}
function homes() { const out = new Set([os.homedir()]); try { out.add(os.userInfo().homedir); } catch { /* ignore */ } return [...out]; }
function isProtectedPath(abs) {
  return homes().some((h) => (/^settings[\w.-]*\.json$/.test(path.basename(abs)) && path.dirname(abs) === path.join(h, '.claude')) ||
    abs.startsWith(path.join(h, '.claude', 'jev') + path.sep) || abs.startsWith(path.join(h, '.claude', 'hooks') + path.sep) ||
    abs.startsWith(path.join(h, '.config', 'jev') + path.sep) || abs === path.join(h, '.config', 'jev'));
}
function pushHit(res, tier, id, detail, needs) { res.hits.push({ tier, id, detail: detail || '', needs: needs || null }); }
function hasFlag(args, re) { return args.some((a) => /^-/.test(a) && re.test(a)); }

function readSmall(p, max = 65536) {
  try { const st = fs.statSync(p); if (!st.isFile() || st.size > 2 * 1024 * 1024) return null; const fd = fs.openSync(p, 'r'); const b = Buffer.alloc(Math.min(st.size, max)); fs.readSync(fd, b, 0, b.length, 0); fs.closeSync(fd); return b.toString('utf8'); } catch { return null; }
}
function codeWindows(text, patterns, max = 5) {
  const out = []; const kws = new Set();
  for (const re of patterns) {
    const g = new RegExp(re.source, re.flags.includes('g') ? re.flags : re.flags + 'g');
    let m; let n = 0;
    while ((m = g.exec(text)) && n < 2) { kws.add(m[0].slice(0, 40)); if (out.length < max) out.push(text.slice(Math.max(0, m.index - 100), Math.min(text.length, m.index + m[0].length + 140))); n++; if (m.index === g.lastIndex) g.lastIndex++; }
  }
  return { kws: [...kws], windows: out };
}

// ---------- per-segment rules ----------
function classifySegment(rawToks, ctx, res) {
  const { toks: tk, sudo } = stripPrefix(rawToks, res);
  if (!tk.length) return;
  const prog = path.basename(tk[0]).replace(/\.(exe|sh)$/, '');
  const args = [];
  for (let i = 1; i < tk.length; i++) { if (tk[i] === '<' || tk[i] === '>' || tk[i] === '>>') { i++; continue; } if (/^\d*>&\d*-?$/.test(tk[i])) continue; args.push(tk[i]); }
  const root = ctx.root, roots = ctx.roots, vcwd = ctx.vcwd;
  if (sudo) pushHit(res, 'T2', 'sudo', prog);

  // redirects / tee / writes outside project
  for (let i = 0; i < tk.length; i++) {
    if ((tk[i] === '>' || tk[i] === '>>') && tk[i + 1]) {
      const r = resolveTarget(tk[i + 1], vcwd, ctx);
      if (!r.unknown && !r.inProject && !r.safeTmp && !/^\/dev\/(null|stdout|stderr|tty|fd\/)/.test(r.abs)) {
        if (/^\/dev\/(sd|nvme|hd|vd|mmcblk)/.test(r.abs)) pushHit(res, 'T1', 'redirect_to_block_device', r.abs);
        else pushHit(res, 'T2', 'write_outside_project', 'redirect');
      }
    }
  }
  if (prog === 'tee') for (const a of args.filter((x) => !/^-/.test(x))) { const r = resolveTarget(a, vcwd, ctx); if (!r.unknown && !r.inProject && !r.safeTmp && !/^\/dev\//.test(r.abs)) pushHit(res, 'T2', 'write_outside_project', 'tee'); }

  switch (prog) {
    case 'cd': { const a = args.find((x) => !/^-/.test(x)); if (a) { const r = resolveTarget(a, vcwd, ctx); if (!r.unknown) ctx.vcwd = r.abs; } return; }
    case 'rm': case 'rmdir': case 'unlink': case 'shred': {
      const recursive = hasFlag(args, /[rR]|recursive/) || prog === 'rmdir';
      const targets = args.filter((a) => !/^-/.test(a));
      if (res.xargs && !targets.length && recursive) pushHit(res, 'T2', 'rm_unresolved_target', 'xargs');
      for (const t of targets) {
        const r = resolveTarget(t, vcwd, ctx);
        if (r.unknown) { if (recursive || /[*?[]/.test(t)) pushHit(res, 'T2', 'rm_unresolved_target', 'variable or substitution'); continue; }
        if (r.isHome || r.isRootish) { pushHit(res, 'T1', 'rm_root_or_home', r.abs); continue; }
        if (!r.inProject && !r.safeTmp) {
          const derived = roots.some((rt) => path.dirname(r.abs) === path.dirname(rt) && path.basename(r.abs).startsWith(path.basename(rt)));
          if (recursive && derived) pushHit(res, 'T2', 'rm_recursive_sibling_derived', path.basename(r.abs).slice(path.basename(root).length));
          else pushHit(res, recursive ? 'T1' : 'T2', recursive ? 'rm_recursive_outside_project' : 'rm_outside_project', r.abs);
          continue;
        }
        if (r.inProject) {
          const base = path.basename(r.abs);
          if (base === '.git') { pushHit(res, recursive ? 'T1' : 'T2', 'rm_dot_git', base); continue; }
          if (r.atRoot || r.abs === vcwd || (r.glob && roots.includes(path.dirname(r.abs)))) { if (recursive) pushHit(res, 'T1', 'rm_project_root_contents', base, 'uncommitted_any'); else pushHit(res, 'T2', 'rm_project_root_contents', base); continue; }
          if (recursive && !REGEN.has(base) && !insideRegen(r.abs, roots)) pushHit(res, 'T2', 'rm_recursive_project_path', path.relative(roots.find((rt) => r.abs.startsWith(rt + path.sep)) || roots[0], r.abs).split(path.sep)[0]);
        }
      }
      return;
    }
    case 'find': {
      if (args.includes('-delete') || (args.includes('-exec') && /\b(rm|shred|unlink)\b/.test(args.join(' ')))) {
        const start = args.find((a) => !/^-/.test(a)) || '.';
        const r = resolveTarget(start, vcwd, ctx);
        const filtered = args.some((a) => /^-(name|iname|path|ipath|regex|iregex|type|mtime|mmin|newer|size|user|perm|empty)$/.test(a));
        if (!r.unknown && (r.isHome || r.isRootish || (!r.inProject && !r.safeTmp)) && !filtered) pushHit(res, 'T1', 'find_delete_wide', start);
        else pushHit(res, 'T2', 'find_delete', start);
      }
      return;
    }
    case 'dd': { if (args.some((a) => /^of=\/dev\//.test(a) && !/^of=\/dev\/(null|zero|stdout|stderr)/.test(a))) pushHit(res, 'T1', 'dd_to_device'); else if (args.some((a) => /^of=/.test(a))) pushHit(res, 'T2', 'dd_write'); return; }
    case 'mkfs': case 'fdisk': case 'parted': case 'wipefs': case 'blkdiscard': case 'sfdisk': case 'cryptsetup': case 'lvremove': case 'vgremove': case 'zpool': case 'zfs': pushHit(res, 'T1', 'disk_admin', prog); return;
    case 'truncate': pushHit(res, 'T2', 'truncate'); return;
    case 'reboot': case 'shutdown': case 'poweroff': case 'halt': pushHit(res, 'T1', 'power', prog); return;
    case 'kill': case 'pkill': case 'killall': {
      const tgt = args.filter((a) => !/^-/.test(a));
      if (tgt.includes('1') || tgt.includes('-1') || args.includes('-1')) pushHit(res, 'T1', 'kill_all');
      else if (tgt.some((a) => /^(node|nodejs|python3?|claude|code|tmux|screen|ssh|sshd|bash|zsh|systemd|dockerd|containerd|postgres|postgresql|redis-server|nginx|chrome|firefox|Xorg|gnome-shell)$/.test(a))) pushHit(res, 'T2', 'kill_generic_process', tgt[0]);
      return;
    }
    case 'systemctl': case 'service': if (args.some((a) => /^(stop|disable|mask|restart|kill|reboot|poweroff)$/.test(a))) pushHit(res, 'T2', 'service_control', args[0]); return;
    case 'crontab': if (args.includes('-r')) pushHit(res, 'T1', 'crontab_remove'); return;
    case 'chmod': case 'chown': case 'chgrp': {
      if (hasFlag(args, /R|recursive/)) { const t = args.filter((a) => !/^-/.test(a)).slice(1); for (const x of t) { const r = resolveTarget(x, vcwd, ctx); if (r.unknown || (!r.inProject && !r.safeTmp)) pushHit(res, (!r.unknown && (r.isHome || r.isRootish)) ? 'T1' : 'T2', 'chmod_recursive_outside', x); } }
      return;
    }
    case 'mv': case 'cp': case 'ln': case 'rsync': case 'install': {
      const tg = args.filter((a) => !/^-/.test(a));
      if (prog === 'rsync' && hasFlag(args, /--delete/)) pushHit(res, 'T2', 'rsync_delete');
      const outside = tg.some((x) => { const r = resolveTarget(x, vcwd, ctx); return !r.unknown && !r.inProject && !r.safeTmp && !/^\/dev\//.test(r.abs); });
      if (outside && (prog === 'mv' || prog === 'rsync')) pushHit(res, 'T2', prog + '_outside_project');
      else if (outside && tg.length && (() => { const r = resolveTarget(tg[tg.length - 1], vcwd, ctx); return !r.unknown && !r.inProject && !r.safeTmp; })()) pushHit(res, 'T2', 'write_outside_project', prog);
      return;
    }
    case 'sed': case 'perl': case 'awk': case 'gawk': {
      if (hasFlag(args, /^-[a-zA-Z]*i|--in-place|-pi/) || args.includes('inplace')) {
        for (const a of args.filter((x) => !/^-/.test(x)).slice(1)) { const r = resolveTarget(a, vcwd, ctx); if (!r.unknown && !r.inProject && !r.safeTmp && /[./]/.test(a)) { pushHit(res, 'T2', 'inplace_edit_outside_project'); break; } }
      }
      return;
    }
    case 'git': return classifyGit(args, ctx, res);
    case 'gh': {
      const [a, b] = args.filter((x) => !/^-/.test(x));
      if (a === 'repo' && /^(delete|archive|rename|transfer|edit)$/.test(b || '')) pushHit(res, 'T1', 'gh_repo_admin', b);
      else if (a === 'pr' && b === 'merge') pushHit(res, 'T2', 'gh_pr_merge');
      else if (a === 'release' && /^(create|delete|edit)$/.test(b || '')) pushHit(res, 'T2', 'gh_release', b);
      else if (a === 'workflow' && b === 'run') pushHit(res, 'T2', 'gh_workflow_run');
      else if (a === 'secret' || a === 'variable') pushHit(res, 'T2', 'gh_secret_var', b);
      else if (a === 'api' && (hasFlag(args, /^-X$|^--method$/) && args.some((x) => /^(POST|PUT|PATCH|DELETE)$/i.test(x)) || args.some((x) => /^(-f|-F|--field|--raw-field|--input)$/.test(x)))) pushHit(res, 'T2', 'gh_api_write');
      return;
    }
    case 'npm': case 'pnpm': case 'yarn': case 'bun': case 'cargo': case 'twine': case 'gem': case 'poetry': case 'uv': {
      if (args.some((a) => /^(publish|unpublish|deprecate|upload|push)$/.test(a)) && !hasFlag(args, /--dry-run|-n$/)) pushHit(res, 'T2', 'publish', prog);
      else if ((prog === 'npm' || prog === 'yarn' || prog === 'pnpm') && args.some((a) => /^(token|owner|access|login|adduser)$/.test(a))) pushHit(res, 'T2', 'registry_admin');
      return;
    }
    case 'docker': case 'podman': case 'docker-compose': {
      const words = args.filter((x) => !/^-/.test(x)); const joined = args.join(' ');
      if (/\bsystem\s+prune\b/.test(joined) || /\bvolume\s+(rm|prune)\b/.test(joined) || (/\b(compose\s+)?down\b/.test(joined) && hasFlag(args, /^-v$|--volumes/)) ||
        (words[0] === 'rm' && hasFlag(args, /f/) ) || /\b(image|container|network|builder)\s+prune\b/.test(joined) || words[0] === 'kill' || /\brmi\b/.test(joined)) pushHit(res, 'T2', 'docker_destructive', words.slice(0, 2).join(' '));
      else if (words[0] === 'push') pushHit(res, 'T2', 'docker_push');
      return;
    }
    case 'curl': case 'wget': case 'http': case 'https': case 'xh': case 'httpie': {
      const joined = args.join(' ');
      const getMode = hasFlag(args, /^-[a-zA-Z]*G|^--get$/);
      const writeM = !getMode && (/(-X|--request)\s*(POST|PUT|PATCH|DELETE)\b/i.test(joined) || hasFlag(args, /^-d$|^--data|^-F$|^--form|^-T$|^--upload-file|^--json$/) || (prog !== 'curl' && prog !== 'wget' && /\b(POST|PUT|PATCH|DELETE)\b/.test(joined)) || /^--post-(data|file)/.test(joined));
      if (writeM) {
        const urlTok = args.find((a) => /^(https?:\/\/)/i.test(a) || /^[\w.-]+\.[a-z]{2,}(:\d+)?(\/|$)/i.test(a));
        const local = urlTok && /^(https?:\/\/)?(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\]|host\.docker\.internal|[\w-]+\.local)(:\d+)?(\/|$)/i.test(urlTok);
        if (!local) pushHit(res, 'T2', 'http_write_remote');
      }
      return;
    }
    default: break;
  }
  if (SQL_CLI.has(prog)) { classifyText(args.join(' '), res, 'sql'); scanReferencedFile(args, ctx, res, /^(-f|--file|<)$/); return; }
  if (CLOUD.has(prog)) {
    const j = args.filter((a) => !/^-/.test(a)).join(' ');
    if (prog === 'terraform' || prog === 'tofu') { if (/\bdestroy\b/.test(j) || (/\bapply\b/.test(j) && /-auto-approve/.test(args.join(' ')))) pushHit(res, /destroy/.test(j) ? 'T1' : 'T2', 'terraform', j.split(' ')[0]); return; }
    if (prog === 'kubectl' || prog === 'oc' || prog === 'helm') { if (/\b(delete|drain|uninstall|replace)\b/.test(j) || /--replicas[= ]0/.test(args.join(' '))) pushHit(res, /\b(namespace|ns|node|pv|pvc|crd|cluster)\b/.test(j) ? 'T1' : 'T2', 'k8s_destructive', j.slice(0, 40)); else if (/\bapply\b/.test(j)) pushHit(res, 'T2', 'k8s_apply'); return; }
    if (CLOUD_DESTRUCT.test(j) || /^(patronictl|patroni)$/.test(prog) && /\b(remove|reinit|failover|switchover|restart|pause)\b/.test(j)) pushHit(res, 'T2', 'cloud_or_infra_change', prog + ' ' + j.split(' ').slice(0, 2).join(' '));
    return;
  }
  if (/^(prisma|drizzle-kit|knex|sequelize|sequelize-cli|typeorm|alembic|flyway|liquibase|django-admin|rails|rake|manage\.py)$/.test(prog) || (prog === 'python' || prog === 'python3') && /manage\.py/.test(args[0] || '')) {
    const j = args.join(' ');
    if (/\b(reset|drop|rollback|flush|force-reset|db\s+push|migrate:undo|migrate:rollback|sqlflush|dbreset|db:drop|db:reset)\b/.test(j)) pushHit(res, 'T2', 'migration_destructive', j.slice(0, 40));
    return;
  }
  if (INTERP.has(prog) || /\.(sh|py|js|ts|rb)$/.test(tk[0])) {
    const inline = [];
    for (let i = 0; i < args.length; i++) if (/^(-c|-e|-p|--eval|--print)$/.test(args[i]) && args[i + 1] !== undefined) inline.push(args[i + 1]);
    if ((prog === 'bash' || prog === 'sh' || prog === 'zsh') && inline.length) { for (const s of inline) { const r = lex(s); ctx.pending.push(...r.segs); ctx.hd.push(...r.heredocs); } }
    else if (inline.length) ctx.codeTexts.push(...inline);
    scanReferencedFile(args, ctx, res, null);
    if (args.includes('-') || args.some((a) => a === '__HEREDOC__')) ctx.hasHeredocInterp = true;
    return;
  }
  // make/just/task/npm run <script>: opaque; not raised (status quo) - documented limitation.
}

// protected config (hooks, policy, key, settings): writes via redirect/tee/cp/mv/rm/sed -i/inline code -> T1 (ack path exists)
function tamperSegment(rawToks, ctx, res) {
  const { toks: tk } = stripPrefix(rawToks, { });
  if (!tk.length) return;
  const prog = path.basename(tk[0]);
  const prot = (tok) => { if (!tok || /^-/.test(tok) || tok.includes('$SUB')) return false; try { return isProtectedPath(path.resolve(ctx.vcwd, expandHome(tok))); } catch { return false; } };
  for (let i = 0; i < tk.length - 1; i++) if ((tk[i] === '>' || tk[i] === '>>') && prot(tk[i + 1])) return pushHit(res, 'T1', 'tamper_protected_config', 'redirect');
  const args = tk.slice(1).filter((a) => a !== '<' && a !== '>' && a !== '>>');
  if (/^(tee|cp|mv|ln|rm|install|truncate|chmod|chown|rsync|dd|shred|unlink)$/.test(prog) && args.some(prot)) return pushHit(res, 'T1', 'tamper_protected_config', prog);
  if (/^(sed|perl)$/.test(prog) && hasFlag(args, /^-[a-zA-Z]*i|--in-place/) && args.some(prot)) return pushHit(res, 'T1', 'tamper_protected_config', prog);
}
const CODE_WRITES_PROTECTED = /(\.claude\/(settings[\w.-]*\.json|jev\/|hooks\/)|\.config\/jev\/)/;
const CODE_WRITE_OPS = /open\([^)]*['"][wa+]|write_text|write_bytes|writeFile|writeFileSync|appendFile|json\.dump|unlink|rename|os\.remove|shutil\.(copy|move|rmtree)|rmSync|\.write\(/;

function classifyText(text, res, kind) {
  for (const re of SQL_DROP) if (re.test(text)) { pushHit(res, 'T2', kind + '_drop_database', String(text.match(re)[0]).slice(0, 30)); return; }
  for (const re of SQL_T2.concat(REDIS_T2)) if (re.test(text)) { pushHit(res, 'T2', kind + '_destructive', String(text.match(re)[0]).slice(0, 30)); return; }
  const nw = sqlNoWhere(text); if (nw) pushHit(res, 'T2', kind + '_destructive', nw);
}
function scanReferencedFile(args, ctx, res, flagRe) {
  const cand = flagRe ? args.filter((a, i) => i > 0 && flagRe.test(args[i - 1])).concat(args.filter((a) => /\.(sql|py|sh|js|ts|rb|mjs|cjs)$/.test(a))) : args.filter((a) => !/^-/.test(a)).slice(0, 1);
  for (const f of cand.slice(0, 1)) {
    if (!f || /\$/.test(f)) continue;
    const p = path.resolve(ctx.vcwd, expandHome(f)); const txt = readSmall(p);
    if (txt == null) continue;
    ctx.fileTexts.push({ file: path.relative(ctx.root, p), text: txt, sql: /\.sql$/.test(p) });
  }
}

function classifyGit(args, ctx, res) {
  let a = args.slice(); let i = 0;
  while (i < a.length && /^-/.test(a[i])) { if (a[i] === '-C' || a[i] === '-c' || a[i] === '--git-dir' || a[i] === '--work-tree') i += 2; else i++; }
  const sub = a[i]; const rest = a.slice(i + 1); const flags = rest.filter((x) => /^-/.test(x)); const words = rest.filter((x) => !/^-/.test(x));
  switch (sub) {
    case 'push': {
      const forceish = flags.some((f) => /^(-f|--force|--mirror|--prune)$/.test(f)) || words.some((w) => /^\+/.test(w));
      const delFlag = flags.some((f) => /^(--delete|-d)$/.test(f));
      const lease = flags.some((f) => /^--force-with-lease(=.*)?$|^--force-if-includes$/.test(f));
      const refName = (w) => w.replace(/^[+:]/, '').replace(/^HEAD:/, '').replace(/^refs\/heads\//, '');
      const specs = words.slice(1);                                                     // words[0] is the remote
      const deleted = delFlag ? specs.map(refName) : specs.filter((w) => /^:[^:]/.test(w)).map(refName);
      const pushed = delFlag ? [] : specs.filter((w) => !/^:[^:]/.test(w)).map(refName);
      const protectedDel = deleted.some((r) => PROTECTED_BRANCH.test(r));              // `git push origin main :old-branch` deletes old-branch only
      const protectedPush = pushed.some((r) => PROTECTED_BRANCH.test(r));
      if (forceish || (lease && protectedPush) || protectedDel) pushHit(res, 'T1', 'git_push_force_or_delete');
      else if (deleted.length) pushHit(res, 'T2', 'git_push_delete_branch');
      else if (lease) pushHit(res, 'T2', 'git_push_force_with_lease');
      return;
    }
    case 'reset': if (flags.includes('--hard')) pushHit(res, 'T1', 'git_reset_hard', '', 'dirty_tracked'); return;
    case 'clean': {
      if (flags.some((f) => /^-[a-zA-Z]*[nN]|--dry-run/.test(f))) return;
      if (flags.some((f) => /^-[a-zA-Z]*[xX]/.test(f))) pushHit(res, 'T1', 'git_clean_ignored'); else if (flags.some((f) => /^-[a-zA-Z]*f|--force/.test(f))) pushHit(res, 'T1', 'git_clean_force', '', 'untracked');
      return;
    }
    case 'checkout': {
      if (flags.some((f) => f === '-f' || f === '--force')) { pushHit(res, 'T1', 'git_checkout_force', '', 'dirty_tracked'); return; }
      const dd = rest.indexOf('--'); const paths = dd >= 0 ? rest.slice(dd + 1) : [];
      if (paths.includes('.') || paths.includes(':/') || (dd < 0 && words.length === 1 && words[0] === '.')) pushHit(res, 'T1', 'git_checkout_discard_all', '', 'dirty_tracked');
      return;
    }
    case 'restore': {
      if (flags.some((f) => /^--staged$|^-S$/.test(f)) && !flags.some((f) => /^--worktree$|^-W$/.test(f))) return;
      if (words.includes('.') || words.includes(':/')) pushHit(res, 'T1', 'git_restore_discard_all', '', 'dirty_tracked');
      return;
    }
    case 'stash': if (words[0] === 'drop' || words[0] === 'clear') pushHit(res, 'T2', 'git_stash_drop'); return;
    case 'branch': if (flags.some((f) => /^-D$|^--delete$/.test(f) && (f === '-D' || flags.includes('--force') || flags.includes('-f')))) pushHit(res, 'T2', 'git_branch_force_delete'); return;
    case 'reflog': if (words[0] === 'expire' || words[0] === 'delete') pushHit(res, 'T1', 'git_reflog_expire'); return;
    case 'gc': if (flags.some((f) => /--prune=now|--aggressive/.test(f))) pushHit(res, 'T1', 'git_gc_prune_now'); return;
    case 'filter-branch': case 'filter-repo': pushHit(res, 'T1', 'git_history_rewrite'); return;
    case 'worktree': if (words[0] === 'remove' && flags.some((f) => f === '-f' || f === '--force')) pushHit(res, 'T2', 'git_worktree_force_remove'); return;
    case 'remote': if (words[0] === 'remove' || words[0] === 'rm' || words[0] === 'set-url') pushHit(res, 'T2', 'git_remote_change'); return;
    default: return;
  }
}

// ---------- entry ----------
function classify(command, opts = {}) {
  const root = path.resolve(opts.root || process.cwd());
  const roots = [root].concat((opts.roots || []).map((r) => path.resolve(r))).filter((v, i, a) => a.indexOf(v) === i);
  const ctx = { root, roots, created: (opts.created || []).map((r) => path.resolve(r)), vcwd: path.resolve(opts.cwd || root), pending: [], hd: [], codeTexts: [], fileTexts: [], hasHeredocInterp: false };
  const res = { hits: [], xargs: false, kws: [], windows: [] };
  const cmd = String(command);
  if (/:\(\)\s*\{\s*:\s*\|\s*:&?\s*\}\s*;\s*:/.test(cmd)) pushHit(res, 'T1', 'fork_bomb');
  const L = lex(cmd); ctx.hd.push(...L.heredocs);
  const queue = L.segs.slice();
  while (queue.length) { const sg = queue.shift(); classifySegment(sg, ctx, res); tamperSegment(sg, ctx, res); while (ctx.pending.length) queue.push(ctx.pending.shift()); }
  // code text scanning (inline interpreter code, heredocs used by interpreters/SQL clients, referenced scripts)
  const interpUsed = ctx.hasHeredocInterp || ctx.codeTexts.length || /__HEREDOC__/.test(cmd);
  const texts = [];
  for (const t of ctx.codeTexts) texts.push({ text: t, where: 'inline' });
  if (interpUsed) for (const h of ctx.hd) texts.push({ text: h, where: 'heredoc' });
  for (const f of ctx.fileTexts) texts.push({ text: f.text, where: 'file:' + f.file, sql: f.sql });
  for (const t of texts) {
    const body = t.text.length > 60000 ? t.text.slice(0, 60000) : t.text;
    const isSql = t.sql || res.hits.some((h) => /^sql_/.test(h.id)) || (/\b(SELECT|INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER)\b/i.test(body) && /\b(FROM|INTO|TABLE|SET)\b/i.test(body));
    const cw = codeWindows(body, CODE_DESTRUCT.concat(SQL_DROP, SQL_T2));
    const nw = sqlNoWhere(body);
    if (nw) cw.kws.push(nw);
    if (cw.kws.length) { pushHit(res, 'T2', isSql ? 'sql_or_code_destructive' : 'code_destructive', t.where + ':' + cw.kws.slice(0, 3).join('|')); res.kws.push(...cw.kws); res.windows.push(...cw.windows); }
  }
  for (const t of texts) if (CODE_WRITES_PROTECTED.test(t.text) && CODE_WRITE_OPS.test(t.text)) pushHit(res, 'T1', 'tamper_protected_config_code', 'code');
  const tiers = res.hits.map((h) => h.tier);
  const tier = tiers.includes('T1') ? 'T1' : tiers.includes('T2') ? 'T2' : 'T0';
  return { tier, hits: res.hits, kws: [...new Set(res.kws)].slice(0, 8), windows: res.windows.slice(0, 5) };
}

// directories this command creates (mkdir / git worktree add / git clone): later `rm -rf` of them is "cleaning up after itself"
function extractCreated(command, cwd, opt) {
  const out = []; let vcwd = path.resolve(cwd || process.cwd());
  const ex = (t) => path.resolve(vcwd, expandHome(t));
  // topmost ancestor that does not exist yet == what this command actually creates (mkdir -p a/b/c creates a, a/b, a/b/c)
  const topNew = (p) => { if (opt && opt.assumeMissing) return p; let cur = p, last = null; for (let i = 0; i < 40 && cur && cur !== '/' && !fs.existsSync(cur); i++) { last = cur; cur = path.dirname(cur); } return last; };
  for (const seg of lex(String(command)).segs) {
    const w = seg.filter((x) => x !== '>' && x !== '>>' && x !== '<');
    if (!w.length) continue;
    const ok = (x) => x && !/^-/.test(x) && !/\$/.test(x);
    const b = path.basename(w[0]);
    if (b === 'cd' && ok(w[1])) { vcwd = ex(w[1]); continue; }
    const add = (a) => { const t = topNew(ex(a)); if (t) out.push(t); };
    if (b === 'mkdir') for (const a of w.slice(1).filter(ok)) add(a);
    const i = w.indexOf('worktree'); if (b === 'git' && i >= 0 && w[i + 1] === 'add') { const a = w.slice(i + 2).filter(ok)[0]; if (a) add(a); }
    const k = w.indexOf('clone'); if (b === 'git' && k >= 0) { const a = w.slice(k + 1).filter(ok); if (a[1]) add(a[1]); }
    const n = w.indexOf('init'); if (b === 'git' && n >= 0) { const a = w.slice(n + 1).filter(ok)[0]; if (a) add(a); }
  }
  return out.slice(0, 40);
}

// T1 rules that are catastrophic and rarely legitimate for an unattended agent: enforced first. The conditional git-discard rules and
// rm outside the project are noisier (real-corpus replay), so they stay in shadow until labelled: t1_enforce 'all' or an explicit list.
const T1_CORE = new Set(['fork_bomb', 'disk_admin', 'dd_to_device', 'redirect_to_block_device', 'power', 'kill_all', 'crontab_remove',
  'git_history_rewrite', 'git_reflog_expire', 'git_gc_prune_now', 'git_clean_ignored', 'gh_repo_admin', 'rm_root_or_home', 'rm_dot_git',
  'find_delete_wide', 'git_push_force_or_delete', 'tamper_protected_config', 'terraform', 'k8s_destructive']);
module.exports = { classify, lex, extractCreated, REGEN, T1_CORE };
