import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("update_news", Path(__file__).parents[1] / "scripts/update_news.py")
news = importlib.util.module_from_spec(spec)
spec.loader.exec_module(news)


class SelectionTests(unittest.TestCase):
    def story(self, title, summary="", url="https://example.org/story", origin="news"):
        return {"title": title, "summary": summary, "url": url, "origin": origin}

    def test_rejects_course_even_when_ai_security_keywords_match(self):
        item = self.story("IIT Delhi launches a course in cybersecurity and AI security")
        self.assertEqual(news.relevance_score(item), 0)

    def test_rejects_marketing_and_unrelated_ai_security_briefing(self):
        self.assertEqual(news.relevance_score(self.story("CrowdStrike announces new native AI security innovations")), 0)
        self.assertEqual(news.relevance_score(self.story("Macron gathers specialists for AI security briefing")), 0)

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


if __name__ == "__main__":
    unittest.main()
