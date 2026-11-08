#!/usr/bin/env python3
"""Collect up to five distinct AI security stories published on one UTC day."""
import argparse
import concurrent.futures
import copy
import datetime as dt
import email.utils
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# Also supports importlib-based tests without installing a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from news_dedupe import canonical_url, dedupe_archive, duplicate_reason, merge_aliases, story_id
from news_images import ensure_preview, remove_unused_assets, validate_previews

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'news.json'
NOW = dt.datetime.now(dt.timezone.utc)
RUN_DAY = NOW.date()
MAX_ARCHIVE = 80
MAX_NEW = 5
USER_AGENT = 'W4AdvisoryNewsBot/3.0 (+https://w4-advisory.github.io/news.html)'
FEEDS = json.loads((ROOT / 'scripts/news_sources.json').read_text())
AI = re.compile(r'\bAI\b|\bLLMs?\b|\bMCPs?\b|\bGenAI\b|artificial intelligence|language model|Claude|ChatGPT|OpenAI|Anthropic|Gemini|Copilot|DeepSeek|agentic|Ollama|LangChain|vLLM|Hugging Face|Codex', re.I)
TECHNICAL = re.compile(r'CVE-\d{4}-\d+|vulnerab|exploit|0.?day|zero.day|prompt injection|jailbreak|poison|fuzz|red.team|attack|sandbox|exfiltrat|\bRCE\b|security|bypass|cybercrime|espionage|threat|unauthorized|unintended|expos|leak|covert|policy|guardrail|safety|privacy', re.I)
PROMO = re.compile(r'\bcourse\b|certification|bootcamp|webinar|training program|how much does it cost|\bpricing\b|\bdiscount\b|\benrol\b|\btuition\b|who can apply|\bsponsored\b|advertorial', re.I)
PRIMARY_HOSTS = {'anthropic.com', 'blog.zsec.uk', 'nvd.nist.gov', 'cisa.gov', 'github.com', 'portswigger.net', 'blog.trailofbits.com', 'research.checkpoint.com', 'unit42.paloaltonetworks.com', 'microsoft.com'}
NEWS_SOURCES = {f['name'].casefold() for f in FEEDS} | {'intelligence online', 'tradingview', '01net', 'ars technica', 'the record'}
REPORT = {}


def clean(value):
    text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html.unescape(value or ''))).strip()
    return re.sub(r'\s+The post .+ appeared first on .+\.?$', '', text, flags=re.I)


def host(url):
    return (urllib.parse.urlparse(url or '').hostname or '').removeprefix('www.').lower()


def https_url(value, base=''):
    if not isinstance(value, str) or not value.strip():
        return None
    url = urllib.parse.urljoin(base, html.unescape(value.strip()))
    p = urllib.parse.urlparse(url)
    return url if p.scheme == 'https' and p.hostname and not p.username and not p.password else None


def timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)


def recent(value):
    published = timestamp(value)
    return bool(published and published.date() == RUN_DAY and published <= NOW)


def set_published(story, value, evidence):
    published = timestamp(value)
    if published:
        story.update(published=published.date().isoformat(), published_at=published.isoformat(), date_source=evidence)
        return True
    return False


def get(url, limit=6_000_000):
    if not https_url(url):
        raise ValueError('An HTTPS URL is required')
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=15) as response:
        if response.status != 200 or not https_url(response.geturl()):
            raise OSError(f'Unexpected response: {response.status}')
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('Response exceeds size limit')
        return raw, response.geturl(), response.headers


def get_json(url, limit=6_000_000):
    return json.loads(get(url, limit)[0])


class Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values, self.images, self.jsonld = {}, [], []
        self.in_json = False
        self.buffer = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            name = (attrs.get('property') or attrs.get('name') or attrs.get('itemprop') or '').lower()
            value = attrs.get('content', '')
            if name in {'og:title', 'og:description', 'twitter:description', 'description', 'article:published_time', 'datepublished', 'date', 'pubdate'}:
                if value:
                    self.values.setdefault(name, value)
            if name in {'og:image:secure_url', 'og:image', 'twitter:image', 'twitter:image:src'} and value:
                self.images.append(value)
        elif tag == 'link' and 'canonical' in attrs.get('rel', '').lower():
            self.values.setdefault('canonical', attrs.get('href', ''))
        elif tag == 'script' and attrs.get('type', '').lower() == 'application/ld+json':
            self.in_json, self.buffer = True, []

    def handle_data(self, data):
        if self.in_json:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.in_json:
            self.in_json = False
            try:
                self.jsonld.append(json.loads(''.join(self.buffer)))
            except ValueError:
                pass

    def structured_articles(self):
        def walk(value):
            if isinstance(value, dict):
                kind = value.get('@type', '')
                kinds = kind if isinstance(kind, list) else [kind]
                if any(str(k).lower() in {'newsarticle', 'article', 'blogposting', 'techarticle', 'report'} for k in kinds):
                    yield value
                for k in ('@graph', 'mainEntity', 'hasPart'):
                    yield from walk(value.get(k))
            elif isinstance(value, list):
                for item in value:
                    yield from walk(item)
        for value in self.jsonld:
            yield from walk(value)


def preview(story):
    """Publisher dates override discovery dates; HN submissions are not dates."""
    target = story.get('article_url') or story['url']
    if host(target) in {'news.google.com', 'google.com'}:
        return False
    try:
        raw, final, headers = get(target, 3_000_000)
        if 'html' not in headers.get('Content-Type', '').lower():
            return False
        parser = Metadata()
        parser.feed(raw.decode('utf-8', errors='replace'))
        values = parser.values
        canonical = https_url(values.get('canonical'), final) or final
        if host(canonical) in {'news.google.com', 'google.com'}:
            return False
        # Sites sometimes serve a home/marketing page at a removed article URL.
        # Do not replace a researched headline or cache that site's generic hero.
        page_title = clean(values.get('og:title'))
        if story.get('title') and page_title and not list(parser.structured_articles()):
            words = lambda t: set(re.findall(r'[a-z0-9]{4,}', t.casefold()))
            if not words(story['title']) & words(page_title):
                return False
        story['article_url'] = canonical
        if values.get('og:title') and not story.get('title'):
            title = clean(values['og:title'])
            title = re.sub(r'\s+[-|–—]\s+' + re.escape(story.get('source', '')) + r'\s*$', '', title, flags=re.I)
            story['title'] = title[:260]
        description = next((values[k] for k in ('og:description', 'twitter:description', 'description') if values.get(k)), '')
        if description and not story.get('summary'):
            story['summary'] = clean(description)[:360]
        images = list(parser.images)
        dates = [values.get(k) for k in ('article:published_time', 'datepublished', 'pubdate', 'date')]
        for article in parser.structured_articles():
            dates.append(article.get('datePublished'))  # Never dateModified.
            if not story.get('title') and article.get('headline'):
                story['title'] = clean(article['headline'])[:260]
            value = article.get('image', [])
            for image in value if isinstance(value, list) else [value]:
                if isinstance(image, str):
                    images.append(image)
                elif isinstance(image, dict):
                    images.append(image.get('url') or image.get('contentUrl'))
        # An official advisory API's publication date is already authoritative.
        if story.get('origin') != 'advisory':
            for value in dates:
                if set_published(story, value, canonical + '#publisher-metadata'):
                    break
        story['_images'] = [u for v in images if (u := https_url(v, final))]
        return True
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"Publisher metadata unavailable for {story.get('source', target)}: {exc}")
        return False


def relevance_score(story):
    title = clean(story.get('title'))
    summary = clean(story.get('summary'))
    target = story.get('article_url') or story.get('url')
    if not title or not https_url(target) or host(target) == 'educationtimes.com' or PROMO.search(title):
        return 0
    if re.search(r'\bsponsored\b|advertorial', summary, re.I):
        return 0
    # General roundups recycle unrelated headlines and can mention AI in passing.
    if re.search(r'week in review|weekly roundup|daily briefing|weekly digest', title, re.I):
        return 0
    if story.get('origin') == 'news' and story.get('source', '').casefold() not in NEWS_SOURCES:
        return 0
    if story.get('origin') == 'hn' and story.get('hn_score', 0) < 3:
        return 0
    if not (AI.search(title + ' ' + summary) and TECHNICAL.search(title + ' ' + summary)):
        return 0
    score = 4 + 2 * bool(TECHNICAL.search(title))
    if AI.search(title) and TECHNICAL.search(title):
        score += 2
    if story.get('origin') in {'research', 'advisory'} or host(target) in PRIMARY_HOSTS:
        score += 3
    if story.get('origin') == 'hn':
        score += 1
    return score if score >= 6 else 0


def rss_stories(url, origin, source=None):
    raw, _, _ = get(url, 8_000_000)
    root = ET.fromstring(raw)
    items = root.findall('.//channel/item') or root.findall('.//{*}entry')
    stories = []
    for item in items:
        def value(name):
            return item.findtext(name) or item.findtext('{*}' + name) or ''
        published = value('pubDate') or value('published') or value('date')
        if not recent(published):
            continue
        link = value('link').strip()
        if not link:
            link = next((e.get('href', '') for e in item.findall('{*}link') if e.get('rel', 'alternate') == 'alternate'), '')
        link = https_url(link)
        if not link or host(link) in {'news.google.com', 'google.com'}:
            continue
        story = {'title': clean(value('title'))[:260], 'url': link, 'source': source or clean(value('source')) or host(link),
                 'origin': origin, 'summary': clean(value('description') or value('summary') or value('encoded') or value('content'))[:360]}
        set_published(story, published, url + '#feed-published')
        if relevance_score(story):
            # Direct feed evidence is a valid fallback when an article blocks bots.
            preview(story)
            if relevance_score(story) and recent(story.get('published_at')):
                stories.append(story)
    return stories


def collect_anthropic():
    raw, _, _ = get('https://www.anthropic.com/sitemap.xml')
    root = ET.fromstring(raw)
    links = [(node.findtext('{*}lastmod') or '', node.findtext('{*}loc') or '') for node in root.findall('.//{*}url')]
    stories = []
    for modified, link in sorted(links, reverse=True):
        if not link.startswith('https://www.anthropic.com/research/') or not recent(modified):
            continue
        story = {'url': link, 'source': 'Anthropic Research', 'origin': 'research'}
        # Sitemap modification dates only discover links, never establish publication.
        if preview(story) and recent(story.get('published_at')) and relevance_score(story):
            stories.append(story)
    return stories


def collect_hn():
    start = dt.datetime.combine(RUN_DAY, dt.time.min, dt.timezone.utc)
    end = min(start + dt.timedelta(days=1), NOW)
    stories, seen = [], set()
    for query in ('AI', 'Claude', 'LLM', 'MCP', 'agent', 'prompt injection'):
        params = {'query': query, 'tags': 'story', 'restrictSearchableAttributes': 'title',
                  'numericFilters': f'created_at_i>={int(start.timestamp())},created_at_i<{int(end.timestamp())},points>=3', 'hitsPerPage': 100}
        for page in range(5):
            data = get_json('https://hn.algolia.com/api/v1/search_by_date?' + urllib.parse.urlencode(dict(params, page=page)))
            for hit in data.get('hits', []):
                item_id = hit['objectID']
                if item_id in seen:
                    continue
                seen.add(item_id)
                discussion = f'https://news.ycombinator.com/item?id={item_id}'
                link = https_url(hit.get('url')) or discussion
                story = {'title': clean(hit.get('title')), 'url': link, 'source': host(link), 'origin': 'hn',
                         'summary': clean(hit.get('story_text'))[:360], 'hn_score': hit.get('points', 0), 'discussion_url': discussion}
                if not relevance_score(story):
                    continue
                if link == discussion:
                    set_published(story, hit.get('created_at'), discussion + '#post-created')
                elif host(link) in {'x.com', 'twitter.com'}:
                    match = re.search(r'/status/(\d+)', link)
                    if match:
                        posted = dt.datetime.fromtimestamp(((int(match[1]) >> 22) + 1288834974657) / 1000, dt.timezone.utc)
                        set_published(story, posted.isoformat(), link + '#post-id-timestamp')
                else:
                    preview(story)
                if recent(story.get('published_at')) and relevance_score(story):
                    stories.append(story)
            if page + 1 >= data.get('nbPages', 1):
                break
    return stories


def collect_kev():
    data = get_json('https://raw.githubusercontent.com/cisagov/kev-data/develop/known_exploited_vulnerabilities.json')
    stories = []
    for item in data.get('vulnerabilities', []):
        cve, date = item.get('cveID', ''), item.get('dateAdded', '')
        if not recent(date) or not re.fullmatch(r'CVE-\d{4}-\d{4,19}', cve):
            continue
        story = {'title': f"{cve}: {clean(item.get('vulnerabilityName'))}",
                 'url': f'https://www.cisa.gov/known-exploited-vulnerabilities-catalog?field_cve={cve}',
                 'source': 'CISA KEV', 'origin': 'advisory', 'summary': clean(item.get('shortDescription'))[:360]}
        set_published(story, date, story['url'] + '#dateAdded')
        if relevance_score(story):
            stories.append(story)
    return stories


def advisory_title(cve_id, description):
    product = re.split(r"\s+(?:v?\d+\.\d+|is a |is an |contains |before |prior to |from )", description, maxsplit=1, flags=re.I)[0].strip()
    # Use the opening sentence; later impact discussion may describe a different issue.
    opening = re.split(r'\. (?=[A-Z])', description)[0]
    issue = re.search(r"prompt injection|remote code execution|arbitrary code execution|authentication bypass|denial of service|symlink following|cross.site scripting|SQL injection|information disclosure|path traversal", opening, re.I)
    if issue and 3 < len(product) < 80:
        return f"{cve_id}: {issue[0].capitalize()} in {product}"
    excerpt = description[:170].rsplit(' ', 1)[0] + '…' if len(description) > 170 else description
    return f"{cve_id}: {excerpt}"


def collect_nvd():
    # Read the day's complete advisory stream, not a handful of keyword queries.
    start = dt.datetime.combine(RUN_DAY, dt.time.min, dt.timezone.utc)
    end = min(start + dt.timedelta(days=1) - dt.timedelta(milliseconds=1), NOW)
    stories = []
    for offset in range(0, 5000, 500):
        if offset:
            time.sleep(6.5)  # Public API: five requests per rolling 30 seconds.
        params = urllib.parse.urlencode({'pubStartDate': start.isoformat(timespec='milliseconds'),
                                         'pubEndDate': end.isoformat(timespec='milliseconds'),
                                         'resultsPerPage': 500, 'startIndex': offset})
        data = get_json('https://services.nvd.nist.gov/rest/json/cves/2.0?' + params)
        for entry in data.get('vulnerabilities', []):
            cve = entry.get('cve', {})
            cve_id = cve.get('id', '')
            if not re.fullmatch(r'CVE-\d{4}-\d{4,19}', cve_id) or cve.get('vulnStatus') == 'Rejected':
                continue
            description = next((clean(d.get('value')) for d in cve.get('descriptions', []) if d.get('lang') == 'en'), '')
            story = {'title': advisory_title(cve_id, description), 'url': f'https://nvd.nist.gov/vuln/detail/{cve_id}',
                     'source': 'NIST NVD', 'origin': 'advisory', 'summary': description[:360]}
            set_published(story, cve.get('published'), story['url'] + '#published')
            if recent(story.get('published_at')) and relevance_score(story):
                stories.append(story)
        if offset + 500 >= data.get('totalResults', len(data.get('vulnerabilities', []))):
            break
    return stories


def collect_github_advisories():
    stories, seen = [], set()
    for page in range(1, 6):
        params = urllib.parse.urlencode({'published': RUN_DAY.isoformat(), 'per_page': 100, 'page': page})
        data = get_json('https://api.github.com/advisories?' + params)
        fresh = [item for item in data if item['ghsa_id'] not in seen]
        for item in fresh:
            seen.add(item['ghsa_id'])
            if item.get('withdrawn_at'):
                continue
            story = {'title': advisory_title(item.get('cve_id') or item['ghsa_id'], clean(item.get('description') or item.get('summary'))),
                     'url': item['html_url'], 'source': 'GitHub Security Advisories', 'origin': 'advisory',
                     'summary': clean(item.get('description'))[:360]}
            set_published(story, item.get('nvd_published_at') or item.get('published_at'), story['url'] + '#published_at')
            if item.get('type') == 'unreviewed' and item.get('cve_id') and relevance_score(story):
                record_url = 'https://cveawg.mitre.org/api/cve/' + item['cve_id']
                record = get_json(record_url)
                if not set_published(story, record.get('cveMetadata', {}).get('datePublished'), record_url + '#datePublished'):
                    continue
            if recent(story.get('published_at')) and relevance_score(story):
                stories.append(story)
        if len(data) < 100 or not fresh:
            break
    return stories


def collect_sources():
    sources = [(f['name'], lambda f=f: rss_stories(f['url'], f['origin'], f['name'])) for f in FEEDS]
    sources += [('Anthropic Research', collect_anthropic), ('Hacker News', collect_hn), ('CISA KEV', collect_kev),
                ('NIST NVD', collect_nvd), ('GitHub Security Advisories', collect_github_advisories)]
    def run(source):
        name, collector = source
        try:
            found = collector()
            return found, {'source': name, 'status': 'ok', 'candidates': len(found)}
        except (OSError, ValueError, ET.ParseError) as exc:
            return [], {'source': name, 'status': 'error', 'error': str(exc)}
    candidates, statuses = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for found, status in pool.map(run, sources):
            candidates.extend(found)
            statuses.append(status)
            print(json.dumps(status), flush=True)
    return candidates, statuses


def existing():
    # A corrupt archive must fail the run rather than silently replacing history.
    if not DATA.exists():
        return []
    data = json.loads(DATA.read_text())
    if not isinstance(data, list):
        raise ValueError('news.json must contain a list')
    return data


def seen_history(archive):
    path = ROOT / 'news-seen.json'
    data = json.loads(path.read_text()) if path.exists() else []
    if not isinstance(data, list):
        raise ValueError('news-seen.json must contain a list')
    return merge_history(data, archive)


def merge_history(history, current):
    # Keep history persistent without re-comparing every historical pair each day.
    merged = copy.deepcopy(history)
    index = {story_id(s): i for i, s in enumerate(merged)}
    for story in current:
        winner = copy.deepcopy(story)
        identity = story_id(winner)
        winner['id'] = identity
        if identity in index:
            merge_aliases(winner, merged[index[identity]])
            merged[index[identity]] = winner
        else:
            index[identity] = len(merged)
            merged.append(winner)
    return merged


def save_history(stories):
    fields = ('id', 'title', 'url', 'article_url', 'source', 'published', 'summary', 'aliases')
    data = [{k: s[k] for k in fields if k in s} for s in stories]
    (ROOT / 'news-seen.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def validate_archive(stories, additions=()):
    validate_previews(stories, ROOT)
    for i, story in enumerate(stories):
        if not story.get('title') or not https_url(story.get('article_url') or story.get('url')):
            raise ValueError('Invalid article')
        for other in stories[i + 1:]:
            if reason := duplicate_reason(story, other):
                raise ValueError(f"Duplicate ({reason}): {story['title']} / {other['title']}")
    for story in additions:
        if not recent(story.get('published_at')) or not story.get('date_source'):
            raise ValueError(f"Unverified or wrong-day article: {story['title']}")
    if sum(s.get('published') == RUN_DAY.isoformat() for s in stories) > MAX_NEW:
        raise ValueError('More than five articles for the selected publication day')


def main(repair_only=False, report_path=None):
    before = existing()
    old, removed = dedupe_archive(copy.deepcopy(before))
    history = seen_history(old)
    day = RUN_DAY.isoformat()
    budget = max(0, MAX_NEW - sum(s.get('published') == day for s in history))
    candidates, statuses = ([], []) if repair_only else collect_sources()
    if not repair_only and not any(s['status'] == 'ok' for s in statuses):
        raise RuntimeError('Every news source failed; archive was not changed')
    additions, rejected = [], []
    source_counts = {}
    for story in history:
        if story.get('published') == day:
            source_counts[story.get('source')] = source_counts.get(story.get('source'), 0) + 1
    # Prefer research, then rotate publisher choices before giving one a second slot.
    pool = sorted(candidates, key=lambda s: (-relevance_score(s), s.get('source', ''), s.get('title', '')))
    while pool:
        pool.sort(key=lambda s: source_counts.get(s.get('source'), 0))
        story = pool.pop(0)
        reason = None
        if not relevance_score(story):
            reason = 'not relevant / promotional'
        elif not recent(story.get('published_at')) or not story.get('date_source'):
            reason = 'not published on selected UTC day'
        else:
            match = next((s for s in old + additions + history if duplicate_reason(story, s)), None)
            if match:
                reason = 'duplicate: ' + duplicate_reason(story, match)
                # Keep aliases, but not temporary image/date fields, for future runs.
                merge_aliases(match, story)
        if not reason and len(additions) >= budget:
            reason = 'daily limit reached'
        if reason:
            rejected.append({'title': story.get('title'), 'url': story.get('url'), 'reason': reason})
            continue
        story.update(id=story_id(story), added_on=NOW.date().isoformat())
        additions.append(story)
        source_counts[story.get('source')] = source_counts.get(story.get('source'), 0) + 1
    result = sorted(additions + old, key=lambda s: (s.get('published', ''), s.get('published_at', ''), s.get('title', '')), reverse=True)[:MAX_ARCHIVE]
    # All records get a real local asset. Metadata refresh is bounded, but image
    # validation is not: a missing or corrupt cached file is always repaired.
    refresh = [s for s in result if s.get('image_kind') != 'publisher']
    for story in refresh[:len(additions) + (MAX_ARCHIVE if repair_only else 8)]:
        if '_images' not in story:
            preview(story)
    # Existing archive publication dates must not be rewritten by a later page edit.
    original_dates = {story_id(s): {k: s[k] for k in ('published', 'published_at', 'date_source') if k in s} for s in before}
    for story in old:
        story.update(original_dates.get(story_id(story), {}))
    for story in result:
        ensure_preview(story, ROOT, get)
        for key in list(story):
            if key.startswith('_'):
                del story[key]
    validate_archive(result, additions)
    remove_unused_assets(result, ROOT)
    save_history(merge_history(history, result))
    DATA.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    report = {'publication_day_utc': day, 'sources': statuses, 'candidates': len(candidates),
              'removed_duplicates': removed, 'rejected': rejected,
              'added': [{k: s[k] for k in ('title', 'url', 'published', 'date_source', 'image', 'image_kind')} for s in additions],
              'archive_count': len(result), 'images': {kind: sum(s['image_kind'] == kind for s in result) for kind in ('publisher', 'headline')}}
    if report_path:
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    message = f"Published day {day} (UTC): added {len(additions)}, removed {len(removed)} duplicates; {len(result)} articles, all with validated local previews."
    print(message, flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write(message + '\n\n' + '\n'.join('- ' + s['title'] for s in additions) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', type=dt.date.fromisoformat, default=NOW.date(), help='Publication day in UTC (default: today)')
    parser.add_argument('--repair-archive', action='store_true', help='Dedupe and backfill previews without adding stories')
    parser.add_argument('--validate-only', action='store_true', help='Offline archive/image validation')
    parser.add_argument('--report', type=Path, help='Write a machine-readable run report')
    args = parser.parse_args()
    RUN_DAY = args.date
    if RUN_DAY > NOW.date():
        parser.error('Publication day cannot be in the future')
    if args.validate_only:
        validate_archive(existing())
        print('Archive, duplicate and preview checks passed.')
    else:
        main(args.repair_archive, args.report)
