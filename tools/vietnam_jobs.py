#!/usr/bin/env python3
"""Portable Vietnamese job retrieval: sources, health, search, detail and import.

Python 3.10+, curl, and standard library only. No AI provider dependency.
Network requests are bounded, source-allowlisted, and robots-checked.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.parse import quote, urljoin, urlsplit

from posting_guard import guard
from robots_check import gate

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "job_scraper/vietnam_sources.json"
UA = "Mozilla/5.0 (compatible; vietnam-jobs-cli/0.1)"
MAX_BYTES = 5 * 1024 * 1024
SOURCES = {s["id"]: s for s in json.loads(REGISTRY.read_text(encoding="utf-8"))["sources"]}
PROBES = {key: source["probe_url"] for key, source in SOURCES.items()}
HOSTS = {key: urlsplit(url).hostname for key, url in PROBES.items()}


class Failure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def enabled(source):
    # Fail closed for future/unverified entries without an explicit opt-in.
    return SOURCES[source].get("enabled") is True


def require_enabled(source):
    if not enabled(source):
        raise Failure("SOURCE_DISABLED", f"{source} is disabled for automatic retrieval. "
                      "Manual import is available without network access. To explicitly probe access, "
                      f"run health --source {source}; a probe does not enable this source.")


def source_url(value, source, *, posting=False):
    try:
        p = urlsplit(value)
        host = (p.hostname or "").removeprefix("www.")
        expected = HOSTS[source].removeprefix("www.")
        api_reference = source == "joywork" and not posting and p.hostname == "api.joywork.vn" and p.path == "/api/jobs"
        valid = (p.scheme == "https" and (host == expected or api_reference) and not p.username
                 and not p.password and p.port in (None, 443))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise Failure("BAD_URL", f"Expected an HTTPS URL on {HOSTS[source]}")
    if posting:
        patterns = {
            "topcv": r"/viec-lam/.+/\d+\.html$",
            "careerviet": r"/(?:vi|en)/tim-viec-lam/[^/]+\.[A-Za-z0-9]+\.html$",
            "indeed": r"/(?:viewjob|rc/clk)$",
            "vietnamworks": r"/[^/]+-\d+-j[vd]$",
            "joywork": r"/jobs/[^/]+/?$",
            "jobsgo": r"/viec-lam/[^/]+\.html$",
            "vieclam24h": r"/[^/]+/[^/]+id\d+\.html$",
        }
        if not re.fullmatch(patterns[source], p.path):
            raise Failure("BAD_URL", "Expected an individual posting URL, not a search or home page")
    return p._replace(fragment="").geturl()


def fetch(url, source, *, policy_cache=None):
    """GET only; enforce policy and host on every redirect, never follow body links."""
    for _ in range(4):
        url = source_url(url, source)
        rc, message = gate(url) if policy_cache is None else gate(url, policy_cache=policy_cache)
        if rc:
            raise Failure("ACCESS_POLICY", message)
        with tempfile.TemporaryDirectory(prefix="vietnam-jobs-") as directory:
            body = Path(directory) / "body"
            headers = Path(directory) / "headers"
            for attempt in range(3):
                result = subprocess.run([
                    "curl", "-sS", "--compressed", "--proto", "=https",
                    "--max-time", "20", "--max-filesize", str(MAX_BYTES),
                    "-A", UA, "-D", str(headers), "-o", str(body),
                    "-w", "%{http_code}", "--", url,
                ], capture_output=True, text=True, timeout=25)
                if result.returncode:
                    raise Failure("TRANSPORT", result.stderr.strip() or "curl failed")
                status = int(result.stdout or 0)
                if status == 429 or 500 <= status < 600:
                    if attempt < 2:
                        # Bound retries; a longer Retry-After is left to the user.
                        header_text = headers.read_text(errors="replace")
                        if re.search(r"(?im)^retry-after:", header_text):
                            raise Failure("RATE_LIMITED", f"HTTP {status}; retry later")
                        time.sleep(2 ** attempt)
                        continue
                break
            if 300 <= status < 400:
                match = re.search(r"(?im)^location:\s*(.+)", headers.read_text())
                if not match:
                    raise Failure("REDIRECT", "Redirect without a Location header")
                url = urljoin(url, match[1].strip())
                continue
            if status in (401, 403, 429):
                raise Failure("BLOCKED", f"HTTP {status}: access blocked, not zero results")
            if status != 200:
                raise Failure("HTTP_ERROR", f"HTTP {status}; posting availability unknown")
            raw = body.read_bytes()
            if len(raw) > MAX_BYTES:
                raise Failure("TOO_LARGE", "Page exceeds size limit")
            text = raw.decode("utf-8", errors="replace")
            if re.search(r"<title[^>]*>\s*(?:Just a moment|Access denied|Attention Required)", text, re.I):
                raise Failure("BLOCKED", "Challenge page, not job results")
            return text
    raise Failure("REDIRECT", "Too many redirects")


class Node:
    def __init__(self, tag="root", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def hidden(self):
        return ("hidden" in self.attrs or self.attrs.get("aria-hidden") == "true"
                or bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:;|$)",
                                  self.attrs.get("style", ""), re.I)))

    def all(self, visible=False):
        if visible and (self.hidden() or self.tag in ("script", "style", "template", "noscript", "svg")):
            return
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.all(visible)

    def has(self, name):
        return name in self.attrs.get("class", "").split()

    def text(self):
        # Skip hidden/script content before exposing any listing fields.
        if self.tag in ("script", "style", "template", "noscript", "svg"):
            return ""
        if self.hidden():
            return ""
        return " ".join(c.text() if isinstance(c, Node) else c for c in self.children)


class Document(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def clean(value, mode="text"):
    text, findings = guard(str(value or ""), mode)
    return text.strip(), findings["verdict"]


def field(node, class_name):
    match = next((n for n in node.all(visible=True) if n.has(class_name)), None)
    return clean(match.text())[0] if match else None


def career_search(markup):
    # Parse each card separately so fields cannot bleed between jobs.
    doc = Document(markup)
    # Next.js streams the result list inside a hidden S:n container and moves
    # it into template B:n via React's $RC call. Recognize that exact pairing
    # without executing any JavaScript; unrelated hidden content stays hidden.
    nodes = list(doc.root.all())
    templates = {n.attrs.get("id") for n in nodes if n.tag == "template"}
    revealed = set()
    for node in nodes:
        if node.tag == "script":
            raw = "".join(c for c in node.children if isinstance(c, str))
            for boundary, segment in re.findall(r'\$RC\("(B:\d+)","(S:\d+)"\)', raw):
                if boundary in templates:
                    revealed.add(segment)
    for node in nodes:
        if node.tag == "div" and node.attrs.get("id") in revealed:
            node.attrs.pop("hidden", None)
    jobs, seen = [], set()
    for card in doc.root.all(visible=True):
        if not card.has("job-item"):
            continue
        link = next((n for n in card.all(visible=True) if n.tag == "a" and n.has("job_link")), None)
        if not link:
            continue
        url = source_url(urljoin(PROBES["careerviet"], link.attrs.get("href", "")), "careerviet", posting=True)
        if url in seen:
            continue
        title = clean(link.text())[0]
        company = field(card, "company-name")
        if not title or not company:
            raise Failure("PARSE_CHANGED", "CareerViet returned a job card without title or company")
        loc = next((n for n in card.all(visible=True) if n.has("location")), None)
        location = clean(loc.attrs.get("title") or loc.text())[0] if loc else None
        jobs.append({"id": link.attrs.get("data-id") or url, "title": title,
                     "company": company, "location": location, "date": None,
                     "url": url, "salary": field(card, "salary"),
                     "portal": "careerviet", "source": "cli", "retrieval": "direct"})
        seen.add(url)
    if not jobs:
        raise Failure("NO_VERIFIED_RESULTS", "No usable CareerViet cards found; empty search or changed page, not verified zero jobs")
    return jobs


def schema_jobs(markup):
    doc = Document(markup)
    def walk(value):
        if isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            if types == "JobPosting" or isinstance(types, list) and "JobPosting" in types:
                yield value
            for child in value.values():
                if isinstance(child, (dict, list)):
                    yield from walk(child)
    for node in doc.root.all():
        if node.tag == "script" and node.attrs.get("type") == "application/ld+json":
            raw = "".join(c for c in node.children if isinstance(c, str))
            try:
                yield from walk(json.loads(raw))
            except json.JSONDecodeError:
                continue


def iso_date(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise Failure("BAD_RECORD", "Dates must be ISO date strings or null")
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError as exc:
        raise Failure("BAD_RECORD", f"Invalid date: {value!r}") from exc


def record(data, source, retrieval):
    if not isinstance(data, dict):
        raise Failure("BAD_RECORD", "Each posting must be an object")
    result = {}
    for key in ("title", "company", "description", "url"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise Failure("BAD_RECORD", f"Posting needs a non-empty {key}")
    result["url"] = source_url(data["url"], source, posting=True)
    for key in ("title", "company", "location"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise Failure("BAD_RECORD", f"{key} must be text or null")
        result[key] = clean(value)[0] or None
    result["description"], result["guard"] = clean(data["description"], "html" if data.get("description_html") else "text")
    if not all(result.get(key) for key in ("title", "company", "description")):
        raise Failure("BAD_RECORD", "Posting has no usable text after cleaning")
    result.update(id=result["url"], date=iso_date(data.get("date")),
                  deadline=iso_date(data.get("deadline")), portal=source,
                  source="cli" if retrieval == "direct" else "websearch",
                  retrieval=retrieval, fetched_at=datetime.now(timezone.utc).isoformat())
    if result["deadline"] and result["deadline"] < date.today().isoformat():
        result["availability"] = "expired_by_deadline"
    else:
        result["availability"] = "unverified"
    return result


def schema_record(data, source, url, retrieval):
    if not isinstance(data.get("description"), str) or not data["description"].strip():
        raise Failure("BAD_RECORD", "JobPosting needs a description")
    org = data.get("hiringOrganization", {})
    locations = data.get("jobLocation", [])
    if isinstance(locations, dict):
        locations = [locations]
    parts = []
    for loc in locations:
        address = loc.get("address", {}) if isinstance(loc, dict) else {}
        if isinstance(address, dict):
            parts.extend(str(address[k]) for k in ("addressLocality", "addressRegion", "addressCountry")
                         if isinstance(address.get(k), str))
    result = record({"title": data.get("title"), "company": org.get("name") if isinstance(org, dict) else None,
                   "description": "\n".join(str(data[k]) for k in ("description", "responsibilities", "skills", "qualifications", "jobBenefits") if data.get(k)), "description_html": True,
                   "url": url, "location": ", ".join(dict.fromkeys(parts)) or None,
                   "date": data.get("datePosted"), "deadline": data.get("validThrough")}, source, retrieval)
    salary = data.get("baseSalary")
    result["salary"] = None
    if isinstance(salary, dict) and isinstance(salary.get("value"), dict):
        amount = salary["value"]
        result["salary"] = {
            "currency": clean(salary.get("currency"))[0] or None,
            "min": amount.get("minValue") if isinstance(amount.get("minValue"), (int, float)) else None,
            "max": amount.get("maxValue") if isinstance(amount.get("maxValue"), (int, float)) else None,
            "period": clean(amount.get("unitText"))[0] or None,
        }
    return result


def fold(value):
    value = unicodedata.normalize("NFKD", value.lower().replace("đ", "d"))
    return " ".join("".join(c for c in value if not unicodedata.combining(c)).split())


CATEGORIES = {"it": ("it-phan-mem", 8), "marketing": ("marketing", 12)}


def next_api(markup):
    doc = Document(markup)
    nodes = [n for n in doc.root.all() if n.tag == "script" and n.attrs.get("id") == "__NEXT_DATA__"]
    try:
        raw = "".join(c for c in nodes[0].children if isinstance(c, str))
        return json.loads(raw)["props"]["initialState"]["api"]
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise Failure("PARSE_CHANGED", "Missing Vieclam24h public initial state") from exc


def vieclam_search(markup, category):
    try:
        data = next_api(markup)["getJobList"]["data"]
        items = data["items"]
        if not isinstance(items, list) or not isinstance(data["total_items"], int):
            raise TypeError()
        links = {}
        for node in Document(markup).root.all(visible=True):
            href = node.attrs.get("href", "") if node.tag == "a" else ""
            match = re.search(r"id(\d+)\.html", href)
            if match:
                links[match[1]] = source_url(urljoin("https://vieclam24h.vn", href), "vieclam24h", posting=True).split("?")[0]
        jobs = []
        for row in items:
            if CATEGORIES[category][1] not in row["occupation_ids_main"]:
                raise Failure("FILTER_CHANGED", "Vieclam24h returned an unexpected occupation")
            title = clean(row["title"])[0]
            company = clean(row["employer_info"]["name"])[0]
            url = links[str(row["id"])]
            if not title or not company:
                raise TypeError()
            jobs.append({"id": str(row["id"]), "title": title, "company": company, "url": url,
                         "location": None, "date": None, "portal": "vieclam24h", "source": "cli", "retrieval": "direct"})
        if not jobs and data["total_items"]:
            raise Failure("PARSE_CHANGED", "Nonempty Vieclam24h search has no usable jobs")
        return jobs
    except (KeyError, TypeError, ValueError) as exc:
        raise Failure("PARSE_CHANGED", "Invalid Vieclam24h listing data or posting links") from exc


def full_detail(url, source, cache):
    markup = fetch(url, source, policy_cache=cache)
    schemas = list(schema_jobs(markup))
    if len(schemas) != 1:
        raise Failure("PARSE_CHANGED", "Expected one complete JobPosting")
    canonical = schemas[0].get("url")
    if canonical and urlsplit(source_url(canonical, source, posting=True)).path != urlsplit(url).path:
        raise Failure("POSTING_MISMATCH", "Detail schema points to a different posting")
    item = schema_record(schemas[0], source, url, "direct")
    if source == "vieclam24h":
        try:
            row = next_api(markup)["jobDetailHiddenContact"]["data"]
            if not re.search(r"id" + re.escape(str(row["id"])) + r"\.html$", urlsplit(url).path):
                raise Failure("POSTING_MISMATCH", "Vieclam24h detail ID differs from the requested posting")
            sections = [row.get(key) for key in ("job_requirement_html", "other_requirement_html")]
            extra, verdict = clean("\n".join(s for s in sections if isinstance(s, str)), "html")
            if not extra:
                raise Failure("INCOMPLETE_DESCRIPTION", "Vieclam24h requirements are missing")
            item["description"] += "\n\nRequirements:\n" + extra
            if verdict != "posting_guard: clean":
                item["guard"] += "; requirements: " + verdict
        except (KeyError, TypeError) as exc:
            raise Failure("PARSE_CHANGED", "Missing Vieclam24h requirements") from exc
    if len(item["description"]) < 300:
        raise Failure("INCOMPLETE_DESCRIPTION", "Description too short for verified collection")
    return item


def collect_source(source, query, *, limit=5, location=None, category=None, pages=1, scan_limit=10, seen=None, tracker=None):
    require_enabled(source)
    if source not in ("careerviet", "vieclam24h"):
        raise Failure("UNSUPPORTED", "No full-description connector for this source")
    if source == "vieclam24h" and category not in CATEGORIES:
        raise Failure("CATEGORY_REQUIRED", "Vieclam24h requires --category it or marketing; keyword filtering is local")
    cache, leads, errors, visited = {}, [], [], []
    for page in range(1, (pages if source == "vieclam24h" else 1) + 1):
        if source == "careerviet":
            url = f"https://careerviet.vn/viec-lam/{quote(query.replace(' ', '-'), safe='')}-k-vi.html"
        else:
            slug, code = CATEGORIES[category]
            url = f"https://vieclam24h.vn/viec-lam-{slug}-o{code}.html?page={page}"
        markup = fetch(url, source, policy_cache=cache)
        found = career_search(markup) if source == "careerviet" else vieclam_search(markup, category)
        visited.append(page)
        leads.extend(found)
        if not found:
            break
    leads = list({j["url"]: j for j in leads}.values())
    parsed_count = len(leads)
    from scrape_state import select
    leads, prefetch_dedup = select(leads, seen or {}, tracker or set())
    collected, attempted, filtered = [], 0, 0
    for lead in leads[:scan_limit]:
        if len(collected) >= limit:
            break
        # CareerViet cards have locations; Vieclam24h locations need detail data.
        if source == "careerviet" and location and fold(location) not in fold(lead.get("location") or ""):
            filtered += 1
            continue
        attempted += 1
        try:
            item = full_detail(lead["url"], source, cache)
            if item["availability"] == "expired_by_deadline":
                filtered += 1
                continue
            if source == "vieclam24h" and not all(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", fold(item["title"] + " " + item["description"])) for term in fold(query).split()):
                filtered += 1
                continue
            if location and fold(location) not in fold(item.get("location") or ""):
                filtered += 1
                continue
            collected.append(item)
        except (Failure, OSError, ValueError, subprocess.SubprocessError) as exc:
            code = getattr(exc, "code", "IO_ERROR")
            errors.append({"url": lead["url"], "code": code, "error": str(exc)})
            if code in ("ACCESS_POLICY", "BLOCKED", "RATE_LIMITED", "TRANSPORT", "IO_ERROR"):
                break
    return collected, {"source": source, "query": query, "attempted": attempted, "errors": errors,
                       "complete": not errors, "pages": visited, "filtered": filtered,
                       "parsed_count": parsed_count, "scan_limit": scan_limit, "prefetch_deduplication": prefetch_dedup,
                       "coverage": "Bounded search sample; CareerViet first page, Vieclam24h category pages with local keyword/location filtering"}


def joy_references(query, limit=5):
    # Explicit reference-only retrieval; no descriptions enter the ranking pool.
    url = "https://api.joywork.vn/api/jobs?limit=" + str(limit) + "&page=1&q=" + quote(query, safe="")
    try:
        data = json.loads(fetch(url, "joywork", policy_cache={}))["data"]
        rows = data["jobs"]
        if not isinstance(rows, list) or not isinstance(data["pagination"]["total"], int):
            raise TypeError()
        refs = []
        for row in rows:
            slug, ident = row["slug"], row["id"]
            if not isinstance(slug, str) or not isinstance(ident, str) or not re.fullmatch(r"[a-z0-9()_-]+", slug) or not re.fullmatch(r"[a-z0-9]+", ident):
                raise TypeError()
            refs.append({"title": clean(row["title"])[0], "company": clean(row["company"]["name"])[0],
                         "url": source_url(f"https://joywork.vn/jobs/{slug}--{ident}", "joywork", posting=True),
                         "portal": "joywork", "usage": "reference_only", "rankable": False})
        return refs
    except (KeyError, TypeError, ValueError) as exc:
        raise Failure("PARSE_CHANGED", "Invalid JoyWork public search response") from exc


def emit(results, **meta):
    print(json.dumps({"meta": {"count": len(results), **meta}, "results": results}, ensure_ascii=False, indent=2))


def positive(value):
    n = int(value)
    if not 1 <= n <= 100:
        raise argparse.ArgumentTypeError("must be between 1 and 100")
    return n


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("USAGE", message)


def main(argv=None):
    parser = Parser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    sub.add_parser("sources")
    health = sub.add_parser("health", help="Check enabled sources only; --source explicitly probes one source, including disabled ones")
    health.add_argument("--source", choices=PROBES, help="Explicitly probe this source, even if disabled; does not enable retrieval")
    search = sub.add_parser("search")
    search.add_argument("--source", choices=PROBES, required=True)
    search.add_argument("--query", "-q", required=True)
    search.add_argument("--location", "-l")
    search.add_argument("--limit", type=positive, default=20)
    collect = sub.add_parser("collect", help="Bounded CareerViet or Vieclam24h search plus full descriptions")
    collect.add_argument("--source", choices=("careerviet", "vieclam24h"), default="careerviet")
    collect.add_argument("--query", "-q", required=True)
    collect.add_argument("--location", "-l")
    collect.add_argument("--limit", type=positive, default=5)
    collect.add_argument("--category", choices=CATEGORIES)
    collect.add_argument("--pages", type=positive, default=1)
    collect.add_argument("--scan-limit", type=positive, default=10)
    refs = sub.add_parser("references", help="Explicit JoyWork reference links only; excluded from AI ranking")
    refs.add_argument("--query", "-q", required=True)
    refs.add_argument("--limit", type=positive, default=5)
    detail = sub.add_parser("detail")
    detail.add_argument("--source", choices=PROBES, required=True)
    detail.add_argument("--url", required=True)
    imp = sub.add_parser("import")
    imp.add_argument("--source", choices=PROBES, required=True)
    imp.add_argument("--input", type=Path, required=True)
    imp.add_argument("--html", action="store_true")
    imp.add_argument("--url")
    try:
        args = parser.parse_args(argv)
        if args.command == "sources":
            print(REGISTRY.read_text(encoding="utf-8"))
        elif args.command == "health":
            results = []
            selected = [args.source] if args.source else [source for source in SOURCES if enabled(source)]
            skipped = [] if args.source else [source for source in SOURCES if not enabled(source)]
            for source in selected:
                entry = {"source": source, "url": PROBES[source], "enabled": enabled(source),
                         "explicit_probe": args.source is not None,
                         "checked_at": datetime.now(timezone.utc).isoformat()}
                try:
                    markup = fetch(PROBES[source], source)
                    if source == "careerviet":
                        jobs = career_search(markup)
                        entry.update(status="search_parsed", count=len(jobs), detail_verified=False)
                    elif source == "vieclam24h":
                        jobs = vieclam_search(markup, "it")
                        entry.update(status="category_parsed", count=len(jobs), detail_verified=False)
                    else:
                        entry.update(status="accessible_unverified", detail_verified=False,
                                     note="Page reachable; query semantics and extraction not verified")
                except Failure as exc:
                    entry.update(status="unavailable", code=exc.code, error=str(exc))
                results.append(entry)
            emit(results, skipped_disabled=skipped,
                 coverage="Point-in-time access and search checks; not a reliability guarantee")
        elif args.command == "references":
            if not args.query.strip():
                raise Failure("USAGE", "Query must not be empty")
            emit(joy_references(args.query.strip(), args.limit), source="joywork", usage="reference_only", rankable=False)
        elif args.command == "collect":
            if not args.query.strip() or args.pages > 3:
                raise Failure("USAGE", "Provide a nonempty query and at most three pages")
            jobs, meta = collect_source(args.source, args.query.strip(), limit=args.limit,
                                        location=args.location, category=args.category,
                                        pages=args.pages, scan_limit=args.scan_limit)
            emit(jobs, **meta)
            return 0 if meta["complete"] else 1
        elif args.command == "search":
            require_enabled(args.source)
            query = args.query.strip()
            if not query:
                raise Failure("USAGE", "Query must not be empty")
            if args.source not in ("careerviet", "topcv", "indeed"):
                raise Failure("UNSUPPORTED", "This source has access probes only; no verified search adapter")
            if args.source == "careerviet":
                url = f"https://careerviet.vn/viec-lam/{quote(query.replace(' ', '-'), safe='')}-k-vi.html"
            elif args.source == "topcv":
                url = f"https://www.topcv.vn/tim-viec-lam-{quote(query.replace(' ', '-'), safe='')}"
            else:
                url = f"https://vn.indeed.com/jobs?q={quote(query, safe='')}&l=Vietnam"
            markup = fetch(url, args.source)
            if args.source != "careerviet":
                raise Failure("UNSUPPORTED", "Page accessible, but search parser is not verified; use import")
            jobs = career_search(markup)
            parsed_count = len(jobs)
            if args.location:
                jobs = [j for j in jobs if fold(args.location) in fold(j["location"] or "")]
            emit(jobs[:args.limit], source=args.source, query=query, page=1,
                 parsed_count=parsed_count, truncated=len(jobs) > args.limit,
                 coverage="First page only; further pages and total coverage unverified",
                 warnings=["Location filtering is client-side on the retrieved page; no personal fit scoring performed",
                           "Search card dates are unknown; use detail for authoritative dates"])
        elif args.command == "detail":
            require_enabled(args.source)
            url = source_url(args.url, args.source, posting=True)
            if args.source == "vieclam24h":
                emit([full_detail(url, args.source, {})])
                return 0
            schemas = list(schema_jobs(fetch(url, args.source)))
            if len(schemas) != 1:
                raise Failure("PARSE_CHANGED", "Expected exactly one JobPosting; import visible text instead")
            emit([schema_record(schemas[0], args.source, url, "direct")])
        else:
            raw = args.input.read_text(encoding="utf-8")
            if args.html:
                if not args.url:
                    raise Failure("USAGE", "HTML import needs --url")
                url = source_url(args.url, args.source, posting=True)
                schemas = list(schema_jobs(raw))
                if len(schemas) != 1:
                    raise Failure("BAD_RECORD", "Expected one JobPosting; import visible text as JSON instead")
                jobs = [schema_record(schemas[0], args.source, url, "imported_html")]
            else:
                data = json.loads(raw)
                rows = data.get("results") if isinstance(data, dict) else data
                if not isinstance(rows, list) or not rows:
                    raise Failure("BAD_RECORD", "Import needs a non-empty array or results array")
                jobs = [record(row, args.source, "imported_text") for row in rows]
            unique = {j["url"]: j for j in jobs}
            emit(list(unique.values()), duplicates_removed=len(jobs) - len(unique),
                 warnings=["Imported content; current availability and search coverage unverified"])
        return 0
    except (Failure, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc), "code": getattr(exc, "code", "IO_ERROR")}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
