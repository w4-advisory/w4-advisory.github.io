import copy
import datetime as dt
from email.message import Message
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import update_news as news
from news_dedupe import canonical_url, dedupe_archive, duplicate_reason
from news_images import ensure_preview, valid_asset, validate_previews
from PIL import Image

NOW = dt.datetime(2026, 11, 8, 23, 30, tzinfo=dt.timezone.utc)
TODAY = '2026-11-08'


def story(title='Claude prompt injection bypass exposes user secrets', **kwargs):
    return dict({'title': title, 'url': 'https://research.example/article', 'source': 'Example Research',
                 'origin': 'research', 'published': TODAY, 'published_at': TODAY + 'T10:00:00+00:00',
                 'date_source': 'https://research.example/feed#feed-published'}, **kwargs)


def response(raw, mime='text/html', final='https://research.example/article'):
    headers = Message()
    headers['Content-Type'] = mime
    return raw, final, headers


class DatesAndSourcesTests(unittest.TestCase):
    def setUp(self):
        self.now = patch.object(news, 'NOW', NOW)
        self.day = patch.object(news, 'RUN_DAY', NOW.date())
        self.now.start(); self.day.start()
        self.addCleanup(self.now.stop); self.addCleanup(self.day.stop)

    def test_only_selected_utc_day_and_never_future_times(self):
        for date in (TODAY, TODAY + 'T00:00:00Z', '2026-11-07T23:30:00-01:00'):
            self.assertTrue(news.recent(date), date)
        for date in ('2026-11-07T23:59:59Z', '2026-11-03', '2026-11-09', TODAY + 'T23:59:00Z', 'bad', None):
            self.assertFalse(news.recent(date), date)

    def test_summer_still_uses_utc_not_london(self):
        with patch.object(news, 'NOW', dt.datetime(2026, 7, 8, 23, 30, tzinfo=dt.timezone.utc)), patch.object(news, 'RUN_DAY', dt.date(2026, 7, 8)):
            self.assertTrue(news.recent('2026-07-09T00:15:00+01:00'))
            self.assertFalse(news.recent('2026-07-09T00:15:00Z'))

    def test_jsonld_published_wins_over_feed_and_modified_is_ignored(self):
        item = story()
        page = b'<script type="application/ld+json">{"@graph":[{"@type":"NewsArticle","datePublished":"2026-11-06T20:00:00Z","dateModified":"2026-11-08T12:00:00Z"}]}</script>'
        with patch.object(news, 'get', return_value=response(page)):
            self.assertTrue(news.preview(item))
        self.assertEqual(item['published'], '2026-11-06')
        self.assertFalse(news.recent(item['published_at']))

    def test_rss_rejects_yesterday_and_publisher_recycled_article(self):
        xml = b'<rss><channel><item><title>Claude prompt injection vulnerability</title><link>https://research.example/a</link><pubDate>Sun, 08 Nov 2026 10:00:00 +0000</pubDate></item><item><title>AI security flaws</title><link>https://research.example/b</link><pubDate>Sat, 07 Nov 2026 10:00:00 +0000</pubDate></item></channel></rss>'
        page = b'<meta property="article:published_time" content="2026-11-01T12:00:00Z">'
        with patch.object(news, 'get', side_effect=[response(xml, 'application/rss+xml'), response(page)]):
            self.assertEqual(news.rss_stories('https://research.example/feed', 'research'), [])

    def test_atom_uses_published_and_rejects_updated_only(self):
        xml = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>AI security research</title><link href="https://research.example/a"/><published>2026-11-08T10:00:00Z</published></entry><entry><title>AI security stale paper</title><link href="https://research.example/b"/><updated>2026-11-08T10:00:00Z</updated></entry></feed>'
        with patch.object(news, 'get', return_value=response(xml, 'application/atom+xml')), patch.object(news, 'preview', return_value=False):
            items = news.rss_stories('https://research.example/feed', 'research')
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]['date_source'].endswith('#feed-published'))

    def test_hn_submission_does_not_redate_old_or_undated_article(self):
        hits = {'hits': [{'objectID': '123', 'title': 'New Claude prompt injection vulnerability', 'url': 'https://research.example/old', 'created_at': TODAY + 'T10:00:00Z', 'points': 20}], 'nbPages': 1}
        with patch.object(news, 'get_json', return_value=hits), patch.object(news, 'preview', return_value=False):
            self.assertEqual(news.collect_hn(), [])

    def test_generic_site_metadata_cannot_replace_an_article_headline(self):
        item = story('Claude Code approval files expose passwords in GitHub repositories', summary='Original findings.')
        page = b'<meta property="og:title" content="Vendor: Secure your development"><meta property="og:description" content="Buy our product"><meta property="og:image" content="https://example.org/home.jpg">'
        with patch.object(news, 'get', return_value=response(page)):
            self.assertFalse(news.preview(item))
        self.assertEqual(item['summary'], 'Original findings.')
        self.assertNotIn('_images', item)

    def test_cve_import_does_not_count_as_new_publication(self):
        advisory = {'ghsa_id': 'GHSA-example', 'cve_id': 'CVE-2026-12345', 'html_url': 'https://github.com/advisories/GHSA-example',
                    'type': 'unreviewed', 'published_at': TODAY + 'T18:00:00Z', 'description': 'MCP gateway prompt injection vulnerability'}
        def fetch(url):
            return {'cveMetadata': {'datePublished': '2026-11-01T10:00:00Z'}} if 'cveawg.mitre.org' in url else [advisory]
        with patch.object(news, 'get_json', side_effect=fetch):
            self.assertEqual(news.collect_github_advisories(), [])

    def test_unreachable_source_does_not_erase_other_candidates(self):
        with patch.object(news, 'FEEDS', []), patch.object(news, 'collect_anthropic', side_effect=OSError('down')), patch.object(news, 'collect_hn', return_value=[story()]), patch.object(news, 'collect_kev', return_value=[]), patch.object(news, 'collect_nvd', return_value=[]), patch.object(news, 'collect_github_advisories', return_value=[]):
            candidates, statuses = news.collect_sources()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(sum(s['status'] == 'error' for s in statuses), 1)

    def test_advisory_sources_use_official_publication_dates(self):
        nvd = {'vulnerabilities': [{'cve': {'id': 'CVE-2026-12345', 'published': TODAY + 'T12:00:00Z', 'descriptions': [{'lang': 'en', 'value': 'AI agent MCP prompt injection allows arbitrary code execution.'}]}}]}
        with patch.object(news, 'get_json', return_value=nvd), patch.object(news.time, 'sleep'):
            items = news.collect_nvd()
        self.assertTrue(items)
        self.assertTrue(all(s['published'] == TODAY and s['date_source'].endswith('#published') for s in items))

    def test_relevance_keeps_policy_and_technical_research_but_rejects_ads(self):
        for title in ('IIT Delhi launches a course in cybersecurity and AI security', 'Sponsored: new AI security product', 'AI security bootcamp discount'):
            self.assertEqual(news.relevance_score(story(title)), 0)
        for title in ('Discovering vulnerabilities with Claude', 'Autonomous Vulnerability Hunting with MCP', 'Macron gathers specialists for AI security briefing'):
            self.assertGreater(news.relevance_score(story(title)), 0)
        self.assertEqual(news.relevance_score(story('New GPU benchmark for LLM inference')), 0)
        self.assertEqual(news.relevance_score(story('Week in review: refrigeration cyberattacks', summary='AI security news and other stories')), 0)


class DeduplicationTests(unittest.TestCase):
    def pair(self, left, right, **kwargs):
        return story(left, **kwargs), story(right, url='https://other.example/different-url', source='Other Publisher', **kwargs)

    def test_palo_alto_all_three_actual_headlines_merge(self):
        headlines = [
            'Palo Alto Networks In Talks To Pay $500 Million For AI Security Startup Clutch Security - The Information - TradingView',
            'Palo Alto Networks Eyes $500 Million AI Security Deal - TradingView',
            'Palo Alto Networks in talks to buy AI security startup Clutch for $500M: report - TradingView']
        items = [story(t, url=f'https://publisher.example/{i}', source='TradingView') for i, t in enumerate(headlines)]
        kept, removed = dedupe_archive(items)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(removed), 2)
        self.assertEqual(len(kept[0]['aliases']), 2)

    def test_crowdstrike_cross_publisher_duplicates_merge(self):
        left, right = self.pair('CrowdStrike Reportedly Paying $495 Million for AI Security Firms Fiddler, Dreadnode - SecurityWeek', 'CrowdStrike to buy two AI security startups for $495M: WSJ - TradingView')
        self.assertIsNotNone(duplicate_reason(left, right))

    def test_paraphrased_vulnerability_without_same_title(self):
        left, right = self.pair('Claude Code leaks credentials through GitHub settings files', 'GitHub settings files expose Claude Code passwords')
        self.assertIsNotNone(duplicate_reason(left, right))

    def test_same_company_different_event_is_kept(self):
        left, right = self.pair('CrowdStrike to buy AI security firms for $495 million', 'CrowdStrike announces new native AI security innovations on AWS')
        self.assertIsNone(duplicate_reason(left, right))
        left, right = self.pair('Palo Alto in talks to buy Clutch for $500 million', 'Microsoft in talks to buy ExampleCorp for $500 million')
        self.assertIsNone(duplicate_reason(left, right))

    def test_distinct_cves_in_same_product_are_kept(self):
        left, right = self.pair('CVE-2026-12345: MCP Gateway authentication bypass', 'CVE-2026-99999: MCP Gateway authentication bypass')
        self.assertIsNone(duplicate_reason(left, right))

    def test_same_cve_across_different_publishers(self):
        left, right = self.pair('CVE-2026-12345: MCP Gateway authentication bypass', 'Critical AI gateway flaw allows token theft')
        right['summary'] = 'Advisory for CVE-2026-12345.'
        self.assertEqual(duplicate_reason(left, right), 'same CVE')

    def test_canonical_tracking_and_recorded_aliases(self):
        left = story(url='https://www.example.com/news/?utm_source=feed')
        right = story('Completely rewritten title', url='https://example.com/news')
        self.assertEqual(duplicate_reason(left, right), 'canonical URL')
        right['url'] = 'https://elsewhere.example/repost'
        left['aliases'] = [dict(right)]
        self.assertEqual(duplicate_reason(left, right), 'canonical URL')
        self.assertNotEqual(canonical_url('https://example.com/item?id=1'), canonical_url('https://example.com/item?id=2'))


class PreviewTests(unittest.TestCase):
    def test_missing_broken_html_and_tiny_images_get_unique_local_cards(self):
        for kind in ('missing', 'html', 'tiny', 'error'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                item = story(image='https://images.example/not-image')
                small = io.BytesIO()
                Image.new('RGB', (1, 1)).save(small, 'PNG')
                def fetch(*args):
                    if kind == 'error': raise OSError('blocked')
                    return response(small.getvalue(), 'image/png') if kind == 'tiny' else response(b'<html>blocked</html>')
                if kind == 'missing': item.pop('image')
                ensure_preview(item, root, fetch)
                self.assertEqual(item['image_kind'], 'headline')
                self.assertTrue(valid_asset(item['image'], root))
                other = story('Different MCP sandbox escape', url='https://research.example/other')
                ensure_preview(other, root, fetch)
                self.assertNotEqual(item['image'], other['image'])
                self.assertIn('Different MCP sandbox escape', (root / other['image']).read_text())
                validate_previews([item, other], root)

    def test_real_image_is_decoded_cached_and_does_not_need_hotlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = io.BytesIO()
            Image.new('RGB', (1200, 675), '#123456').save(content, 'PNG')
            item = story(_images=['https://images.example/photo.png'])
            with patch('builtins.print'):
                ensure_preview(item, root, lambda *args: response(content.getvalue(), 'image/png'))
            self.assertEqual(item['image_kind'], 'publisher')
            self.assertTrue(item['image'].endswith('.webp'))
            ensure_preview(item, root, lambda *args: self.fail('Cached image must not be downloaded again'))
            validate_previews([item], root)

    def test_missing_cached_file_is_repaired_and_validation_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = story(image='assets/news/' + 'a' * 24 + '.webp', image_kind='publisher')
            with self.assertRaises(ValueError): validate_previews([item], root)
            ensure_preview(item, root, lambda *args: (_ for _ in ()).throw(OSError('offline')))
            self.assertEqual(item['image_kind'], 'headline')
            validate_previews([item], root)

    def test_empty_metadata_cannot_turn_article_url_into_image(self):
        self.assertIsNone(news.https_url('', 'https://example.org/article'))
        self.assertIsNone(news.https_url(None, 'https://example.org/article'))


class PipelineTests(unittest.TestCase):
    def candidates(self):
        titles = [
            'MCP gateway authentication bypass CVE-2026-12345',
            'Claude sandbox escape through file uploads',
            'Ollama model poisoning via unsafe deserialization',
            'Gemini browser prompt injection steals session tokens',
            'LangChain agent remote code execution CVE-2026-23456',
            'Anthropic AI security policy briefing',
            'Codex malicious plugin supply chain vulnerability',
        ]
        return [story(t, url=f'https://research{i}.example/article', source=f'Research {i}') for i, t in enumerate(titles)]

    def test_five_per_publication_day_repeated_run_ignores_old_addition_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'news.json'
            # An old article added today must not consume today's publication budget.
            old = story('Earlier AI security policy research', url='https://old.example/article', published='2026-11-06', published_at='2026-11-06T12:00:00Z', added_on=TODAY)
            archive.write_text(json.dumps([old]))
            stale = story('Old Claude vulnerability', url='https://stale.example/article', published='2026-11-07', published_at='2026-11-07T12:00:00Z')
            found = self.candidates() + [stale] + self.candidates()
            with patch.object(news, 'ROOT', root), patch.object(news, 'DATA', archive), patch.object(news, 'NOW', NOW), patch.object(news, 'RUN_DAY', NOW.date()), patch.object(news, 'collect_sources', return_value=(found, [{'source':'test', 'status':'ok'}])), patch.object(news, 'preview', return_value=False):
                first = news.main()
                original = archive.read_bytes()
                second = news.main()
            self.assertEqual(len(first['added']), 5)
            self.assertEqual(len(second['added']), 0)
            self.assertEqual(archive.read_bytes(), original)
            data = json.loads(original)
            self.assertEqual(len(data), 6)
            self.assertEqual(sum(s['published'] == TODAY for s in data), 5)
            self.assertTrue(all('stale.example' not in s['url'] for s in data))
            validate_previews(data, root)

    def test_partial_day_fills_remaining_slots_and_honours_publisher_diversity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive = root / 'news.json'
            candidates = self.candidates()
            candidates[0]['source'] = candidates[1]['source'] = candidates[2]['source'] = 'Same Publisher'
            archive.write_text(json.dumps(candidates[:2]))
            with patch.object(news, 'ROOT', root), patch.object(news, 'DATA', archive), patch.object(news, 'NOW', NOW), patch.object(news, 'RUN_DAY', NOW.date()), patch.object(news, 'collect_sources', return_value=(candidates, [{'source':'test', 'status':'ok'}])), patch.object(news, 'preview', return_value=False):
                result = news.main()
            data = json.loads(archive.read_text())
            self.assertEqual(len(result['added']), 3)
            self.assertEqual(sum(s['source'] == 'Same Publisher' for s in data), 2)

    def test_all_sources_down_preserves_archive_and_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'news.json'; archive.write_text('[]\n')
            with patch.object(news, 'DATA', archive), patch.object(news, 'collect_sources', return_value=([], [{'source':'test','status':'error'}])):
                with self.assertRaises(RuntimeError): news.main()
            self.assertEqual(archive.read_text(), '[]\n')

    def test_retired_archive_stories_still_cannot_return(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive = root / 'news.json'; archive.write_text('[]')
            retired = story(published='2026-01-01', published_at='2026-01-01T10:00:00Z')
            (root / 'news-seen.json').write_text(json.dumps([retired]))
            new_copy = story('Rewritten coverage of the same Claude vulnerability')
            with patch.object(news, 'ROOT', root), patch.object(news, 'DATA', archive), patch.object(news, 'NOW', NOW), patch.object(news, 'RUN_DAY', NOW.date()), patch.object(news, 'collect_sources', return_value=([new_copy], [{'source':'test','status':'ok'}])), patch.object(news, 'preview', return_value=False):
                report = news.main()
            self.assertEqual(report['added'], [])
            self.assertTrue(report['rejected'][0]['reason'].startswith('duplicate'))

    def test_corrupt_archive_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'news.json'; archive.write_text('broken')
            with patch.object(news, 'DATA', archive):
                with self.assertRaises(ValueError): news.existing()


if __name__ == '__main__':
    unittest.main()
