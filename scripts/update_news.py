#!/usr/bin/env python3
"""Curate recent AI security headlines from public RSS feeds. Standard library only."""
import datetime as dt
import email.utils
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "news.json"
NOW = dt.datetime.now(dt.timezone.utc)
MAX_AGE = dt.timedelta(days=10)
MAX_ARCHIVE = 80
QUERY = '(("AI security" OR "LLM security" OR "prompt injection" OR "AI model vulnerability" OR "agentic AI security") when:10d)'
RSS = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
    "q": QUERY, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"
})
SECURITY = re.compile(r"security|vulnerab|attack|exploit|inject|jailbreak|poison|breach|leak|red.team|adversarial|malware|risk|threat|safety|guardrail|supply.chain", re.I)
AI = re.compile(r"\bAI\b|artificial intelligence|\bLLM\b|language model|chatgpt|generative|agentic|\bagent\b|deepfake|\bmodel\b", re.I)

def clean(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()

def existing():
    try:
        data = json.loads(DATA.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return []

def fetch():
    req = urllib.request.Request(RSS, headers={"User-Agent": "W4AdvisoryNewsBot/1.0 (+https://w4-advisory.github.io/)"})
    with urllib.request.urlopen(req, timeout=25) as response:
        if response.status != 200:
            raise RuntimeError(f"RSS HTTP {response.status}")
        raw = response.read(2_000_000)
    tree = ET.fromstring(raw)
    stories = []
    for item in tree.findall(".//channel/item"):
        title = clean(item.findtext("title"))
        url = (item.findtext("link") or "").strip()
        publisher = clean(item.findtext("source"))
        date = item.findtext("pubDate")
        try:
            published = email.utils.parsedate_to_datetime(date).astimezone(dt.timezone.utc)
        except (TypeError, ValueError, OverflowError):
            continue
        if not (NOW - MAX_AGE <= published <= NOW + dt.timedelta(hours=3)):
            continue
        if not (SECURITY.search(title) and AI.search(title)):
            continue
        p = urllib.parse.urlparse(url)
        if p.scheme != "https" or not p.hostname or p.hostname not in {"news.google.com", "www.google.com"}:
            continue
        if len(title) < 20 or len(title) > 240:
            continue
        stories.append({"title": title, "url": url, "source": publisher or "Google News", "published": published.date().isoformat()})
    return stories

def key(story):
    # Headlines prevent repeat posts even when the same article appears with a new RSS URL.
    title = re.sub(r"\s+-\s+[^-]{2,70}$", "", story["title"])
    return re.sub(r"[^a-z0-9]+", "", title.casefold())

def main():
    old = existing()
    try:
        candidates = fetch()
    except (OSError, ET.ParseError, RuntimeError) as exc:
        print(f"Feed unavailable; preserving archive: {exc}")
        return
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
    if not additions:
        print("No relevant new articles; archive unchanged.")
        return
    result = (additions + old)[:MAX_ARCHIVE]
    DATA.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Added {len(additions)} new stories (archive: {len(result)}).")

if __name__ == "__main__":
    main()
