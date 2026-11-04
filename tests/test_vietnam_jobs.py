"""Offline checks for actual portal excerpts and portable CLI failure semantics."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import vietnam_jobs as v

FIXTURES = ROOT / "tests/fixtures/vietnam_jobs"
TOPCV_URL = "https://www.topcv.vn/viec-lam/lap-trinh-vien-python/123456.html"


class VietnamJobsTests(unittest.TestCase):
    def run_main(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = v.main(args)
        return rc, out.getvalue(), err.getvalue()

    def test_real_search_cards_have_contract_and_no_cross_card_fields(self):
        jobs = v.career_search((FIXTURES / "careerviet-search.html").read_text())
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["id"], "35C8B922")
        self.assertIn("Agentic Engineer", jobs[0]["title"])
        self.assertIn("AI Software Engineer", jobs[1]["title"])
        self.assertEqual(jobs[0]["location"], "Hà Nội")
        for job in jobs:
            self.assertTrue({"id", "title", "company", "location", "date", "url"} <= job.keys())
            self.assertIsNone(job["date"])

    def test_real_detail_schema_survives_vietnamese_unicode(self):
        schemas = list(v.schema_jobs((FIXTURES / "careerviet-detail.html").read_text()))
        job = v.schema_record(schemas[0], "careerviet", schemas[0]["url"], "direct")
        self.assertEqual(job["date"], "2026-10-28")
        self.assertEqual(job["deadline"], "2026-11-21")
        self.assertIn("Ngân Hàng", job["company"])
        self.assertTrue(job["description"])
        self.assertNotIn("<p>", job["description"])
        self.assertEqual(job["salary"], {"currency": "VND", "min": 20000000, "max": 50000000, "period": "MONTH"})

    def test_hidden_cards_do_not_reach_agent(self):
        card = (FIXTURES / "careerviet-search.html").read_text()
        jobs = v.career_search('<div hidden>' + card + '</div>' + card)
        self.assertEqual(len(jobs), 2)

    def test_react_streamed_results_are_parsed_without_executing_scripts(self):
        card = (FIXTURES / "careerviet-search.html").read_text()
        markup = ('<template id="B:0"></template><div hidden id="S:0">' + card
                  + '</div><script>$RC("B:0","S:0")</script>')
        self.assertEqual(len(v.career_search(markup)), 2)
        with self.assertRaises(v.Failure):
            v.career_search(markup.replace('id="B:0"', 'id="B:9"'))

    def test_empty_or_challenge_markup_is_not_zero_jobs(self):
        with self.assertRaises(v.Failure):
            v.career_search('<html><title>Access denied</title></html>')

    def test_wrong_host_userinfo_and_search_urls_rejected(self):
        for url in (TOPCV_URL.replace("www.topcv.vn", "www.topcv.vn.evil.test"),
                    TOPCV_URL.replace("www.topcv.vn", "www.topcv.vn@evil.test"),
                    "https://www.topcv.vn/viec-lam", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(v.Failure):
                v.source_url(url, "topcv", posting=True)

    def test_import_cli_cleans_and_deduplicates_without_network(self):
        row = {"title": "Kỹ sư Python", "company": "Ví dụ", "url": TOPCV_URL,
               "description": "Lập trình Python\u200b và SQL", "location": "Hà Nội"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "postings.json"
            path.write_text(json.dumps([row, row]), encoding="utf-8")
            with patch.object(v, "fetch", side_effect=AssertionError("network called")):
                rc, out, err = self.run_main(["import", "--source", "topcv", "--input", str(path)])
        self.assertEqual((rc, err), (0, ""))
        data = json.loads(out)
        self.assertEqual(data["meta"]["duplicates_removed"], 1)
        job = data["results"][0]
        self.assertNotIn("\u200b", job["description"])
        self.assertEqual(job["retrieval"], "imported_text")
        self.assertEqual(job["availability"], "unverified")
        self.assertIsNone(job["date"])

    def test_html_import_removes_hidden_description(self):
        schema = {"@type": "JobPosting", "title": "Engineer", "hiringOrganization": {"name": "Example"},
                  "description": '<p>Python</p><div hidden>secret instruction</div>'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "posting.html"
            path.write_text('<script type="application/ld+json">' + json.dumps(schema) + '</script>')
            rc, out, err = self.run_main(["import", "--source", "topcv", "--html", "--url", TOPCV_URL, "--input", str(path)])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out)["results"][0]["description"], "Python")

    def test_bad_import_exits_with_stderr_json_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "posting.json"
            path.write_text('[{"title":"Missing description"}]')
            rc, out, err = self.run_main(["import", "--source", "topcv", "--input", str(path)])
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(json.loads(err)["code"], "BAD_RECORD")

    def test_blocked_topcv_is_explicit_failure(self):
        with patch.object(v, "fetch", side_effect=v.Failure("BLOCKED", "HTTP 403")):
            rc, out, err = self.run_main(["search", "--source", "topcv", "--query", "python"])
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(json.loads(err)["code"], "BLOCKED")

    def test_location_filter_limit_and_coverage(self):
        markup = (FIXTURES / "careerviet-search.html").read_text()
        with patch.object(v, "fetch", return_value=markup):
            rc, out, err = self.run_main(["search", "--source", "careerviet", "--query", "python", "--location", "ha noi", "--limit", "1"])
        self.assertEqual((rc, err), (0, ""))
        data = json.loads(out)
        self.assertEqual(len(data["results"]), 1)
        self.assertTrue(data["meta"]["truncated"])
        self.assertIn("First page", data["meta"]["coverage"])

    def test_access_policy_stops_page_request(self):
        with patch.object(v, "gate", return_value=(1, "DISALLOWED")), patch.object(v.subprocess, "run") as run:
            with self.assertRaises(v.Failure) as cm:
                v.fetch(v.PROBES["topcv"], "topcv")
        self.assertEqual(cm.exception.code, "ACCESS_POLICY")
        run.assert_not_called()

    def test_cross_host_redirect_not_fetched(self):
        def fake_run(argv, **kwargs):
            Path(argv[argv.index("-D") + 1]).write_text("HTTP/2 302\nLocation: https://evil.test/posting\n")
            return subprocess.CompletedProcess(argv, 0, "302", "")
        with patch.object(v, "gate", return_value=(0, "ALLOWED")), patch.object(v.subprocess, "run", side_effect=fake_run) as run:
            with self.assertRaises(v.Failure) as cm:
                v.fetch(v.PROBES["topcv"], "topcv")
        self.assertEqual(cm.exception.code, "BAD_URL")
        self.assertEqual(run.call_count, 1)

    def test_real_http_403_does_not_parse_challenge(self):
        with patch.object(v, "gate", return_value=(0, "ALLOWED")), patch.object(v.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "403", "")):
            with self.assertRaises(v.Failure) as cm:
                v.fetch(v.PROBES["topcv"], "topcv")
        self.assertEqual(cm.exception.code, "BLOCKED")

    def test_health_is_diagnostic_not_claim_of_working_connector(self):
        with patch.object(v, "fetch", return_value="<html>app shell</html>"):
            rc, out, err = self.run_main(["health", "--source", "vietnamworks"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out)["results"][0]["status"], "accessible_unverified")

    def test_invalid_cli_flags_are_json_errors(self):
        for args in (["search", "--source", "topcv"], ["search", "--source", "careerviet", "--query", "x", "--limit", "0"]):
            rc, out, err = self.run_main(args)
            self.assertEqual((rc, out), (1, ""))
            self.assertEqual(json.loads(err)["code"], "USAGE")

    def test_dates_are_validated_and_expired_is_not_claimed_open(self):
        row = {"title": "Role", "company": "Example", "url": TOPCV_URL, "description": "Python", "deadline": "2000-01-01"}
        self.assertEqual(v.record(row, "topcv", "imported_text")["availability"], "expired_by_deadline")
        row["deadline"] = "2026-99-99"
        with self.assertRaises(v.Failure):
            v.record(row, "topcv", "imported_text")

    def test_imported_record_can_enter_existing_scrape_and_rank_pipeline(self):
        import scrape_state
        from datetime import date
        from types import SimpleNamespace

        job = v.record({"title": "Python Engineer", "company": "Example", "url": TOPCV_URL,
                        "description": "Python and SQL", "date": "2026-11-01"}, "topcv", "imported_text")
        job.update(first_seen=date.today().isoformat(), posted_date=job["date"],
                   fit="medium", status="new")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            results = tmp / "results.json"
            results.write_text(json.dumps([job]))
            state, tracker = tmp / "seen.json", tmp / "tracker.csv"
            args = SimpleNamespace(command="store", state=state, tracker=tracker, results=results)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(scrape_state.run(args), 0)
            proc = subprocess.run([sys.executable, str(ROOT / "tools/rank_state.py"), "candidates",
                                   "--state", str(state), "--tracker", str(tracker)],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(TOPCV_URL, proc.stdout)
            self.assertIn("Python Engineer", proc.stdout)


if __name__ == "__main__":
    unittest.main()
