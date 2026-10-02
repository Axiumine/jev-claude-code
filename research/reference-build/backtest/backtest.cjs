'use strict';
// Offline backtest: replay the user's own Bash history (Claude Code transcripts, main sessions and/or subagents) through the
// deterministic tiers. No network. Prints aggregates and command SKELETONS only (names/literals/hosts stripped).
// usage: node backtest.cjs <days=21> <scope=main|sub|all> [skeletons]
const fs = require('fs'), path = require('path'), os = require('os');
const { classify, extractCreated } = require('../jev/lib/shellclass.cjs');
const { skeleton } = require('../jev/lib/skeleton.cjs');
const days = Number(process.argv[2] || 21); const scope = process.argv[3] || 'main';
const base = path.join(os.homedir(), '.claude', 'projects'); const cutoff = Date.now() - days * 864e5;
const tiers = {}, ids = { T1: {}, T2: {} }, sk = { T1: {}, T2: {} }; let total = 0, uncondT1 = 0, condT1 = 0, needs = {}, ms = 0, maxms = 0, slow = 0, nfiles = 0; const perDay = {}, t2PerDay = {}, t1PerDay = {};

function* walk(dir, want) {
  let ents; try { ents = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
  for (const e of ents) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) { yield* walk(p, want); continue; }
    if (!e.name.endsWith('.jsonl')) continue;
    const isSub = p.includes(path.sep + 'subagents' + path.sep);
    if ((want === 'main' && isSub) || (want === 'sub' && !isSub)) continue;
    yield p;
  }
}
for (const fp of walk(base, scope)) {
  let st; try { st = fs.statSync(fp); } catch { continue; }
  if (st.mtimeMs < cutoff || st.size > 400e6) continue;
  nfiles++; let root = null; const created = new Set();
  const gitRoot = (d) => { let c = d; for (let i = 0; i < 12 && c && c !== '/'; i++) { if (fs.existsSync(path.join(c, '.git'))) return c; c = path.dirname(c); } return d; };
  for (const line of fs.readFileSync(fp, 'utf8').split('\n')) {
    if (!line) continue;
    if (!root) { const m = line.match(/"cwd":"([^"]+)"/); if (m) root = gitRoot(m[1]); }
    if (line.indexOf('"name":"Bash"') < 0) continue;
    let o; try { o = JSON.parse(line); } catch { continue; }
    if (o.type !== 'assistant') continue;
    for (const b of (o.message && o.message.content) || []) {
      if (!b || b.type !== 'tool_use' || b.name !== 'Bash') continue;
      const cmd = (b.input && b.input.command) || ''; if (!cmd) continue;
      const cwd = o.cwd || root || '/'; const t0 = process.hrtime.bigint();
      let r = classify(cmd, { root: root || cwd, cwd });
      if (created.size && r.hits.some((h) => /outside|sibling/.test(h.id))) r = classify(cmd, { root: root || cwd, cwd, created: [...created] });
      if (/\b(mkdir|worktree|clone)\b/.test(cmd) && r.tier !== 'T1') for (const d of extractCreated(cmd, cwd, { assumeMissing: true })) created.add(d);
      const el = Number(process.hrtime.bigint() - t0) / 1e6; ms += el; if (el > maxms) maxms = el; if (el > 50) slow++;
      total++; tiers[r.tier] = (tiers[r.tier] || 0) + 1;
      const day = (o.timestamp || '').slice(0, 10); perDay[day] = (perDay[day] || 0) + 1;
      if (r.tier === 'T2') t2PerDay[day] = (t2PerDay[day] || 0) + 1;
      if (r.tier === 'T1') { const uncond = r.hits.some((h) => h.tier === 'T1' && !h.needs); if (uncond) { t1PerDay[day] = (t1PerDay[day] || 0) + 1; uncondT1++; } else condT1++; }
      if (r.tier === 'T1' || r.tier === 'T2') {
        const key = r.hits.filter((h) => h.tier === r.tier).map((h) => h.id + (h.needs ? '?' + h.needs : '')).sort().join('+');
        ids[r.tier][key] = (ids[r.tier][key] || 0) + 1;
        if (process.argv[4] === 'skeletons') { const s = skeleton(cmd, { root: root || cwd, cwd }).slice(0, 110); sk[r.tier][key + ' :: ' + s] = (sk[r.tier][key + ' :: ' + s] || 0) + 1; }
        if (r.tier === 'T1') for (const h of r.hits) if (h.needs) needs[h.needs] = (needs[h.needs] || 0) + 1;
      }
    }
  }
}
const top = (o, n) => Object.entries(o).sort((a, b) => b[1] - a[1]).slice(0, n);
console.log('scope:', scope, '| window days:', days, '| files:', nfiles, '| bash commands:', total, '| classify ms avg/max:', (ms / Math.max(1, total)).toFixed(3), maxms.toFixed(1), '| >50ms:', slow);
console.log('tiers:', tiers, '| pct:', Object.fromEntries(Object.entries(tiers).map(([k, v]) => [k, (100 * v / total).toFixed(2) + '%'])));
console.log('T1 unconditional commands:', uncondT1, '| T1 only-if-git-state commands:', condT1);
console.log('T1 conditional needs (resolved by git facts at runtime):', needs);
console.log('--- T1 by hit-ids'); for (const [k, v] of top(ids.T1, 20)) console.log(String(v).padStart(5), k);
console.log('--- T2 by hit-ids'); for (const [k, v] of top(ids.T2, 20)) console.log(String(v).padStart(5), k);
const dd = Object.keys(perDay).sort(); const t2v = dd.map((d) => t2PerDay[d] || 0).sort((a, b) => a - b);
console.log('--- per-day: bash | T2(Jev-bound) | unconditional-T1'); for (const d of dd) console.log(d, String(perDay[d]).padStart(6), String(t2PerDay[d] || 0).padStart(5), String(t1PerDay[d] || 0).padStart(3));
console.log('T2/day  median:', t2v[Math.floor(t2v.length / 2)], ' p90:', t2v[Math.floor(t2v.length * 0.9)], ' max:', t2v[t2v.length - 1], ' mean:', (t2v.reduce((a, b) => a + b, 0) / Math.max(1, t2v.length)).toFixed(1));
if (process.argv[4] === 'skeletons') { console.log('--- T1 skeleton samples'); for (const [k, v] of top(sk.T1, 40)) console.log(String(v).padStart(4), k); console.log('--- T2 skeleton samples'); for (const [k, v] of top(sk.T2, 30)) console.log(String(v).padStart(4), k); }
