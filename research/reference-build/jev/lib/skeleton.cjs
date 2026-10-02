'use strict';
// skeleton.cjs - privacy-preserving "command skeleton": keeps what decides destructiveness (program, subcommand, flags,
// SQL keywords, protected words, path CLASS) and drops names/literals/hosts. Used for client roots and for log lines.
const path = require('path');
const os = require('os');
const { lex, REGEN } = require('./shellclass.cjs');

const WORDS = new Set(('git push pull fetch clone commit add rm mv cp checkout switch restore reset revert rebase merge cherry-pick stash drop clear branch tag remote ' +
  'origin upstream HEAD main master prod production staging develop dev release trunk live stable all force hard soft mixed clean apply diff log status show ' +
  'npm npx pnpm yarn bun node deno python python3 pip pip3 uv poetry pytest vitest jest tsc eslint prettier ruff mypy black cargo go make docker compose podman kubectl helm terraform tofu pulumi aws gcloud az ' +
  'gh api pr issue repo release workflow run view list create delete merge close edit secret variable psql mysql sqlite3 redis-cli mongosh curl wget ssh scp rsync sudo chmod chown kill pkill killall ' +
  'systemctl service prisma migrate reset dev deploy publish install uninstall test build lint start stop restart up down exec logs ps images prune volume system network ' +
  'select insert update delete from into where set table database schema index drop truncate alter add column constraint values create user role grant revoke flushall flushdb del unlink config ' +
  'supabase db push pull diff patronictl patroni failover switchover reinit sed awk grep rg find xargs tee cat ls head tail wc sort uniq echo mkdir touch dd mkfs cd POST PUT PATCH DELETE GET HEAD ' +
  'pod pods deployment deployments deploy svc service services namespace namespaces ns node nodes pv pvc secret secrets configmap job cronjob statefulset daemonset ingress crd rollout scale').split(/\s+/));
const PROG_OK = /^[a-z][a-z0-9._+-]{0,23}$/i;
const SQLW = /\b(SELECT|INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|FROM|INTO|WHERE|SET|TABLE|DATABASE|SCHEMA|INDEX|VALUES|CASCADE|FLUSHALL|FLUSHDB|GRANT|REVOKE|BEGIN|COMMIT|ROLLBACK)\b/gi;

function hostClass(h) {
  if (/^(localhost|127\.|0\.0\.0\.0|\[::1\]|host\.docker\.internal)/i.test(h) || /\.local(:\d+)?$/i.test(h)) return 'local';
  if (/^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(h)) return 'private';
  return 'public';
}
function pathClass(tok, root, cwd) {
  const home = os.homedir();
  let p = tok.startsWith('~') ? path.join(home, tok.slice(1)) : tok.startsWith('$HOME') ? path.join(home, tok.slice(5)) : tok;
  if (/\$/.test(p)) return '<path:var>';
  const abs = path.resolve(cwd, p);
  const glob = /[*?[]/.test(tok) ? '*' : '';
  if (abs === root) return '<project-root>' + glob;
  if (abs.startsWith(root + path.sep)) { const parts = path.relative(root, abs).split(path.sep); const tag = parts.find((x) => REGEN.has(x)) || (parts[0] === '.git' ? '.git' : ''); return '<in-project' + (tag ? ':' + tag : '') + '>' + glob; }
  if (abs === home) return '<home>';
  if (abs.startsWith(home + path.sep)) { const first = path.relative(home, abs).split(path.sep)[0]; return /^\.(ssh|aws|config|claude|kube|docker|gnupg|npm|cache|local|gitconfig|bashrc|zshrc|profile)/.test(first) ? '<home/' + first + '>' : '<home-path>'; }
  if (abs.startsWith('/tmp/') || abs.startsWith('/var/tmp/')) return '<tmp>';
  if (abs === '/') return '</>';
  const top = abs.split('/')[1];
  return /^(etc|usr|var|opt|boot|bin|sbin|lib|lib64|root|srv|run|sys|proc|dev|mnt|media|home)$/.test(top) ? '</' + top + '/...>' : '<abs-outside-project>';
}
function skSql(s) {
  const L = '\u0001';
  const t = s.replace(/'[^']*'|"[^"]*"|\b\d+\b/g, L).replace(/\b[A-Za-z_][\w.]*\b/g, (w) => { const k = new RegExp('^(' + SQLW.source.slice(3, -3) + ')$', 'i').test(w); return k ? w.toUpperCase() : '<id>'; });
  return 'SQL[' + t.replace(new RegExp(L, 'g'), '<lit>').replace(/\s+/g, ' ').slice(0, 160) + ']';
}
function skTok(tok, i, ctx) {
  if (tok === '>' || tok === '>>' || tok === '<') return tok;
  if (tok.startsWith('$SUB')) return '$(…)';
  if (tok === '__HEREDOC__') return '<heredoc>';
  if (/^-/.test(tok)) { const eq = tok.indexOf('='); return (eq > 0 ? tok.slice(0, eq) + '=<v>' : tok).slice(0, 24); }
  if (/^\+/.test(tok) || /^:[^:]/.test(tok)) return tok[0] + '<ref>';
  if (WORDS.has(tok) || WORDS.has(tok.toLowerCase())) return tok;
  const url = tok.match(/^[a-z][a-z0-9+.-]*:\/\/([^/\s:@]*:?[^/\s@]*@)?([^/\s]+)/i);
  if (url) return '<url:' + hostClass(url[2]) + '>';
  if (SQLW.test(tok) && / /.test(tok)) { SQLW.lastIndex = 0; return skSql(tok); }
  SQLW.lastIndex = 0;
  if (/^[~.\/$]|\//.test(tok)) return pathClass(tok, ctx.root, ctx.cwd);
  if (/^\d+$/.test(tok)) return '<n>';
  if (/^\$[A-Za-z_][A-Za-z0-9_]*$/.test(tok)) return tok;
  return i === 0 && PROG_OK.test(tok) && WORDS.has(tok.toLowerCase()) ? tok : (i === 0 ? '<prog>' : '<w>');
}
function skeleton(cmd, opts = {}) {
  const ctx = { root: path.resolve(opts.root || process.cwd()), cwd: path.resolve(opts.cwd || opts.root || process.cwd()) };
  const { segs, heredocs } = lex(String(cmd));
  const parts = segs.slice(0, 14).map((seg) => seg.slice(0, 24).map((t, i) => skTok(t, i, ctx)).join(' '));
  let out = parts.join(' ; ');
  if (heredocs.length) { const kws = new Set(); for (const h of heredocs) for (const m of h.matchAll(/shutil\.rmtree|os\.(remove|unlink|system)|subprocess|\brm\s+-\w+|DROP\s+\w+|TRUNCATE|DELETE\s+FROM|fs\.(rm|unlink)\w*|requests\.(delete|post)|git\s+(push|reset|clean)/gi)) kws.add(m[0].replace(/\s+/g, ' ')); out += ' [heredoc:' + heredocs.length + (kws.size ? ' kw=' + [...kws].slice(0, 6).join('|') : '') + ']'; }
  return out.slice(0, 900);
}
module.exports = { skeleton, pathClass, hostClass };
