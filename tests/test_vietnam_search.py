"""Shared-flow checks: source failures, duplicates, bounded queries and rank isolation."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
import vietnam_jobs as v
import vietnam_search as shared

FIXTURES = Path(__file__).parent / 'fixtures/vietnam_jobs'


def posting(portal='careerviet', title='Engineer', company='Example'):
    url = ('https://careerviet.vn/vi/tim-viec-lam/engineer.ABC123.html' if portal == 'careerviet'
           else 'https://vieclam24h.vn/it/engineer-id123.html')
    return {'title': title, 'company': company, 'url': url, 'portal': portal,
            'description': 'Verified requirements ' * 25, 'source': 'cli', 'retrieval': 'direct',
            'date': None, 'deadline': None, 'availability': 'unverified'}


class SharedSearchTests(unittest.TestCase):
    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = shared.main(['--query', 'IT', '--category', 'it', '--state', tmp+'/seen.json',
                              '--tracker', tmp+'/tracker.csv', *args])
        return rc, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def test_one_source_failure_retains_other_jobs_and_reports_partial(self):
        def collect(source, *a, **kw):
            if source == 'careerviet':
                raise v.Failure('ACCESS_POLICY', 'unconfirmed')
            return [posting('vieclam24h')], {'complete': True, 'source': source}
        with patch.object(v, 'collect_source', side_effect=collect):
            rc, data, err = self.call()
        self.assertEqual((rc, err), (1, ''))
        self.assertEqual(len(data['results']), 1)
        self.assertFalse(data['meta']['complete'])
        self.assertEqual(data['meta']['sources'][0]['code'], 'ACCESS_POLICY')

    def test_cross_board_duplicate_keeps_both_links_and_history_is_read_only(self):
        a, b = posting(), posting('vieclam24h')
        jobs, counts = shared.combine([[a], [b]])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(jobs[0]['source_urls']), 2)
        self.assertEqual(counts['cross_source_duplicates'], 1)
        jobs, counts = shared.combine([[a], [b]], seen={'previous': a})
        self.assertEqual(jobs, [])
        self.assertEqual(counts['already_seen'], 1)

    def test_references_never_enter_ranking_pool_or_call_disabled_boards(self):
        def collect(source, *a, **kw):
            return [posting(source, title=source)], {'complete': True, 'source': source}
        ref = {'title': 'Reference', 'url': 'https://joywork.vn/jobs/example', 'rankable': False}
        with patch.object(v, 'collect_source', side_effect=collect) as collect, patch.object(v, 'joy_references', return_value=[ref]):
            rc, data, err = self.call('--references')
        self.assertEqual((rc, err), (0, ''))
        self.assertEqual([c.args[0] for c in collect.call_args_list], ['careerviet', 'vieclam24h'])
        self.assertEqual(len(data['results']), 2)
        self.assertEqual(data['references'], [ref])
        self.assertFalse(data['meta']['ranked'])

    def test_reference_source_is_not_contacted_by_default(self):
        with patch.object(v, 'collect_source', return_value=([], {'complete': True})), patch.object(v, 'joy_references') as refs:
            rc, data, err = self.call()
        refs.assert_not_called()
        self.assertEqual(rc, 0)

    def test_all_failed_is_not_success_or_verified_zero(self):
        with patch.object(v, 'collect_source', side_effect=v.Failure('BLOCKED', '403')):
            rc, data, err = self.call()
        self.assertEqual(rc, 1)
        self.assertFalse(data['meta']['complete'])
        self.assertTrue(all(s['status'] == 'failed' for s in data['meta']['sources']))

    def test_timeout_in_one_connector_preserves_the_other(self):
        import subprocess
        def collect(source, *a, **kw):
            if source == "careerviet":
                raise subprocess.TimeoutExpired("curl", 25)
            return [posting("vieclam24h")], {"complete": True, "source": source}
        with patch.object(v, "collect_source", side_effect=collect):
            rc, data, err = self.call()
        self.assertEqual((rc, err), (1, ""))
        self.assertEqual(len(data["results"]), 1)

    def test_budget_and_blank_query_rejected_before_network(self):
        for args in (['--query', ' '], ['--pages', '4']):
            with patch.object(v, 'collect_source') as collect:
                rc, data, err = self.call(*args)
            self.assertEqual(rc, 1)
            collect.assert_not_called()


class VieclamConnectorTests(unittest.TestCase):
    def test_captured_listing_state_extracts_only_public_contract(self):
        jobs = v.vieclam_search((FIXTURES/'vieclam24h-search.html').read_text(), 'it')
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(j['portal'] == 'vieclam24h' for j in jobs))
        self.assertNotIn('assigned_staff', json.dumps(jobs))
        self.assertTrue(all('?' not in j['url'] for j in jobs))

    def test_category_mismatch_and_missing_data_are_failures(self):
        html = (FIXTURES/'vieclam24h-search.html').read_text()
        for markup in (html, '<html>loading</html>'):
            with self.assertRaises(v.Failure):
                v.vieclam_search(markup, 'marketing')

    def test_category_required_before_fetch(self):
        with patch.object(v, 'fetch') as fetch, self.assertRaises(v.Failure) as cm:
            v.collect_source('vieclam24h', 'python')
        self.assertEqual(cm.exception.code, 'CATEGORY_REQUIRED')
        fetch.assert_not_called()

    def test_requirements_and_benefits_survive_full_detail(self):
        html = (FIXTURES/'vieclam24h-detail.html').read_text()
        schema = list(v.schema_jobs(html))[0]
        with patch.object(v, 'fetch', return_value=html):
            job = v.full_detail(schema['url'], 'vieclam24h', {})
        self.assertIn('Requirements:', job['description'])
        self.assertGreater(len(job['description']), 300)
        self.assertNotIn('<p>', job['description'])

    def test_local_keywords_do_not_match_inside_other_words_and_scan_is_bounded(self):
        leads = [posting('vieclam24h', title='Writer') for _ in range(4)]
        for i, lead in enumerate(leads):
            lead['url'] = lead['url'].replace('123', str(i))
            lead['title'] += str(i)
        with patch.object(v, 'fetch', return_value='unused'), patch.object(v, 'vieclam_search', return_value=leads), patch.object(v, 'full_detail', return_value=posting('vieclam24h', title='Writer')) as detail:
            jobs, meta = v.collect_source('vieclam24h', 'IT', category='it', scan_limit=2)
        self.assertEqual(jobs, [])
        self.assertEqual(detail.call_count, 2)
        self.assertEqual(meta['filtered'], 2)

    def test_description_guard_warning_is_not_erased_by_clean_requirements(self):
        html = (FIXTURES/'vieclam24h-detail.html').read_text()
        schema = list(v.schema_jobs(html))[0]
        warned = {**posting('vieclam24h'), 'guard': 'posting_guard: warning'}
        with patch.object(v, 'fetch', return_value=html), patch.object(v, 'schema_record', return_value=warned):
            job = v.full_detail(schema['url'], 'vieclam24h', {})
        self.assertIn('warning', job['guard'])

    def test_detail_timeout_preserves_already_collected_records(self):
        import subprocess
        leads = [posting(title='First'), posting(title='Second')]
        leads[1]['url'] = leads[1]['url'].replace('ABC123', 'DEF456')
        with patch.object(v, 'fetch', return_value='unused'), patch.object(v, 'career_search', return_value=leads), patch.object(v, 'full_detail', side_effect=[posting(), subprocess.TimeoutExpired('curl', 25)]):
            jobs, meta = v.collect_source('careerviet', 'IT', limit=2)
        self.assertEqual(len(jobs), 1)
        self.assertFalse(meta['complete'])
        self.assertEqual(meta['errors'][0]['code'], 'IO_ERROR')

    def test_saved_jobs_are_skipped_before_expensive_detail_requests(self):
        lead = posting('vieclam24h')
        with patch.object(v, 'fetch', return_value='unused'), patch.object(v, 'vieclam_search', return_value=[lead]), patch.object(v, 'full_detail') as detail:
            jobs, meta = v.collect_source('vieclam24h', 'IT', category='it', seen={'old': lead})
        detail.assert_not_called()
        self.assertEqual(jobs, [])
        self.assertEqual(meta['prefetch_deduplication']['already_seen'], 1)
        self.assertEqual(meta['parsed_count'], 1)

    def test_joy_reference_mapping_excludes_full_text_and_accepts_observed_slug(self):
        payload = {"data": {"jobs": [{"id": "abc123", "slug": "marketing-(ha-noi)",
                    "title": "Marketing", "company": {"name": "Example"},
                    "tasks": "This full text must not enter the shared ranking pool"}],
                    "pagination": {"total": 1}}}
        with patch.object(v, 'fetch', return_value=json.dumps(payload)) as fetch:
            refs = v.joy_references('marketing', 2)
        self.assertEqual(len(refs), 1)
        self.assertFalse(refs[0]['rankable'])
        self.assertNotIn('tasks', refs[0])
        self.assertNotIn('description', refs[0])
        self.assertIn('q=marketing', fetch.call_args.args[0])
        with patch.object(v, 'fetch', return_value='{"data": {"jobs": []}}'), self.assertRaises(v.Failure):
            v.joy_references('marketing')

    def test_joy_api_allowlist_does_not_allow_other_routes_hosts_or_credentials(self):
        self.assertEqual(v.source_url('https://api.joywork.vn/api/jobs?q=it', 'joywork'), 'https://api.joywork.vn/api/jobs?q=it')
        for url in ('https://api.joywork.vn/api/users', 'https://evil.test/api/jobs', 'https://www.api.joywork.vn/api/jobs', 'https://user@api.joywork.vn/api/jobs'):
            with self.assertRaises(v.Failure):
                v.source_url(url, 'joywork')


if __name__ == '__main__':
    unittest.main()
