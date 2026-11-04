# Vietnamese job-board access research

_Investigated 4 November 2026. This note records observed access and published
integration routes. It does not authorize bypassing a board's access controls or
terms._

## What the blocked responses mean

The MVP's direct, read-only probes returned HTTP 403 from TopCV, Indeed Vietnam,
and JobsGO. A 403 means the server declined that client or request; it does not
mean there are no jobs and it does not identify a safe technical workaround.
TopCV's `robots.txt` permits its public search route, so the 403 is likely an
application/WAF policy rather than a robots exclusion. That distinction still
does not grant permission to evade it. [TopCV robots.txt](https://www.topcv.vn/robots.txt)

The other boards that returned HTTP 200 need separate validation: an accessible
page can be an application shell without embedded listings. The first MVP probes
found usable public HTML and detail data at CareerViet; that is the strongest
direct connector candidate. VietnamWorks and Vieclam24h returned public pages
but need source-specific parsing and repeated live checks. JoyWork was reachable,
but its public root page was not enough to establish a jobs-search adapter.

## Viable routes

| Source | Current direct-MVP evidence | Legitimate integration route | Recommendation |
|---|---|---|---|
| TopCV | Direct search returned 403. | TopCV documents a partner API for employer accounts and applicant-CV synchronisation, not a public candidate job-search API. It requires partner credentials plus an employer token. [Partner connection](https://docs-tuyendung.topcv.vn/partner-api/connection.html), [CV sync service](https://tuyendung.topcv.vn/help/cac-cau-hoi-thuong-gap/dich-vu-api/) | Ask TopCV for a candidate-job-search or syndication feed. Until then, support user-supplied postings and agent web-search discovery; do not build a 403-bypass connector. |
| Indeed Vietnam | Direct search returned 403. | Indeed's documented integrations are employer/ATS XML-feed and application integrations, not a public job-search API. [Indeed integration checklist](https://developer.indeed.com/public/pdf/indeed-apply/integration-review-checklist.pdf) | Treat this as a discovery-only source until Indeed provides a suitable partner arrangement. |
| VietnamWorks | Public search page returned 200 but results may be client-rendered. | Published employer API documentation describes OAuth access to an employer's own listings, requiring an issued consumer key and employer authorization; it cannot search the overall candidate market. [Employer API documentation](https://github.com/chrisshayan/vietnamworks/blob/master/employer-api-docs/README.md) | Build a small public-page adapter only if repeated tests prove its public data is stable and permitted. For a paid third-party option, evaluate a VietnamWorks-specific Apify Actor as a vendor integration, with a cost cap and validation. [Actor listing](https://apify.com/usefulapi/vietnamworks-scraper), [Apify Actor terms](https://docs.apify.com/legal/actor-terms-and-conditions) |
| CareerViet | Public search HTML and a public JobPosting schema record were retrieved. | No candidate-search API found. Its robots file allows several named bots but has route-specific restrictions, so keep the adapter limited to public search/detail routes and re-check policy when routes change. [CareerViet robots.txt](https://careerviet.vn/robots.txt) | First direct connector. Parse the public first page and `JobPosting` JSON-LD detail records; add fixture-based tests and a health check before enabling it by default. |
| Vieclam24h | Public search page returned 200 and embedded initial state contained listings. | No candidate-search API found. The robots policy has route restrictions. [Vieclam24h robots.txt](https://vieclam24h.vn/robots.txt) | Second direct connector candidate. Extract only embedded public page data; add a parser fixture, pagination evidence, and a detail-page test. |
| JoyWork | Public root and `/jobs` page returned 200, but no verified listing structure yet. | No public API found. Its robots file carries Cloudflare content signals and prohibits several AI bots. [JoyWork robots.txt](https://joywork.vn/robots.txt) | Do not send retrieved content into an LLM unless its policy permits that use. Seek written permission or a partner feed before adding it to agent ranking. |
| JobsGO | Direct robots and search requests returned 403. | No candidate job-search API found. The public employer material is for posting/recruitment operations. [JobsGO employer posting](https://employer.jobsgo.vn/viec-nhanh/dang-tin) | Request a feed or partnership from JobsGO; otherwise treat as unavailable for automated retrieval. |

## Community tools: useful evidence, not a dependency by default

[Crawl-Jobs](https://github.com/hieu-isods/Crawl-Jobs) is an open-source Scrapy
pipeline that claims adapters for TopCV, VietnamWorks, CareerViet, and JobsGO.
It can be studied for its normalized data model and test ideas. It is not a
reliable drop-in dependency for this MVP: it is an independent project, its
documentation calls for credentials for some sources, and its own crawler
behaviour must be reviewed against current source policies before running it.

Apify has community-maintained VietnamWorks Actors, including
[usefulapi/vietnamworks-scraper](https://apify.com/usefulapi/vietnamworks-scraper).
An Actor can be called through its API and returns structured datasets, which is
technically suitable for Gemini CLI or Codex. It is a paid third-party service,
not an official VietnamWorks integration, and its availability, cost, output
quality, terms, and privacy handling need a small trial before adoption. Apify
classifies these as community Actors, and users remain responsible for legitimate
use and each Actor's applicable terms. [Apify Actor terms](https://docs.apify.com/legal/actor-terms-and-conditions)

### Crawl-Jobs audit (4 November 2026)

I reviewed commit `aa68524` of [Crawl-Jobs](https://github.com/hieu-isods/Crawl-Jobs).
It is **not suitable to run or import as an MVP dependency**. Its settings file
first enables then later disables `ROBOTSTXT_OBEY`, leaving robots enforcement
off. Its Selenium code uses a browser-shaped user agent and anti-detection
settings, including replacing `navigator.webdriver`; the ITviec path also
disables browser web security. [Settings](https://github.com/hieu-isods/Crawl-Jobs/blob/aa68524b1fbaf8e60cd744eb9eddff69e5204a3b/CrawlJob/settings.py), [Selenium middleware](https://github.com/hieu-isods/Crawl-Jobs/blob/aa68524b1fbaf8e60cd744eb9eddff69e5204a3b/CrawlJob/selenium_middleware.py), [ITviec spider](https://github.com/hieu-isods/Crawl-Jobs/blob/aa68524b1fbaf8e60cd744eb9eddff69e5204a3b/CrawlJob/spiders/itviec_spider.py)

It also ships an unauthenticated local Postgres and API stack with default
passwords and host-port publishing in its Compose file. Its dependencies are
pinned, but the project has no automated test suite, no CI workflows, and its
last visible commit is from October 2025. [Compose configuration](https://github.com/hieu-isods/Crawl-Jobs/blob/aa68524b1fbaf8e60cd744eb9eddff69e5204a3b/docker-compose.yml), [dependency list](https://github.com/hieu-isods/Crawl-Jobs/blob/aa68524b1fbaf8e60cd744eb9eddff69e5204a3b/requirements.txt)

The code is syntactically valid when compiled in an isolated cache, but that
only establishes that Python can parse it. A functioning live crawl remains
unverified: the project lacks fixtures or integration tests, its selectors are
specific to prior page designs, and a live run would exercise the anti-detection
paths above. Treat it as a reference for data fields only, not as a component.

No maintained MCP server or credible public candidate-search API was found for
TopCV, Indeed Vietnam, JobsGO, JoyWork, CareerViet, or Vieclam24h. That is a
finding of this targeted review, not proof that none exists. Any new candidate
should be accepted only after verifying its source code, owner, license, cost,
data handling, and the board's current access policy.

## Integration decision

| Job board | Integrate now? | Integration route | Condition before release |
|---|---:|---|---|
| CareerViet | Yes | Direct public search and JobPosting detail pages | Fixture-backed search/detail parsing, robots-aware health check, and a small live smoke test |
| Vieclam24h | Next | Public search page's embedded job data and public detail pages | Confirm detail-page extraction, pagination behavior, and current route permissions |
| VietnamWorks | Conditional | Direct public-page adapter, or optional paid vendor provider | Validate public data stability; any vendor must pass a small quality, privacy, cost, and terms trial |
| TopCV | No direct adapter | Official candidate-job feed or approved vendor only | Written permission or a documented candidate-search integration; retain paste/import fallback meanwhile |
| Indeed Vietnam | No direct adapter | Approved Indeed partner integration only | An approved use case and credentials from Indeed |
| JobsGO | No direct adapter | Partner feed only | A documented candidate-search feed or written permission |
| JoyWork | Not yet | Partner feed or explicit permission | Confirm that collection and LLM use are permitted under its published content policy |

The first deliverable should therefore cover **CareerViet**, report TopCV,
Indeed Vietnam, and JobsGO as unavailable rather than empty, and preserve source
provenance on every result. This gives Gemini CLI and Codex a reliable path to
useful Vietnamese results while additional board relationships are established.

## Recommended MVP shape

1. Ship a direct CareerViet connector, because it has the best observed
   search-to-detail path.
2. Add Vieclam24h only after a fixture-backed search and detail test succeeds.
3. Keep TopCV as an explicit `blocked` source, with a user-paste/import path and
   a request for an official feed. This gives the cousin useful results without
   pretending the board is covered.
4. Run a low-volume health check before searches and report source status in
   every result. A blocked source should appear as `unavailable`, never as zero
   results.
5. If VietnamWorks coverage is important, run a short, budget-limited trial of
   one community Actor, compare five returned records against the public board,
   and integrate it behind an optional provider setting rather than embedding a
   vendor credential in the repository.
