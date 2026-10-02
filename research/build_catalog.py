#!/usr/bin/env python3
"""Merge round-1 source audits, round-2 triage/deep/verify/category-judge results
into one catalog (data/catalog.json + JEV-CATALOG.md)."""
import json, re, collections, os

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, 'data')


def load(name):
    return json.load(open(os.path.join(D, name)))


def slug_of(k):
    return re.sub(r'[^a-z0-9]+', '-', k.lower()).strip('-')[:60] or 'x'


def gh_key(url):
    u = str(url or '').strip().lower()
    u = re.sub(r'^https?://', '', u)
    u = re.sub(r'^www\.', '', u)
    u = re.sub(r'[?#].*$', '', u).rstrip('/')
    u = re.sub(r'\.git$', '', u)
    m = re.match(r'^github\.com/([^/]+)/([^/]+)', u)
    return ('gh:' + m.group(1) + '/' + m.group(2)) if m else u


deep1 = load('deep_results.json')
cands = load('all_candidates.json')
meta = load('gh_meta.json')
meta.update(load('gh_meta_missing.json'))
r2 = load('round2_results.json')
awesome = load('awesome_entries.json')

# canonical GitHub names (follows renames)
canon = {k: (m['nameWithOwner'].lower() if m else None) for k, m in meta.items()}


def canon_key(url):
    k = gh_key(url)
    c = canon.get(k)
    return ('gh:' + c) if c else k


def gh_meta_for(url):
    k = gh_key(url)
    m = meta.get(k)
    if m:
        return m
    for kk, mm in meta.items():
        if mm and ('gh:' + mm['nameWithOwner'].lower()) == canon_key(url):
            return mm
    return None


cat_of = {}
for v in r2['categorize'].values():
    for it in (v or {}).get('items', []):
        cat_of[it['slug']] = it
verify = {k: v for k, v in r2['verify'].items() if v}
deep2 = {k: v for k, v in r2['deep'].items() if v}
triage = {}
for v in r2['triage'].values():
    for it in (v or {}).get('items', []):
        triage[it['slug']] = it

judge_rank = {}
cat_meta = {}
for cat, j in r2['catjudge'].items():
    if not j:
        continue
    cat_meta[cat] = {k: j.get(k) for k in ('summary', 'builtInAlternative', 'worthIt', 'worthItReason', 'bestPick', 'runnerUp', 'recommendationForUser')}
    for i, r in enumerate(j.get('ranking') or []):
        judge_rank[canon_key(r.get('url'))] = {'category': cat, 'rank': i + 1, 'score': r.get('score'), 'why': r.get('why'),
                                               'pros': r.get('pros'), 'cons': r.get('cons'), 'verified': r.get('verified')}


def apply_verify(e, v):
    if not v:
        return e
    rp = set(v.get('refutedPros') or [])
    rc = set(v.get('refutedCons') or [])
    e['pros'] = [p for p in e['pros'] if p not in rp] + list(v.get('addedPros') or [])
    e['cons'] = [c for c in e['cons'] if c not in rc] + list(v.get('addedCons') or [])
    if isinstance(v.get('adjustedScore'), (int, float)):
        e['score'] = v['adjustedScore']
    e['openrouter'] = v.get('openrouterSupportVerified') or e['openrouter']
    e['tier'] = 'verified'
    e['verification'] = {'confidence': v.get('confidence'), 'corrections': len(v.get('corrections') or []), 'note': v.get('note')}
    return e


def from_deep(d, slug, category, tier):
    ps = d.get('providerSupport') or {}
    return {
        'slug': slug, 'name': d.get('name'), 'url': d.get('url'), 'category': category, 'kind': d.get('kind'), 'tier': tier,
        'score': d.get('score'), 'openrouter': ps.get('openrouter'), 'maturity': d.get('maturity'),
        'fit': d.get('dailyCodingFit'), 'what': d.get('whatItDoes'),
        'interface': [i.get('name') for i in (d.get('interface') or [])][:20],
        'autoInvocation': d.get('autoInvocation'), 'security': (d.get('security') or [])[:6],
        'dataEgress': d.get('dataEgress'), 'failureMode': d.get('failureMode'), 'contextCost': d.get('contextCost'),
        'pros': list(d.get('pros') or []), 'cons': list(d.get('cons') or []), 'verdict': d.get('verdict'),
        'license': d.get('license'), 'stars': d.get('stars'), 'created': (d.get('createdAt') or '')[:10], 'pushed': (d.get('lastCommit') or '')[:10],
    }


entries = {}


def put(e):
    k = canon_key(e['url'])
    m = gh_meta_for(e['url'])
    if m:
        e['stars'] = m.get('stargazerCount')
        e['created'] = (m.get('createdAt') or '')[:10]
        e['pushed'] = (m.get('pushedAt') or '')[:10]
        e['license'] = ((m.get('licenseInfo') or {}).get('spdxId')) or e.get('license')
        e['url'] = m.get('url') or e['url']
        e['archived'] = m.get('isArchived')
        e['fork'] = m.get('isFork')
    jr = judge_rank.get(k)
    if jr:
        e['judge'] = jr
        if isinstance(jr.get('score'), (int, float)):
            e['score'] = jr['score']
        e['category'] = jr['category']
    order = {'verified': 3, 'source-audited': 2, 'readme-triaged': 1, 'listed-only': 0}
    prev = entries.get(k)
    if prev is None or order[e['tier']] > order[prev['tier']] or (order[e['tier']] == order[prev['tier']] and (e.get('score') or 0) > (prev.get('score') or 0)):
        if prev:
            e.setdefault('aliases', []).append(prev['url'])
        entries[k] = e
    else:
        prev.setdefault('aliases', []).append(e['url'])


excluded = []
# round 1 source audits
for slug, d in deep1.items():
    if not (d.get('exists') and d.get('isJevRelated')):
        excluded.append({'name': d.get('name'), 'url': d.get('url'), 'reason': 'nonexistent' if not d.get('exists') else 'not Jev-related'})
        continue
    c = cat_of.get(slug, {})
    e = from_deep(d, slug, c.get('category') or 'uncategorized', 'source-audited')
    put(apply_verify(e, verify.get(slug)))
# round 2 triage (+ new deep reads)
for slug, t in triage.items():
    if not (t.get('exists') and t.get('isJevRelated')):
        excluded.append({'name': t.get('name'), 'url': t.get('url'), 'reason': 'nonexistent' if not t.get('exists') else 'not Jev-related'})
        continue
    d = deep2.get(slug)
    if d and d.get('exists') and d.get('isJevRelated'):
        e = from_deep(d, slug, t.get('category'), 'source-audited')
    else:
        e = {'slug': slug, 'name': t.get('name'), 'url': t.get('url'), 'category': t.get('category'), 'kind': t.get('kind'),
             'tier': 'readme-triaged', 'score': t.get('score'), 'openrouter': t.get('openrouter'), 'maturity': t.get('maturity'),
             'fit': None, 'what': t.get('what'), 'interface': [x.strip() for x in re.split(r'[,;]', t.get('interface') or '') if x.strip()][:20],
             'autoInvocation': t.get('autoInvocation'), 'security': [], 'pros': list(t.get('pros') or []), 'cons': list(t.get('cons') or []),
             'verdict': None, 'stars': t.get('stars')}
    e['ccRelevant'] = t.get('ccRelevant')
    put(apply_verify(e, verify.get(slug)))

analyzed = {canon_key(e['url']) for e in entries.values()}
listed_only = []
for repo, es in awesome.items():
    k = canon_key('https://github.com/' + repo)
    if k in analyzed or ('gh:' + repo) in analyzed:
        continue
    secs = sorted({s for _, s, _ in es})
    listed_only.append({'name': repo, 'url': 'https://github.com/' + repo, 'category': 'unanalyzed', 'tier': 'listed-only',
                        'sections': secs, 'what': re.sub(r'\s+', ' ', es[0][2])[:300]})

routes = load('routes.json')
for e in entries.values():
    if e['slug'] in routes['labels']:
        e['routes'] = routes['labels'][e['slug']]
cat_meta['model-router']['routesLegend'] = routes['legend']

cat_list = list(entries.values())
cat_list.sort(key=lambda e: (e['category'] or 'zz', -(e.get('score') or 0), -(e.get('stars') or 0)))
json.dump({'generated': '2026-09-30', 'categories': cat_meta, 'entries': cat_list, 'listedOnly': listed_only, 'excluded': excluded},
          open(os.path.join(D, 'catalog.json'), 'w'), indent=1)

# ---------- markdown ----------
ORDER = ['decision-mcp', 'task-mcp', 'permission-gate', 'security-guard', 'stop-verifier', 'review-ci', 'code-search-nav', 'dev-skill',
         'prompt-steering', 'context-pruning', 'compaction-memory', 'model-router', 'skill-tool-router', 'suite-plugin', 'library-harness',
         'browser-os', 'eval-observability', 'other', 'uncategorized']


def cell(s, n=200):
    s = re.sub(r'\s+', ' ', str(s or '')).replace('|', '/')
    return s if len(s) <= n else s[:n - 1] + '…'


def bullets(xs, n=3, w=180):
    return '<br>'.join('• ' + cell(x, w) for x in (xs or [])[:n]) or '-'


by_cat = collections.defaultdict(list)
for e in cat_list:
    by_cat[e['category'] or 'uncategorized'].append(e)
tiers = collections.Counter(e['tier'] for e in cat_list)
L = ['# Jev integration catalog (Claude Code relevance)', '',
     f'Generated 2026-09-30. {len(cat_list)} analysed entries ({tiers["verified"]} adversarially verified, {tiers["source-audited"]} source-audited, '
     f'{tiers["readme-triaged"]} README-triaged), {len(listed_only)} listed in community awesome-lists but not analysed (off-target), {len(excluded)} excluded.',
     '', 'Score: 0-10 for daily coding in Claude Code with OpenRouter-hosted Jev (verified or category-judge score when available). '
     'OR = OpenRouter support found in code. Tier: V = verified, S = source-audited, R = README-triaged.', '']
for cat in ORDER + sorted(set(by_cat) - set(ORDER)):
    es = by_cat.get(cat)
    if not es:
        continue
    cm = cat_meta.get(cat) or {}
    L.append(f'## {cat} ({len(es)})')
    if cm:
        L.append('')
        L.append(f'**Worth it:** {cm.get("worthIt")}. {cell(cm.get("worthItReason"), 600)}')
        bp = cm.get('bestPick') or {}
        L.append(f'**Best pick:** {bp.get("name")}: {cell(bp.get("reason"), 400)}')
    L += ['', '| # | Name | Tier | Score | OR | Maturity | Stars | What | Pros | Cons |', '|---|---|---|---|---|---|---|---|---|---|']
    for i, e in enumerate(es, 1):
        t = {'verified': 'V', 'source-audited': 'S', 'readme-triaged': 'R'}[e['tier']]
        L.append(f'| {i} | [{cell(e["name"], 60)}]({e["url"]}) | {t} | {e.get("score")} | {e.get("openrouter") or "?"} | {e.get("maturity") or "?"} | '
                 f'{e.get("stars") if e.get("stars") is not None else "?"} | {cell(e.get("what"), 220)} | {bullets(e.get("pros"))} | {bullets(e.get("cons"))} |')
    L.append('')
L += [f'## Listed in awesome-lists, not analysed ({len(listed_only)})', '',
      'Off-target for Claude Code (apps, games, SDK experiments, domain tools) per their list section.', '',
      '| Name | List sections |', '|---|---|']
for x in sorted(listed_only, key=lambda x: x['name']):
    L.append(f'| [{x["name"]}]({x["url"]}) | {cell(", ".join(x["sections"]), 120)} |')
L += ['', f'## Excluded ({len(excluded)})', '', '| Name | Reason |', '|---|---|']
for x in excluded:
    L.append(f'| {cell(x["name"], 80)} ({x["url"]}) | {x["reason"]} |')
open(os.path.join(HERE, 'JEV-CATALOG.md'), 'w').write('\n'.join(L) + '\n')
print('entries', len(cat_list), dict(tiers), 'listed-only', len(listed_only), 'excluded', len(excluded))
print('by category', {c: len(v) for c, v in sorted(by_cat.items(), key=lambda x: -len(x[1]))})
