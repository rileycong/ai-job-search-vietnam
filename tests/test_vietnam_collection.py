"""Per-run policy reuse must still evaluate each requested path."""
import sys
from pathlib import Path
import unittest
import io
import json
import contextlib
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
import robots_check as r
import vietnam_jobs as v


class CollectionPolicyTests(unittest.TestCase):
    def test_valid_policy_reused_but_each_path_checked(self):
        policy = 'User-agent: *\nAllow: /\nDisallow: /private/\n'
        cache = {}
        with patch.object(r, '_fetch', side_effect=[(policy, 200), ('<html>bad CDN response</html>', 200)]) as fetch:
            self.assertEqual(r.gate('https://careerviet.vn/viec-lam/python-k-vi.html', policy_cache=cache)[0], 0)
            self.assertEqual(r.gate('https://careerviet.vn/vi/tim-viec-lam/example.html', policy_cache=cache)[0], 0)
            self.assertEqual(r.gate('https://careerviet.vn/private/example', policy_cache=cache)[0], 1)
        self.assertEqual(fetch.call_count, 1)

    def test_unreadable_policy_not_cached(self):
        cache = {}
        with patch.object(r, '_fetch', return_value=('<html>not robots</html>', 200)):
            self.assertEqual(r.gate('https://careerviet.vn/a', policy_cache=cache)[0], 1)
        self.assertEqual(cache, {})

    def test_expired_policy_is_not_reused(self):
        cache = {'https://careerviet.vn': (0, 'User-agent: *\nAllow: /')}
        with patch.object(r.time, 'monotonic', return_value=301), patch.object(r, '_fetch', return_value=('User-agent: *\nDisallow: /', 200)):
            self.assertEqual(r.gate('https://careerviet.vn/a', policy_cache=cache)[0], 1)

    def test_collect_fetches_full_records_and_reports_partial_failures(self):
        fixture = Path(__file__).parent / 'fixtures/vietnam_jobs'
        search = (fixture / 'careerviet-search.html').read_text()
        detail = (fixture / 'careerviet-detail.html').read_text()
        # Full-description threshold is independent of search-card snippets.
        detail = detail.replace('LLM-pow', 'LLM-powered services. ' * 30)
        calls = []
        def fetch(url, source, **kwargs):
            calls.append(kwargs.get('policy_cache'))
            if len(calls) == 1:
                return search
            if len(calls) == 2:
                return detail
            raise v.Failure('BLOCKED', 'HTTP 403')
        out = io.StringIO()
        with patch.object(v, 'fetch', side_effect=fetch), contextlib.redirect_stdout(out):
            rc = v.main(['collect', '--query', 'python', '--limit', '2'])
        data = json.loads(out.getvalue())
        self.assertEqual(rc, 1)
        self.assertEqual(data['meta']['count'], 1)
        self.assertFalse(data['meta']['complete'])
        self.assertEqual(data['meta']['errors'][0]['code'], 'BLOCKED')
        self.assertTrue(all(c is calls[0] for c in calls))
        self.assertGreater(len(data['results'][0]['description']), 300)


if __name__ == '__main__':
    unittest.main()
