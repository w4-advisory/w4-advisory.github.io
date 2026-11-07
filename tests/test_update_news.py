import importlib.util
import datetime as dt
from email.message import Message
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("update_news", Path(__file__).parents[1] / "scripts/update_news.py")
news = importlib.util.module_from_spec(spec)
spec.loader.exec_module(news)


class SelectionTests(unittest.TestCase):
    def test_calendar_window_includes_fifth_day_and_rejects_older_or_future(self):
        with patch.object(news, "NOW", dt.datetime(2026, 11, 7, 9, tzinfo=dt.timezone.utc)):
            for date in ("2026-11-07", "2026-11-02", "2026-11-02T00:00:00Z"):
                self.assertTrue(news.recent(date), date)
            for date in ("2026-11-01T23:59:59Z", "2026-11-08", "invalid", None):
                self.assertFalse(news.recent(date), date)

    def test_daily_cap_deduplication_and_archive_retention(self):
        today = news.NOW.astimezone(news.DAY_ZONE).date().isoformat()
        old = {"title": "Old AI security research", "url": "https://example.org/old",
               "origin": "research", "published": "2026-01-01"}
        candidates = [{"title": f"AI security research finding {i}", "url": f"https://example.org/{i}",
                       "origin": "research", "published": today} for i in range(8)]
        stale = dict(candidates[0], title="Stale AI security research", url="https://example.org/stale",
                     published=(news.NOW - dt.timedelta(days=6)).date().isoformat())
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "news.json"
            archive.write_text(json.dumps([old]))
            with patch.object(news, "DATA", archive), \
                 patch.object(news, "collect_anthropic", return_value=[stale] + candidates + candidates), \
                 patch.object(news, "collect_hn", return_value=[]), \
                 patch.object(news, "collect_kev", return_value=[]), \
                 patch.object(news, "collect_nvd", return_value=[]), \
                 patch.object(news, "rss_stories", return_value=[]), \
                 patch.object(news, "preview", return_value=False):
                news.main()
                first = json.loads(archive.read_text())
                news.main()
                second = json.loads(archive.read_text())
            self.assertEqual(len(first), 6)
            self.assertEqual(first, second)
            self.assertEqual(sum(item.get("added_on") == today for item in first), 5)
            self.assertIn(old["url"], [item["url"] for item in first])
            self.assertNotIn(stale["url"], [item["url"] for item in first])
            self.assertEqual(len({item["url"] for item in first}), 6)

    def test_empty_image_metadata_does_not_resolve_to_article_url(self):
        self.assertIsNone(news.https_url("", "https://example.org/article"))
        self.assertIsNone(news.https_url(None, "https://example.org/article"))

    def story(self, title, summary="", url="https://example.org/story", origin="news"):
        return {"title": title, "summary": summary, "url": url, "origin": origin}

    def test_rejects_course_even_when_ai_security_keywords_match(self):
        item = self.story("IIT Delhi launches a course in cybersecurity and AI security")
        self.assertEqual(news.relevance_score(item), 0)

    def test_keeps_relevant_industry_and_policy_coverage(self):
        for title, source, url in (
            ("CrowdStrike announces new native AI security innovations", "01net", "https://www.01net.it/story"),
            ("CrowdStrike to buy AI security startups and launch Kill Switch for rogue agents", "TradingView", "https://www.tradingview.com/story"),
            ("Macron gathers specialists for AI security briefing", "Intelligence Online", "https://www.intelligenceonline.com/story"),
        ):
            with self.subTest(title=title):
                item = self.story(title, url=url)
                item["source"] = source
                self.assertGreater(news.relevance_score(item), 0)

    def test_rejects_training_promotion_even_from_vetted_source(self):
        item = self.story("New AI security training course: who can apply and how much does it cost",
                          url="https://www.01net.it/story")
        item["source"] = "01net"
        self.assertEqual(news.relevance_score(item), 0)

    def test_accepts_first_party_vulnerability_research(self):
        item = self.story("Discovering vulnerabilities with Claude", url="https://www.anthropic.com/research/discovering-vulnerabilities-with-claude", origin="research")
        self.assertGreaterEqual(news.relevance_score(item), 8)

    def test_accepts_independent_mcp_vulnerability_research(self):
        item = self.story("Autonomous Vulnerability Hunting with MCP", "LLM-assisted 0day research", "https://blog.zsec.uk/bullyingllms/", "research")
        self.assertGreaterEqual(news.relevance_score(item), 8)

    def test_accepts_cve_and_relevant_hn_x_post(self):
        cve = self.story("CVE-2026-99999: Prompt injection in an AI agent", url="https://nvd.nist.gov/vuln/detail/CVE-2026-99999", origin="advisory")
        post = self.story("New prompt injection exploit against an AI agent", url="https://x.com/researcher/status/123", origin="hn")
        post["hn_score"] = 20
        self.assertGreater(news.relevance_score(cve), 0)
        self.assertGreater(news.relevance_score(post), 0)

    def test_rejects_unvetted_feed_and_low_engagement_hn_posts(self):
        feed = self.story("New prompt injection exploit against an AI agent", origin="news")
        feed["source"] = "Promo Daily"
        hn = self.story("New prompt injection exploit against an AI agent", origin="hn")
        hn["hn_score"] = 1
        self.assertEqual(news.relevance_score(feed), 0)
        self.assertEqual(news.relevance_score(hn), 0)

    def test_research_rss_uses_description_for_context(self):
        date = news.NOW.strftime("%a, %d %b %Y %H:%M:%S +0000")
        xml = (f"<rss><channel><item><title>Jenny was a Friend of Mine - MCPs and Friends</title><link>https://blog.zsec.uk/example/</link>"
               f"<pubDate>{date}</pubDate><description>LLM-assisted vulnerability hunting and 0days</description>"
               f"</item></channel></rss>").encode()
        with patch.object(news, "get", return_value=(xml, "https://blog.zsec.uk/rss/", {})):
            items = news.rss_stories("https://blog.zsec.uk/rss/", "research")
        self.assertEqual(len(items), 1)
        self.assertIn("vulnerability hunting", items[0]["summary"])

    def test_nvd_cve_is_ingested_when_it_affects_an_ai_agent(self):
        data = {"vulnerabilities": [{"cve": {"id": "CVE-2026-99999", "published": news.NOW.isoformat(),
                "vulnStatus": "Analyzed", "descriptions": [{"lang": "en", "value":
                "Prompt injection in an AI agent allows malicious tool calls."}]}}]}
        with patch.object(news, "get_json", return_value=data):
            items = news.collect_nvd()
        self.assertTrue(any(item["source"] == "NIST NVD" for item in items))

    def test_cisa_kev_only_adds_ai_relevant_entries(self):
        data = {"vulnerabilities": [
            {"cveID": "CVE-2026-99999", "dateAdded": news.NOW.date().isoformat(),
             "vulnerabilityName": "AI Agent Prompt Injection Vulnerability",
             "shortDescription": "An AI agent can execute malicious instructions."},
            {"cveID": "CVE-2026-88888", "dateAdded": news.NOW.date().isoformat(),
             "vulnerabilityName": "Router Buffer Overflow", "shortDescription": "A router can crash."}]}
        with patch.object(news, "get_json", return_value=data):
            items = news.collect_kev()
        self.assertEqual([item["title"].split(":")[0] for item in items], ["CVE-2026-99999"])

    def test_sync_assigns_an_image_when_publisher_has_none(self):
        item = self.story("AI agent prompt injection vulnerability", url="https://example.org/research", origin="research")
        item.update(source="Example Research", published="2026-11-01")
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "news.json"
            seed = Path(directory) / "seed.json"
            archive.write_text("[]")
            seed.write_text(json.dumps([item]))
            with patch.object(news, "DATA", archive):
                news.main(seed, 1)
            self.assertEqual(json.loads(archive.read_text())[0]["image"], news.FALLBACK_IMAGE)

    def test_publisher_image_replaces_fallback(self):
        item = self.story("AI agent prompt injection vulnerability", url="https://example.org/research", origin="research")
        item["image"] = news.FALLBACK_IMAGE
        page = b'<html><meta property="og:image" content="/preview.png"></html>'
        headers = Message()
        headers["Content-Type"] = "text/html"
        with patch.object(news, "get", return_value=(page, item["url"], headers)):
            self.assertTrue(news.preview(item))
        self.assertEqual(item["image"], "https://example.org/preview.png")

    def test_daily_collection_assigns_an_image_to_new_stories(self):
        item = self.story("AI agent prompt injection vulnerability", url="https://example.org/research", origin="research")
        item.update(source="Example Research", published=news.NOW.date().isoformat())
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "news.json"
            archive.write_text("[]")
            with patch.object(news, "DATA", archive), \
                 patch.object(news, "collect_anthropic", return_value=[item]), \
                 patch.object(news, "collect_hn", return_value=[]), \
                 patch.object(news, "collect_kev", return_value=[]), \
                 patch.object(news, "collect_nvd", return_value=[]), \
                 patch.object(news, "rss_stories", return_value=[]), \
                 patch.object(news, "preview", return_value=False):
                news.main()
            self.assertEqual(json.loads(archive.read_text())[0]["image"], news.FALLBACK_IMAGE)


if __name__ == "__main__":
    unittest.main()
