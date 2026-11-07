#!/usr/bin/env python3
"""Select technical AI security research and advisories from public sources."""
import concurrent.futures
import datetime as dt
import email.utils
import html
from html.parser import HTMLParser
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "news.json"
NOW = dt.datetime.now(dt.timezone.utc)
MAX_AGE = dt.timedelta(days=14)
MAX_ARCHIVE = 80
MAX_NEW = 2
MAX_ENRICH = 12
USER_AGENT = "W4AdvisoryNewsBot/2.0 (+https://w4-advisory.github.io/news.html)"

# These queries are discovery sources, not an automatic inclusion rule.
GOOGLE_QUERIES = [
    '"prompt injection" OR "LLM exploit" OR "AI agent vulnerability" when:14d',
    '"AI vulnerability research" OR "Claude CVE" OR "MCP security flaw" when:14d',
]
NVD_TERMS = ("prompt injection", "large language model", "Ollama", "Model Context Protocol")
AI = re.compile(r"\bAI\b|\bLLMs?\b|\bMCPs?\b|\bGenAI\b|artificial intelligence|language model|"
                r"Claude|ChatGPT|OpenAI|Anthropic|Gemini|Copilot|DeepSeek|agentic|AI agent|Ollama|"
                r"LangChain|vLLM|Hugging Face", re.I)
TECHNICAL = re.compile(r"CVE-\d{4}-\d+|vulnerab|exploit|0.?day|zero.day|prompt injection|"
                       r"jailbreak|poison|fuzz|red.team|attack chain|sandbox escape|exfiltrat|"
                       r"remote code execution|\bRCE\b|security flaw|auth bypass|bypass|"
                       r"malicious instruction", re.I)
PROMO = re.compile(r"\bcourse\b|certificat|bootcamp|webinar|training program|how much does it cost|"
                   r"\bpricing\b|\bdiscount\b|\bacqui(?:re|sition)\b|\bbuy\b|\bfunding\b|"
                   r"\bpartnership\b|\bdeal\b|\bstock\b|launches new|announces new|"
                   r"new native .* innovations", re.I)
LOW_SIGNAL_HOSTS = {"tradingview.com", "stocktwits.com", "educationtimes.com"}
PRIMARY_HOSTS = {"anthropic.com", "blog.zsec.uk", "nvd.nist.gov", "cisa.gov", "github.com"}
NEWS_SOURCES = {"bleepingcomputer", "securityweek", "the record", "portswigger",
                "ars technica", "dark reading"}


def clean(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(value or ""))).strip()


def host(url):
    return (urllib.parse.urlparse(url or "").hostname or "").removeprefix("www.").lower()


def https_url(value, base=""):
    url = urllib.parse.urljoin(base, html.unescape(value or ""))
    parsed = urllib.parse.urlparse(url)
    return url if parsed.scheme == "https" and parsed.hostname else None


def recent(value):
    try:
        date = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=dt.timezone.utc)
        return NOW - MAX_AGE <= date <= NOW + dt.timedelta(hours=3)
    except (AttributeError, ValueError):
        return False


def get(url, limit=2_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as response:
        if response.status != 200:
            raise OSError(f"HTTP {response.status}")
        return response.read(limit), response.geturl(), response.headers


def get_json(url, limit=3_000_000):
    return json.loads(get(url, limit)[0])


class Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            name = (attrs.get("property") or attrs.get("name") or "").lower()
            if name in {"og:title", "og:image", "og:image:secure_url", "twitter:image",
                        "og:description", "twitter:description", "description", "article:published_time"}:
                self.values.setdefault(name, attrs.get("content", ""))
        elif tag == "link" and "canonical" in attrs.get("rel", "").lower():
            self.values.setdefault("canonical", attrs.get("href", ""))


def preview(story):
    """Add publisher Open Graph metadata without treating Google pages as articles."""
    target = story.get("article_url") or story["url"]
    try:
        raw, final, headers = get(target, 500_000)
        if "html" not in headers.get("Content-Type", "").lower() or host(final) in {"news.google.com", "google.com"}:
            return False
        parser = Metadata()
        parser.feed(raw.decode(headers.get_content_charset() or "utf-8", errors="replace"))
        values = parser.values
        canonical = https_url(values.get("canonical"), final) or final
        if host(canonical) in {"news.google.com", "google.com"}:
            return False
        story.setdefault("article_url", canonical)
        title = clean(values.get("og:title"))
        if title and not story.get("title"):
            story["title"] = title[:240]
        description = clean(next((values[k] for k in ("og:description", "twitter:description", "description") if values.get(k)), ""))
        if description and not story.get("summary"):
            story["summary"] = description[:260]
        image = https_url(next((values[k] for k in ("og:image:secure_url", "og:image", "twitter:image") if values.get(k)), ""), final)
        if image and not story.get("image"):
            story["image"] = image
        published = values.get("article:published_time", "")
        if published and not story.get("published") and recent(published):
            story["published"] = published[:10]
        return True
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Preview unavailable for {story.get('source', 'article')}: {exc}")
        return False


def relevance_score(story):
    title = clean(story.get("title"))
    text = f"{title} {clean(story.get('summary'))}"
    target = story.get("article_url") or story.get("url")
    if not title or not https_url(target) or PROMO.search(title) or host(target) in LOW_SIGNAL_HOSTS:
        return 0
    if story.get("origin") == "news" and story.get("source", "").casefold() not in NEWS_SOURCES:
        return 0
    if story.get("origin") == "hn" and story.get("hn_score", 0) < 10:
        return 0
    if not (AI.search(text) and TECHNICAL.search(text)):
        return 0
    score = 4
    if TECHNICAL.search(title):
        score += 2
    if host(target) in PRIMARY_HOSTS or story.get("origin") in {"research", "advisory"}:
        score += 3
    if story.get("origin") == "hn":
        score += 1
    if story.get("hn_score", 0) >= 20:
        score += 1
    return score if score >= 6 else 0


def feed_url(query):
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": query, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"
    })


def rss_stories(url, origin):
    raw, _, _ = get(url)
    items = ET.fromstring(raw).findall(".//channel/item")
    stories = []
    for item in items:
        try:
            published = email.utils.parsedate_to_datetime(item.findtext("pubDate")).astimezone(dt.timezone.utc)
        except (TypeError, ValueError, OverflowError):
            continue
        if not recent(published.isoformat()):
            continue
        title = clean(item.findtext("title"))
        link = https_url((item.findtext("link") or "").strip())
        if not link or not (20 <= len(title) <= 240):
            continue
        source = clean(item.findtext("source")) or ("ZephrSec" if origin == "research" else "Google News")
        story = {"title": title, "url": link, "source": source,
                 "published": published.date().isoformat(), "origin": origin}
        if origin == "research":
            story["summary"] = clean(item.findtext("description"))[:260]
        if relevance_score(story):
            stories.append(story)
    return stories


def collect_anthropic():
    raw, _, _ = get("https://www.anthropic.com/sitemap.xml", 4_000_000)
    root = ET.fromstring(raw)
    links = []
    for node in root.findall(".//{*}url"):
        loc = node.findtext("{*}loc") or ""
        modified = node.findtext("{*}lastmod") or ""
        if loc.startswith("https://www.anthropic.com/research/") and recent(modified):
            links.append((modified, loc))
    stories = []
    for _, link in sorted(links, reverse=True)[:15]:
        story = {"url": link, "source": "Anthropic Research", "origin": "research"}
        if preview(story) and story.get("published") and relevance_score(story):
            stories.append(story)
    return stories


def collect_hn():
    base = "https://hacker-news.firebaseio.com/v0/"
    ids = list(dict.fromkeys(get_json(base + "topstories.json")[:120] +
                             get_json(base + "newstories.json")[:150]))
    def load(item_id):
        try:
            return get_json(base + f"item/{item_id}.json", 100_000)
        except (OSError, ValueError):
            return None
    stories = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for item in pool.map(load, ids):
            if not item or item.get("type") != "story" or item.get("dead") or item.get("deleted"):
                continue
            try:
                published = dt.datetime.fromtimestamp(item["time"], dt.timezone.utc)
            except (KeyError, ValueError, OverflowError):
                continue
            title = clean(item.get("title"))
            if not recent(published.isoformat()) or not (AI.search(title) and TECHNICAL.search(title)):
                continue
            link = https_url(item.get("url")) or f"https://news.ycombinator.com/item?id={item['id']}"
            story = {"title": title, "url": link, "source": "Hacker News" if host(link) == "news.ycombinator.com" else host(link),
                     "published": published.date().isoformat(), "origin": "hn", "hn_score": item.get("score", 0),
                     "discussion_url": f"https://news.ycombinator.com/item?id={item['id']}"}
            if relevance_score(story):
                stories.append(story)
    return stories


def collect_kev():
    data = get_json("https://raw.githubusercontent.com/cisagov/kev-data/develop/known_exploited_vulnerabilities.json", 4_000_000)
    stories = []
    for item in data.get("vulnerabilities", []):
        date = item.get("dateAdded", "")
        if not recent(date + "T00:00:00+00:00"):
            continue
        cve = item.get("cveID", "")
        if not re.fullmatch(r"CVE-\d{4}-\d{4,19}", cve):
            continue
        title = f"{cve}: {clean(item.get('vulnerabilityName'))}"[:240]
        story = {"title": title, "url": f"https://www.cisa.gov/known-exploited-vulnerabilities-catalog?field_cve={cve}",
                 "source": "CISA KEV", "published": date, "origin": "advisory",
                 "summary": clean(item.get("shortDescription"))[:260]}
        if relevance_score(story):
            stories.append(story)
    return stories


def collect_nvd():
    stories = []
    start = (NOW - MAX_AGE).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
    end = NOW.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
    for term in NVD_TERMS:
        params = urllib.parse.urlencode({"keywordSearch": term, "pubStartDate": start,
                                         "pubEndDate": end, "resultsPerPage": 200})
        try:
            data = get_json("https://services.nvd.nist.gov/rest/json/cves/2.0?" + params)
        except (OSError, ValueError) as exc:
            print(f"NVD query {term!r} failed: {exc}")
            continue
        for entry in data.get("vulnerabilities", []):
            cve = entry.get("cve", {})
            cve_id = cve.get("id", "")
            if not re.fullmatch(r"CVE-\d{4}-\d{4,19}", cve_id) or cve.get("vulnStatus") == "Rejected":
                continue
            description = next((clean(d.get("value")) for d in cve.get("descriptions", []) if d.get("lang") == "en"), "")
            published = cve.get("published", "")
            if not recent(published):
                continue
            story = {"title": f"{cve_id}: {description[:170]}",
                     "url": f"https://nvd.nist.gov/vuln/detail/{cve_id}", "source": "NIST NVD",
                     "published": published[:10], "origin": "advisory", "summary": description[:260]}
            if relevance_score(story):
                stories.append(story)
    return stories


def existing():
    try:
        data = json.loads(DATA.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return []


def key(story):
    link = story.get("article_url") or story.get("url", "")
    if host(link) == "nvd.nist.gov":
        match = re.search(r"CVE-\d{4}-\d+", link, re.I)
        if match:
            return match.group().lower()
    title = re.sub(r"\s+-\s+[^-]{2,70}$", "", story.get("title", ""))
    return re.sub(r"[^a-z0-9]+", "", title.casefold())


def main():
    old = [story for story in existing() if relevance_score(story)]
    candidates = []
    sources = [("Anthropic research", collect_anthropic),
               ("ZephrSec RSS", lambda: rss_stories("https://blog.zsec.uk/rss/", "research")),
               ("Hacker News", collect_hn), ("CISA KEV", collect_kev), ("NIST NVD", collect_nvd)]
    sources += [(f"Google News {i + 1}", lambda q=q: rss_stories(feed_url(q), "news"))
                for i, q in enumerate(GOOGLE_QUERIES)]
    reached = 0
    for name, collect in sources:
        try:
            found = collect()
            candidates.extend(found)
            reached += 1
            print(f"{name}: {len(found)} relevant candidates")
        except (OSError, ValueError, ET.ParseError) as exc:
            print(f"{name} unavailable: {exc}")
    if not reached:
        print("No sources reachable; preserving archive.")
        return
    seen = {key(item) for item in old}
    seen_urls = {item.get("article_url") or item.get("url") for item in old}
    additions = []
    for story in sorted(candidates, key=lambda s: (relevance_score(s), s.get("published", "")), reverse=True):
        k = key(story)
        url = story.get("article_url") or story["url"]
        if k in seen or url in seen_urls:
            continue
        additions.append(story)
        seen.add(k)
        seen_urls.add(url)
        if len(additions) >= MAX_NEW:
            break
    result = (additions + old)[:MAX_ARCHIVE]
    pending = [story for story in result if not (story.get("image") and story.get("summary"))]
    start = NOW.date().toordinal() % len(pending) if pending else 0
    for story in (pending[start:] + pending[:start])[:MAX_ENRICH]:
        preview(story)
    if result != existing():
        DATA.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Added {len(additions)} technical stories; archive now {len(result)} items.")


if __name__ == "__main__":
    main()
