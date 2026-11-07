#!/usr/bin/env python3
"""Collect AI security news and enrich archive entries with publisher previews."""
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
MAX_ENRICH_PER_RUN = 12
# Separate, broadly scoped feeds avoid Google's handling of a deeply nested OR query.
QUERIES = [
    '"prompt injection" OR "LLM vulnerability" when:14d',
    '"AI security" OR "AI cyberattack" when:14d',
    '"AI agent" security OR "agentic AI" threat when:14d',
    '"model poisoning" OR "AI jailbreak" OR "AI red teaming" when:14d',
]
SECURITY = re.compile(
    r"secur|vulnerab|attack|exploit|inject|jailbreak|poison|breach|leak|red.team|"
    r"adversarial|malware|risk|threat|safety|guardrail|supply.chain|"
    r"compromis|hijack|exfiltrat|prompt.injection",
    re.I,
)
AI = re.compile(
    r"\bAI\b|artificial intelligence|\bLLM\b|language model|chatgpt|"
    r"generative|agentic|\bagent\b|deepfake|\bmodel\b|openai|"
    r"anthropic|claude|gemini|copilot|deepseek|grok",
    re.I,
)

def clean(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(value or ""))).strip()

class Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            name = (attrs.get("property") or attrs.get("name") or "").lower()
            if name in {"og:image", "og:image:secure_url", "twitter:image", "og:description", "twitter:description", "description"}:
                self.values.setdefault(name, attrs.get("content", ""))
        elif tag == "link" and "canonical" in attrs.get("rel", "").lower():
            self.values.setdefault("canonical", attrs.get("href", ""))

def https_url(value, base=""):
    url = urllib.parse.urljoin(base, html.unescape(value or ""))
    parsed = urllib.parse.urlparse(url)
    return url if parsed.scheme == "https" and parsed.hostname else None

def preview(story):
    """Best-effort publisher metadata; Google interstitials are never used as previews."""
    target = story.get("article_url") or story["url"]
    try:
        req = urllib.request.Request(target, headers={"User-Agent": "Mozilla/5.0 (compatible; W4AdvisoryNewsBot/1.0)"})
        with urllib.request.urlopen(req, timeout=12) as response:
            final = response.geturl()
            if response.status != 200 or "html" not in response.headers.get("Content-Type", "").lower():
                return False
            raw = response.read(500_000)
            charset = response.headers.get_content_charset() or "utf-8"
        if urllib.parse.urlparse(final).hostname in {"news.google.com", "www.google.com"}:
            return False
        parser = Metadata()
        parser.feed(raw.decode(charset, errors="replace"))
        canonical = https_url(parser.values.get("canonical"), final) or final
        if urllib.parse.urlparse(canonical).hostname in {"news.google.com", "www.google.com"}:
            return False
        changed = False
        if not story.get("article_url"):
            story["article_url"] = canonical
            changed = True
        description = clean(next((parser.values.get(k) for k in ("og:description", "twitter:description", "description") if parser.values.get(k)), ""))
        if description and not story.get("summary"):
            story["summary"] = description[:260]
            changed = True
        image = https_url(next((parser.values.get(k) for k in ("og:image:secure_url", "og:image", "twitter:image") if parser.values.get(k)), ""), final)
        if image and not story.get("image"):
            story["image"] = image
            changed = True
        return changed
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Preview unavailable for {story.get('source', 'article')}: {exc}")
        return False

def existing():
    try:
        data = json.loads(DATA.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return []

def feed_url(query):
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": query, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"
    })

def fetch(query):
    req = urllib.request.Request(
        feed_url(query),
        headers={"User-Agent": "Mozilla/5.0 (compatible; W4AdvisoryNewsBot/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=25) as response:
        if response.status != 200:
            raise RuntimeError(f"RSS HTTP {response.status}")
        raw = response.read(2_000_000)
    return ET.fromstring(raw).findall(".//channel/item")

def collect():
    stories = []
    total_items = 0
    rejected_date = rejected_topic = rejected_url = 0
    successful = 0
    for query in QUERIES:
        try:
            items = fetch(query)
        except (OSError, ET.ParseError, RuntimeError) as exc:
            print(f"Feed request failed for {query!r}: {exc}")
            continue
        successful += 1
        total_items += len(items)
        print(f"RSS query {query!r}: {len(items)} items")
        for item in items:
            title = clean(item.findtext("title"))
            url = (item.findtext("link") or "").strip()
            publisher = clean(item.findtext("source"))
            try:
                published = email.utils.parsedate_to_datetime(
                    item.findtext("pubDate")
                ).astimezone(dt.timezone.utc)
            except (TypeError, ValueError, OverflowError):
                rejected_date += 1
                continue
            if not (NOW - MAX_AGE <= published <= NOW + dt.timedelta(hours=3)):
                rejected_date += 1
                continue
            # Require clear AI security relevance in the headline. Avoid keyword
            # matches on article descriptions and other potentially unrelated text.
            if not (SECURITY.search(title) and AI.search(title)):
                rejected_topic += 1
                continue
            parsed = urllib.parse.urlparse(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in {"news.google.com", "www.google.com"}
                or not (20 <= len(title) <= 240)
            ):
                rejected_url += 1
                continue
            stories.append({
                "title": title,
                "url": url,
                "source": publisher or "Google News",
                "published": published.date().isoformat(),
            })
    print(
        f"Feed diagnostics: {successful}/{len(QUERIES)} feeds reachable, "
        f"{total_items} items; rejected date={rejected_date}, "
        f"topic={rejected_topic}, url/length={rejected_url}; "
        f"eligible={len(stories)}"
    )
    return stories, successful

def key(story):
    title = re.sub(r"\s+-\s+[^-]{2,70}$", "", story["title"])
    return re.sub(r"[^a-z0-9]+", "", title.casefold())

def main():
    old = existing()
    candidates, successful = collect()
    if not successful:
        print("No feeds were reachable; retaining existing archive.")
    seen = {key(item) for item in old}
    seen_urls = {item.get("url") for item in old}
    additions = []
    for story in sorted(candidates, key=lambda s: s["published"], reverse=True):
        k = key(story)
        if k not in seen and story["url"] not in seen_urls:
            additions.append(story)
            seen.add(k)
            seen_urls.add(story["url"])
        if len(additions) == 2:
            break
    result = (additions + old)[:MAX_ARCHIVE]
    pending = [story for story in result if not (story.get("image") and story.get("summary"))]
    start = NOW.date().toordinal() % len(pending) if pending else 0
    selected = (pending[start:] + pending[:start])[:MAX_ENRICH_PER_RUN]
    for story in selected:
        preview(story)
    enriched = len(selected)
    if result != old:
        DATA.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Added {len(additions)} new stories; checked {enriched} previews (archive: {len(result)}).")

if __name__ == "__main__":
    main()

