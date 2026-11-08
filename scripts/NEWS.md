# Daily AI security news

The scheduled workflow runs at **11:30 UTC**. GitHub may deliver scheduled runs
late. The collector uses the actual run's UTC date, not a rolling lookback window.
It accepts at most **five distinct stories published on that date**, including
any already added by an earlier manual or scheduled run. It can return fewer
when there are not five eligible stories. Historical articles remain visible.

## Sources and selection

`news_sources.json` lists 19 direct publisher feeds. These are supplemented by
Anthropic research, Hacker News discovery, CISA KEV, the complete daily NVD
publication stream, and GitHub Security Advisories (24 collectors in total).
Research and advisories rank first. Selection rotates publishers to encourage
variety without discarding relevant stories just to fill a source quota.

RSS/Atom publication timestamps and publisher `datePublished`/Open Graph dates
establish the publication day. `dateModified`, sitemap modification dates, and
HN submission times do not turn old articles into today's news. A first-party
HN text post or linked X post can use its own creation timestamp. Unreviewed
GitHub CVE imports are checked against the original CVE publication record.
Unknown publication dates, sponsored posts, course promotions, and general
news roundups are excluded. Google News wrapper URLs are no longer ingested.

## Story deduplication

Canonical URLs, CVEs, normalized headlines, shared event vocabulary and
transaction facts are compared across sources. The regression suite contains
the reported Palo Alto/Clutch and CrowdStrike headline variants, paraphrased
credential leaks, and distinct events that must remain separate. Merged aliases
are retained. `news-seen.json` keeps identity information after a story leaves
the 80-entry display archive, so expired stories cannot be imported again.

Matching free-form reports is heuristic: ambiguous wording can require an
additional regression fixture. Every run records duplicate decisions in its
selection report and refuses to publish duplicates detected by the validator.

## Previews

Publisher images are downloaded, fully decoded with Pillow, checked for minimum
dimensions, resized, and stored as WebP in `assets/news/`. Visitors load them
from this site, not a publisher's hotlink. HTML error pages, tiny images, oversized
responses, corrupt files and known generic publisher logos are rejected.

Every article also gets its own local SVG headline card, containing its title,
source and publication date. This is the preview when no usable publisher image
exists. `image_kind` explicitly distinguishes `publisher` from `headline`.
The browser falls back to that card, then an inline article-specific SVG if a
file is unexpectedly missing. The pipeline validates every referenced asset
before committing the archive, identity history and images together.

## Run and inspect

```sh
python -m pip install -r scripts/requirements-news.txt
python scripts/update_news.py --report /tmp/news-run.json
python scripts/update_news.py --validate-only
python -m unittest discover -s tests -v
node --test tests/test_news_preview.js
```

`--date YYYY-MM-DD` selects a specific UTC publication day and still applies the
five-story limit and persistent dedupe. `--repair-archive` repairs previews and
deduplicates the archive without adding stories. A corrupt archive or an outage
of every collector fails the run without replacing the archive.

On GitHub, use **Actions → Update AI security news → Run workflow** for a manual
refresh. Open a completed run's summary and download **news-selection-report**
to see source availability, candidates, exclusions, additions and preview types.
The daily schedule remains in `.github/workflows/update-ai-security-news.yml`.
