#!/usr/bin/env node
'use strict';
// jev-report: summarise the local decision log (shadow-mode review). Run in YOUR terminal.
// Usage: node report.cjs [days=14] [--would]     --would lists every would_deny / warn row so you can label it right or wrong.
const fs = require('fs'), path = require('path');
const L = require('./lib/jevlib.cjs');
const days = Number(process.argv[2]) > 0 ? Number(process.argv[2]) : 14; const cutoff = Date.now() - days * 864e5; const rows = [];
try {
  for (const f of fs.readdirSync(L.STATE)) if (/^log-.*\.jsonl$/.test(f)) for (const ln of fs.readFileSync(path.join(L.STATE, f), 'utf8').split('\n')) {
    if (!ln) continue; try { const o = JSON.parse(ln); if (Date.parse(o.ts) >= cutoff) rows.push(o); } catch { /* skip */ }
  }
} catch { console.log('no log yet (' + L.STATE + ')'); process.exit(0); }
const count = (arr, f) => { const m = {}; for (const x of arr) { const k = f(x); m[k] = (m[k] || 0) + 1; } return Object.entries(m).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k}=${v}`).join('  ') || '-'; };
const q = (a, p) => { const s = a.slice().sort((x, y) => x - y); return s.length ? s[Math.min(s.length - 1, Math.floor(p * s.length))] : '-'; };
const okCalls = rows.filter((r) => typeof r.ms === 'number' && r.model);
const last = okCalls.length ? okCalls[okCalls.length - 1].ts : null;
console.log(`rows=${rows.length} window=${days}d | last successful Jev call: ${last || 'NONE in window (hooks may be dead or the model/route was rejected: run doctor --live)'}`);
const by = {}; for (const r of rows) { const k = `${r.c}/${r.tier || ''}/${r.decision || r.why || 'n/a'}`; by[k] = (by[k] || 0) + 1; }
for (const [k, v] of Object.entries(by).sort()) console.log(String(v).padStart(6), k);
const brake = rows.filter((r) => r.c === 'brake' && r.tier);
if (brake.length) {
  console.log('--- brake by agent:', count(brake, (r) => `${r.tier}/${r.agent || '?'}`));
  console.log('--- T1 rule ids:', count(brake.filter((r) => r.tier === 'T1'), (r) => (r.ids || []).join('+')));
  console.log('--- T1 enforced vs shadow-only (scope_skipped = main-thread rows the t1_scope=subagents default did not block):', count(brake.filter((r) => r.tier === 'T1'), (r) => (r.enforced && r.enforced.length ? 'enforced' : r.scope_skipped ? 'scope_skipped' : 'shadow')));
  console.log('--- T2 rule ids:', count(brake.filter((r) => r.tier === 'T2'), (r) => (r.ids || []).join('+')));
}
if (okCalls.length) console.log(`jev latency ms p50=${q(okCalls.map((r) => r.ms), 0.5)} p95=${q(okCalls.map((r) => r.ms), 0.95)} max=${q(okCalls.map((r) => r.ms), 1)} n=${okCalls.length}`);
console.log('cost usd total:', rows.reduce((a, r) => a + (r.cost || 0), 0).toFixed(5));
console.log('models seen:', [...new Set(rows.map((r) => r.model).filter(Boolean))].join(', ') || '-', '| rows with drift:', rows.filter((r) => r.drift).length);
const errs = {}; for (const r of rows) if (r.why && r.decision === 'pass') errs[r.why] = (errs[r.why] || 0) + 1; console.log('fail-open reasons:', JSON.stringify(errs));
const jevCalls = rows.filter((r) => r.model || (r.why && r.decision === 'pass' && !/egress_off/.test(r.why)));
if (jevCalls.length) console.log(`error rate of Jev calls: ${(100 * jevCalls.filter((r) => !r.model).length / jevCalls.length).toFixed(1)}% of ${jevCalls.length}`);
if (process.argv.includes('--would')) {
  console.log('--- would_deny / warn rows (label each right/wrong; graduation gates are in the plan)');
  const g = {};
  for (const r of rows.filter((x) => x.decision === 'would_deny' || x.decision === 'warn')) {
    const k = r.c === 'webscreen' ? `web ${r.host} steers=${r.steers} plain=${r.plain} agent=${r.agent || '?'}` : `${r.tier} ${(r.ids || []).join('+')} irr=${r.irr ?? '-'} con=${r.con ?? '-'} wan=${r.wan ?? '-'} agent=${r.agent || '?'} :: ${r.sk}`;
    g[k] = (g[k] || 0) + 1;
  }
  for (const [k, v] of Object.entries(g).sort((a, b) => b[1] - a[1]).slice(0, 80)) console.log(String(v).padStart(4), k);
}
