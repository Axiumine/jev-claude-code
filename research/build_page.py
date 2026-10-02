#!/usr/bin/env python3
"""Inject a slim, scrubbed copy of data/catalog.json into page-template.html."""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, 'jev-in-claude-code.html')
cat = json.load(open(os.path.join(HERE, 'data', 'catalog.json')))

SCRUB = [(re.compile(r'/home/gio'), '~'), (re.compile(r'/media/(?:nvme|hdd)/Clienti[^\s,;)]*'), '<client root>'),
         (re.compile(r'/media/nvme/[^\s,;)]*'), '<local path>'), (re.compile(r'/tmp/claude-1000/[^\s,;)]*'), '<scratch path>'),
         (re.compile(r'[A-Za-z0-9._%+-]+@(?!(?:[\w-]+\.)*(?:example\.com|typesafe\.ai)\b)[A-Za-z0-9.-]+\.[a-z]{2,}'), '[email removed]')]


def clean(s, n=None):
    if s is None:
        return None
    s = re.sub(r'\s+', ' ', str(s)).strip()
    for rx, rep in SCRUB:
        s = rx.sub(rep, s)
    return s if n is None or len(s) <= n else s[:n - 1] + '…'


def clean_list(xs, k=5, n=260):
    return [clean(x, n) for x in (xs or [])[:k] if x]


entries = []
for e in cat['entries']:
    ver = e.get('verification') or {}
    entries.append({
        'n': clean(e.get('name'), 90), 'u': e.get('url'), 'c': e.get('category') or 'other', 'tier': e['tier'],
        's': e.get('score') if isinstance(e.get('score'), (int, float)) else None,
        'o': e.get('openrouter') or 'unknown', 'm': e.get('maturity'), 'st': e.get('stars'), 'cr': e.get('created') or None,
        'w': clean(e.get('what'), 320), 'p': clean_list(e.get('pros')), 'k': clean_list(e.get('cons')),
        'v': clean(e.get('verdict'), 400), 'a': clean(e.get('autoInvocation'), 260), 'i': [clean(x, 60) for x in (e.get('interface') or [])[:12]],
        'sec': clean_list(e.get('security'), 4, 240), 'vn': clean(ver.get('note'), 420),
        'mcp': e.get('kind') == 'mcp-server' or e.get('category') in ('decision-mcp', 'task-mcp'),
    })
listed = [{'n': x['name'], 'u': x['url'], 'tier': 'listed-only', 'w': clean(re.sub(r'\[[^\]]*\]\([^)]*\)', '', x.get('what') or ''), 240),
           'sec': clean(', '.join(x.get('sections') or []), 80), 'p': [], 'k': [], 'i': []} for x in cat['listedOnly']]
cats = {k: {'worthIt': v.get('worthIt'), 'bestPick': {'name': clean((v.get('bestPick') or {}).get('name'), 80)}} for k, v in cat['categories'].items()}
stats = {'analysed': len(entries), 'verified': sum(e['tier'] == 'verified' for e in entries),
         'source': sum(e['tier'] in ('verified', 'source-audited') for e in entries), 'mcp': sum(e['mcp'] for e in entries)}
data = json.dumps({'generated': '30 Sep 2026', 'stats': stats, 'categories': cats, 'entries': entries, 'listed': listed},
                  ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
page = open(os.path.join(HERE, 'page-template.html')).read().replace('__DATA__', data)
open(out_path, 'w').write(page)
leaks = [m for m in re.findall(r'/home/gio|Clienti|sk-or-[A-Za-z0-9]{8,}|[A-Za-z0-9._%+-]+@(?!(?:[\w-]+\.)*(?:example\.com|typesafe\.ai)\b)[A-Za-z0-9.-]+\.[a-z]{2,}', page)]
print(out_path, len(page.encode()), 'bytes; entries', len(entries), 'listed', len(listed), 'stats', stats, 'leak-hits', len(leaks))
