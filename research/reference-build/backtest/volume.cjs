#!/usr/bin/env node
'use strict';
// volume.cjs - offline replay of YOUR Claude Code transcripts (last N days, default 21) through the final classifier.
// Answers, before you decide which directories are "personal": how many commands would the brake deny, how many would go to Jev (T2, judgeable),
// how many web reads would be screened, per root class. Prints counts and masked path classes only - no command text.
// usage: node backtest/volume.cjs [days=21] [--personal /a,/b] [--client /c,/d]     (default roots come from ~/.config/jev/policy.json, no personal roots => exit 2)
const fs = require('fs'), path = require('path');
const L = require('../jev/lib/jevlib.cjs');
const { classify, T1_CORE } = require('../jev/lib/shellclass.cjs');
const arg = (k) => { const i = process.argv.indexOf(k); return i > 0 && process.argv[i + 1] ? process.argv[i + 1].split(',') : null; };
const days = Number(process.argv[2]) > 0 ? Number(process.argv[2]) : 21; const cutoff = Date.now() - days * 864e5;
const pol = L.loadPolicy();
const roots = { personal: arg('--personal') || (pol.roots.personal.length ? pol.roots.personal : []), client: arg('--client') || (pol.roots.client.length ? pol.roots.client : []) };
if (!roots.personal.length) { console.error('no personal roots: pass --personal a,b or set roots in policy.json'); process.exit(2); }
const cls = (d) => L.rootClass({ roots }, d);
const zero = () => ({ personal: 0, client: 0, unknown: 0 });
const bash = zero(), t2 = zero(), t2sent = zero(), web = zero(), curl = zero(), gh = zero(); const core = { main: 0, sub: 0 }, coreIds = {}, t1cond = { main: 0, sub: 0 };
const CURL = /\b(curl|wget)\b[^|;&\n]*https?:\/\//, GH = /\bgh\s+(api|issue\s+view|pr\s+view|repo\s+view|search|release\s+view)\b/;
function* walk(d) { let es; try { es = fs.readdirSync(d, { withFileTypes: true }); } catch { return; } for (const e of es) { const p = path.join(d, e.name); if (e.isDirectory()) yield* walk(p); else if (e.name.endsWith('.jsonl')) yield p; } }
let total = 0;
for (const fp of walk(path.join(L.HOME, '.claude', 'projects'))) {
  let st; try { st = fs.statSync(fp); } catch { continue; } if (st.mtimeMs < cutoff || st.size > 400e6) continue; const sub = fp.includes(path.sep + 'subagents' + path.sep); let root = null;
  for (const line of fs.readFileSync(fp, 'utf8').split('\n')) {
    if (!line) continue; if (!root) { const m = line.match(/"cwd":"([^"]+)"/); if (m) root = m[1]; }
    const isBash = line.indexOf('"name":"Bash"') >= 0, isWeb = /"name":"(WebFetch|WebSearch)"/.test(line); if (!isBash && !isWeb) continue;
    let o; try { o = JSON.parse(line); } catch { continue; } if (o.type !== 'assistant') continue;
    const cwd = o.cwd || root || '/'; const c = cls([root || cwd, cwd]);
    for (const b of (o.message && o.message.content) || []) {
      if (!b || b.type !== 'tool_use') continue;
      if (b.name === 'WebFetch' || b.name === 'WebSearch') { web[c]++; continue; }
      if (b.name !== 'Bash') continue; const cmd = (b.input && b.input.command) || ''; if (!cmd) continue; total++; bash[c]++;
      if (CURL.test(cmd)) curl[c]++; if (GH.test(cmd)) gh[c]++;
      const r = classify(cmd, { root: root || cwd, cwd });
      const hard = r.hits.filter((h) => h.tier === 'T1' && !h.needs && T1_CORE.has(h.id));
      if (hard.length) { core[sub ? 'sub' : 'main']++; for (const h of hard) coreIds[h.id] = (coreIds[h.id] || 0) + 1; }
      if (r.hits.some((h) => h.tier === 'T1' && h.needs)) t1cond[sub ? 'sub' : 'main']++;
      if (r.tier === 'T2') { t2[c]++; if (!r.hits.every((h) => h.id === 'rm_unresolved_target') && c === 'personal') t2sent.personal++; }
    }
  }
}
const per = (n) => (n / days).toFixed(1) + '/day';
console.log(`roots personal=${roots.personal.join(',')}  client=${roots.client.join(',')}  (everything else = unknown)`);
console.log('bash commands by class:', JSON.stringify(bash), 'total', total);
console.log(`T1 core rules (deny-worthy, unconditional): main-thread ${core.main} (shadow by default), subagents ${core.sub} (enforced by default) over ${days}d; by id ${JSON.stringify(coreIds)}`);
console.log('T1 conditional git-discard rules (fire only if the tree is dirty at run time; shadow until labelled):', JSON.stringify(t1cond));
console.log('T2 gray zone by class:', JSON.stringify(t2), '| judgeable T2 that would be SENT to Jev (personal only):', t2sent.personal, `(${per(t2sent.personal)})`);
console.log('WebFetch/WebSearch by class:', JSON.stringify(web), '| curl/wget public reads by class:', JSON.stringify(curl), '| gh reads by class:', JSON.stringify(gh));
const screened = web.personal + web.unknown + curl.personal + curl.unknown + gh.personal;
console.log(`web screens per day with web_root_classes=[personal,unknown] (gh: personal only): ${per(screened)} => at ~$0.0001 each ~ $${(screened / days * 0.0001).toFixed(3)}/day`);
