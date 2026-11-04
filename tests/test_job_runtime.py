import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
import job_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def test_clean_machine_reports_actionable_missing_components(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(runtime.shutil, 'which', return_value=None), patch.object(runtime.importlib.util, 'find_spec', return_value=None):
            result = runtime.doctor(Path(tmp))
        self.assertFalse(result['retrieval_ready'])
        self.assertFalse(result['pdf_ready'])
        self.assertFalse(result['network_checked'])
        self.assertIn('xelatex', {x['component'] for x in result['missing']})
        self.assertTrue(all(x['action'] for x in result['missing']))

    def test_retrieval_can_be_ready_without_pdf_tools(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(runtime.shutil, 'which', side_effect=lambda n, **kw: '/tools/curl' if n == 'curl' else None):
            result = runtime.doctor(Path(tmp))
        self.assertTrue(result['retrieval_ready'])
        self.assertFalse(result['pdf_ready'])

    def test_xpdf_without_bbox_is_not_layout_ready(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(runtime.shutil, 'which', side_effect=lambda n, **kw: '/tools/' + n), patch.object(runtime.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'xpdf help', '')):
            result = runtime.doctor(Path(tmp))
        self.assertFalse(result['pdf_ready'])
        self.assertIn('pdftotext', {x['component'] for x in result['missing']})

    def test_status_empty_workspace_requires_setup_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = runtime.status(root)
            self.assertEqual(result['next_step'], 'setup_profile')
            self.assertEqual(list(root.iterdir()), [])

    def test_status_resumes_saved_draft_without_mutating_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'CLAUDE.md').write_text('# Fictional profile\nDemo candidate\n## Repo Structure\n')
            (root / 'job_scraper').mkdir()
            state = root / 'job_scraper/seen_jobs.json'
            state.write_text(json.dumps({'seen': {'one': {'status': 'ranked'}, 'two': {'status': 'new'}}}))
            tracker = root / 'job_search_tracker.csv'
            tracker.write_text('company,status\nExample,drafted\n')
            before = (state.read_bytes(), tracker.read_bytes())
            result = runtime.status(root)
            self.assertEqual(result['next_step'], 'review_existing_drafts')
            self.assertEqual(result['tracker_counts'], {'drafted': 1})
            self.assertEqual(result['job_counts'], {'ranked': 1, 'new': 1})
            self.assertEqual((state.read_bytes(), tracker.read_bytes()), before)

    def test_malformed_history_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'job_scraper').mkdir()
            (root / 'job_scraper/seen_jobs.json').write_text('{"seen": []}')
            with self.assertRaises(ValueError):
                runtime.status(root)

    def test_compile_rejects_files_outside_document_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'anything.tex'
            source.write_text('test')
            with self.assertRaises(ValueError):
                runtime.source_path(str(source), 'cv')

    def test_compile_missing_glyphs_is_failure_even_when_engine_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for folder in ['cv', 'cover_letters']:
                (root / folder).mkdir()
                (root / folder / 'demo.tex').write_text('test')
            ready = {'pdf_ready': True, 'tools': {k: {'path': '/tools/' + k} for k in ['lualatex', 'xelatex', 'pdftoppm']}}
            with patch.object(runtime, 'ROOT', root), patch.object(runtime, 'doctor', return_value=ready), patch.object(runtime.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'Missing character: unsupported glyph', '')):
                result = runtime.compile_application('cv/demo.tex', 'cover_letters/demo.tex')
            self.assertFalse(result['success'])
            self.assertEqual(result['stage'], 'compile')


if __name__ == '__main__':
    unittest.main()
