"""Conservative story matching across syndication, headlines and publishers.

URLs/CVEs are exact identifiers. Rewritten headlines additionally need shared
event facts or substantial specific vocabulary; a company/topic alone is not
enough. Merged aliases remain in the archive so later variants stay suppressed.
"""
import datetime as dt
import hashlib
import html
from functools import lru_cache
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

STOP = set('a an and are as at be been by can could for from has have how in into is it its new of on or our over reportedly report reports says security that the their this to two up using was were will with ai artificial intelligence startup startups firm firms million network networks talks eyes plans looks set seeks software technology companies company tech'.split())
ALIASES = {
    r'\bpalo alto(?: networks)?\b|\bpanw\b': 'paloalto',
    r'\bcrowd\s?strike\b|\bcrwd\b': 'crowdstrike',
    r'\bclutch(?: security)?\b': 'clutch',
    r'\bopen\s?ai\b': 'openai',
    r'\bchat\s?gpt\b': 'chatgpt',
    r'\bclaude code\b': 'claudecode',
    r'\bmodel context protocol\b': 'mcp',
    r'\blarge language models?\b|\bllms\b': 'llm',
    r'\b(?:buy|buys|buying|acquire|acquires|acquiring|acquisition|purchase|purchases|pay|paying|deal)\b': 'acquire',
    r'\b(?:leaks?|leaking|leakage|exposes?|exposed|exposing)\b': 'leak',
    r'\b(?:credentials?|passwords?|secrets?)\b': 'secret',
    r'\b(?:flaws?|vulnerabilities|vulnerability|bugs?)\b': 'vulnerability',
    r'\b(?:bypasses|bypassing|bypassed)\b': 'bypass',
}


@lru_cache(maxsize=50000)
def canonical_url(url):
    p = urlsplit(html.unescape(url or ''))
    hostname = (p.hostname or '').lower().removeprefix('www.')
    path = re.sub(r'/amp/?$', '', p.path).rstrip('/')
    query = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith('utm_')
             and k.lower() not in {'fbclid', 'gclid', 'ref', 'source', 'oc', 'output', 'amp'}]
    return urlunsplit(('https', hostname, path, urlencode(sorted(query)), '')) if hostname else ''


@lru_cache(maxsize=50000)
def normalized(text, source=''):
    text = unicodedata.normalize('NFKD', html.unescape(text or '')).encode('ascii', 'ignore').decode().lower()
    if source:
        text = re.sub(r'\s*[-|–—]\s*' + re.escape(source.lower()) + r'\s*$', '', text)
    text = re.sub(r'\$?([\d,]+(?:\.\d+)?)\s*(billion|bn|million|m)\b',
                  lambda m: ' money' + str(round(float(m[1].replace(',', '')) * (1000 if m[2] in {'billion', 'bn'} else 1))) + ' ', text)
    for pattern, replacement in ALIASES.items():
        text = re.sub(pattern, replacement, text)
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()


@lru_cache(maxsize=50000)
def tokens(text):
    return {t for t in normalized(text).split() if t not in STOP and len(t) > 2}


def variants(story):
    yield story
    yield from story.get('aliases', [])


def urls(story):
    return {canonical_url(v.get(k)) for v in variants(story) for k in ('url', 'article_url') if v.get(k)}


def cves(story):
    return set(re.findall(r'CVE-\d{4}-\d{4,19}',
                         ' '.join(str(story.get(k, '')) for k in ('title', 'url', 'article_url', 'summary')), re.I))


def close_dates(a, b):
    try:
        return abs((dt.date.fromisoformat(a['published'][:10]) - dt.date.fromisoformat(b['published'][:10])).days) <= 14
    except (KeyError, ValueError):
        return False


def headline_match(a, b):
    left, right = normalized(a.get('title'), a.get('source', '')), normalized(b.get('title'), b.get('source', ''))
    if left and left == right:
        return 'normalized headline'
    if not close_dates(a, b):
        return None
    x, y = tokens(left), tokens(right)
    shared = x & y
    # Deal headlines often omit the target. Actor + transaction + exact amount
    # catches "$495M" / "$495 million" without conflating two different deals.
    if 'acquire' in shared and any(t.startswith('money') for t in shared):
        actors = shared - {'acquire'} - {t for t in shared if t.startswith('money')}
        if actors:
            return 'same transaction and amount'
    if len(shared) >= 4 and len(shared) / min(len(x), len(y)) >= .78:
        return 'shared event vocabulary'
    # Compare descriptions too, but require several specific headline anchors.
    if len(shared) >= 3 and a.get('summary') and b.get('summary'):
        sx, sy = tokens(a['summary']), tokens(b['summary'])
        if len(sx & sy) >= 8 and len(sx & sy) / max(1, min(len(sx), len(sy))) >= .72:
            return 'same described event'
    return None


def duplicate_reason(a, b):
    if urls(a) & urls(b):
        return 'canonical URL'
    ac, bc = {s.upper() for s in cves(a)}, {s.upper() for s in cves(b)}
    if ac & bc:
        return 'same CVE'
    if ac and bc:
        return None  # Different CVEs in the same product are different stories.
    for av in variants(a):
        for bv in variants(b):
            reason = headline_match(av, bv)
            if reason:
                return reason
    return None


def story_id(story):
    return story.get('id') or hashlib.sha256(canonical_url(story.get('article_url') or story['url']).encode()).hexdigest()[:16]


def merge_aliases(winner, other):
    aliases = winner.setdefault('aliases', [])
    for variant in variants(other):
        alias = {k: variant[k] for k in ('title', 'url', 'article_url', 'source', 'published') if variant.get(k)}
        if alias not in aliases and alias.get('url') != winner.get('url'):
            aliases.append(alias)
    if not winner.get('discussion_url') and other.get('discussion_url'):
        winner['discussion_url'] = other['discussion_url']


def dedupe_archive(stories):
    # Prefer a direct article over a Google wrapper; keep stable archive order.
    ordered = sorted(stories, key=lambda s: 'news.google.com' in (s.get('article_url') or s['url']))
    kept, removed = [], []
    for story in ordered:
        match = next(((old, duplicate_reason(story, old)) for old in kept if duplicate_reason(story, old)), None)
        if match:
            winner, reason = match
            merge_aliases(winner, story)
            removed.append({'title': story['title'], 'kept': winner['title'], 'reason': reason})
        else:
            story['id'] = story_id(story)
            kept.append(story)
    return kept, removed
