#!/usr/bin/env python3
"""Combine enabled Vietnamese connectors into one deduplicated full-description pool.

Retrieves data only; the host agent ranks it. History/tracker are read-only.
JoyWork reference links are opt-in and never enter results for ranking.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import subprocess

import scrape_state
from rank_state import STATE, TRACKER, load_state, tracker_pairs
import vietnam_jobs as v

CONNECTORS = ("careerviet", "vieclam24h")


def combine(batches, *, seen=None, tracker=None):
    pool, identities = [], {}
    for jobs in batches:
        for job in jobs:
            # Retain all source links when the same employer/title is syndicated.
            _, pair = scrape_state.identity(job)
            if pair in identities:
                existing = identities[pair]
                existing["source_urls"].append({"portal": job["portal"], "url": job["url"]})
                continue
            item = dict(job, source_urls=[{"portal": job["portal"], "url": job["url"]}])
            identities[pair] = item
            pool.append(item)
    selected, counts = scrape_state.select(pool, seen or {}, tracker or set())
    counts["cross_source_duplicates"] = sum(len(batch) for batch in batches) - len(pool)
    return selected, counts


def run(args):
    sources = list(dict.fromkeys(args.sources)) if args.sources else [s for s in CONNECTORS if v.enabled(s)]
    seen = load_state(args.state)[1] if args.state.exists() else {}
    tracker = tracker_pairs(args.tracker)
    reports, batches = [], []
    def collect(source):
        try:
            jobs, meta = v.collect_source(source, args.query, limit=args.limit_per_source,
                                          category=args.category, location=args.location,
                                          pages=args.pages, scan_limit=args.scan_limit, seen=seen, tracker=tracker)
            return jobs, {**meta, "count": len(jobs), "status": "success" if meta["complete"] else "partial"}
        except (v.Failure, OSError, ValueError, subprocess.SubprocessError) as exc:
            return [], {"source": source, "count": 0, "status": "failed",
                        "code": getattr(exc, "code", "IO_ERROR"), "error": str(exc)}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for jobs, report in pool.map(collect, sources):
            batches.append(jobs)
            reports.append(report)
    results, dedup = combine(batches, seen=seen, tracker=tracker)
    references = []
    if args.references:
        try:
            references = v.joy_references(args.query, args.limit_per_source)
            reports.append({"source": "joywork", "status": "reference_only", "count": len(references), "rankable": False})
        except (v.Failure, OSError, ValueError, subprocess.SubprocessError) as exc:
            reports.append({"source": "joywork", "status": "failed", "count": 0,
                            "code": getattr(exc, "code", "IO_ERROR"), "error": str(exc)})
    complete = bool(sources) and all(r["status"] in ("success", "reference_only") for r in reports)
    return {"meta": {"count": len(results), "complete": complete, "sources": reports,
                     "skipped_sources": [s for s in v.SOURCES if s not in sources and not (s == "joywork" and args.references)],
                     "deduplication": dedup, "ranked": False,
                     "coverage": "Bounded sample, not every job on each board. Rank results only; references are links for manual review."},
            "results": results, "references": references}


def main(argv=None):
    p = v.Parser(description=__doc__)
    p.add_argument("--query", "-q", required=True)
    p.add_argument("--sources", nargs="+", choices=CONNECTORS)
    p.add_argument("--category", choices=v.CATEGORIES, help="Required for Vieclam24h")
    p.add_argument("--location")
    p.add_argument("--limit-per-source", type=v.positive, default=5)
    p.add_argument("--scan-limit", type=v.positive, default=10, help="Maximum detail requests per board")
    p.add_argument("--pages", type=v.positive, default=1, help="Vieclam24h category pages, maximum 3")
    p.add_argument("--references", action="store_true", help="Also retrieve JoyWork reference links; excluded from ranking")
    p.add_argument("--state", type=Path, default=STATE)
    p.add_argument("--tracker", type=Path, default=TRACKER)
    try:
        args = p.parse_args(argv)
        args.query = args.query.strip()
        if not args.query or args.pages > 3:
            raise v.Failure("USAGE", "Provide a nonempty query and at most three pages")
        data = run(args)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if data["meta"]["complete"] else 1
    except (v.Failure, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"code": getattr(exc, "code", "IO_ERROR"), "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
