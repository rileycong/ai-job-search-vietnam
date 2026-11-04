#!/usr/bin/env python3
"""Shared local runtime for coding agents: doctor, bootstrap, status, scratch, compile.

No model/provider SDK or API key. Commands return JSON; failures exit 1.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import venv

ROOT = Path(__file__).resolve().parent.parent


def runtime_env(root=ROOT):
    env = os.environ.copy()
    # Discover portable TeX installations without embedding a user's machine path.
    bins = [p for p in (root / '.local-tools/TinyTeX/bin').glob('*') if p.is_dir()]
    env['PATH'] = os.pathsep.join([*(str(p) for p in bins), env.get('PATH', '')])
    return env


def executable(name, env):
    path = shutil.which(name, path=env.get('PATH', ''))
    return {'available': bool(path), 'path': path}


def doctor(root=ROOT):
    env = runtime_env(root)
    tools = {name: executable(name, env) for name in
             ('curl', 'lualatex', 'xelatex', 'pdftotext', 'pdftoppm')}
    tools['python'] = {'available': sys.version_info >= (3, 10),
                       'path': sys.executable, 'version': sys.version.split()[0]}
    tools['pypdf'] = {'available': importlib.util.find_spec('pypdf') is not None}
    bbox = False
    if tools['pdftotext']['available']:
        p = subprocess.run([tools['pdftotext']['path'], '-h'], capture_output=True,
                           text=True, errors='replace', timeout=10)
        bbox = '-bbox' in p.stdout + p.stderr
    tools['pdftotext']['bbox_supported'] = bbox
    missing = []
    actions = {
        'python': 'Install Python 3.10+ and rerun with that interpreter.',
        'curl': 'Install curl and add it to PATH.',
        'lualatex': 'Install TeX Live/TinyTeX/MacTeX or MiKTeX; see SETUP.md.',
        'xelatex': 'Install TeX Live/TinyTeX/MacTeX or MiKTeX; see SETUP.md.',
        'pdftotext': 'Install Poppler with pdftotext supporting -bbox and add its bin directory to PATH.',
        'pdftoppm': 'Install Poppler with pdftoppm and add its bin directory to PATH.',
    }
    for name, action in actions.items():
        if not tools[name]['available'] or name == 'pdftotext' and not bbox:
            missing.append({'component': name, 'action': action})
    if not tools['pypdf']['available'] and not tools['pdftotext']['available']:
        missing.append({'component': 'pdf_text', 'action': 'Run job_runtime.py bootstrap, then use the printed virtual-environment Python.'})
    agent_bundle = [name for name, item in tools.items()
                    if item.get('path') and 'codex-runtimes' in str(Path(item['path']).resolve())]
    retrieval_ready = tools['python']['available'] and tools['curl']['available']
    pdf_ready = not any(x['component'] != 'curl' for x in missing)
    return {'tools': tools, 'retrieval_ready': retrieval_ready,
            'pdf_ready': pdf_ready, 'full_workflow_ready': retrieval_ready and pdf_ready, 'missing': missing,
            'agent_bundled_components': agent_bundle,
            'portability_note': 'Agent-bundled tools work here but are not an installation on another computer.' if agent_bundle else None,
            'network_checked': False,
            'visual_inspection': 'Rendering capability is checked; a human or image-capable agent must inspect the pages.'}


def status(root=ROOT):
    profile = root / 'CLAUDE.md'
    text = profile.read_text(encoding='utf-8') if profile.exists() else ''
    profile_part = text.split('## Repo Structure', 1)[0]
    needs_profile = not text or bool(re.search(r'\[YOUR_|\[First\]|\[Last\]', profile_part))
    state_path = root / 'job_scraper/seen_jobs.json'
    counts = {}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding='utf-8'))
        seen = state.get('seen', state)
        if not isinstance(seen, dict):
            raise ValueError('Invalid job history: expected an object')
        for entry in seen.values():
            if not isinstance(entry, dict):
                raise ValueError('Invalid job history entry')
            key = entry.get('status', 'unknown')
            counts[key] = counts.get(key, 0) + 1
    tracker = root / 'job_search_tracker.csv'
    tracker_counts = {}
    if tracker.exists():
        with tracker.open(encoding='utf-8-sig', newline='') as file:
            for row in csv.DictReader(file):
                key = (row.get('status') or 'unknown').strip().lower().replace(' ', '_')
                tracker_counts[key] = tracker_counts.get(key, 0) + 1
    next_step = ('setup_profile' if needs_profile else 'review_existing_drafts' if tracker_counts.get('drafted')
                 else 'rank_new_jobs' if counts.get('new') or counts.get('unreachable')
                 else 'review_shortlist' if counts.get('ranked') else 'search_jobs')
    return {'profile_needs_setup': needs_profile, 'job_counts': counts,
            'tracker_counts': tracker_counts, 'next_step': next_step,
            'note': 'Read the saved profile and relevant records before acting. This summary does not certify profile accuracy or PDF verification.'}


def bootstrap(root=ROOT):
    if sys.version_info < (3, 10):
        raise ValueError('Use Python 3.10+ to create the local environment')
    target = root / '.venv'
    python = target / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not target.exists():
        venv.EnvBuilder(with_pip=True).create(target)
    if not python.is_file():
        raise ValueError('Existing .venv has no usable Python; it was left unchanged')
    result = subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(root / 'requirements-runtime.txt')],
                            capture_output=True, text=True, errors='replace', timeout=180)
    if result.returncode:
        raise ValueError('Could not install local PDF requirements: ' + result.stderr[-1500:])
    return {'python': str(python), 'next': [str(python), str(root / 'tools/job_runtime.py'), 'doctor'],
            'note': 'Python PDF dependency installed locally. TeX and Poppler are separate system or portable dependencies; doctor reports what is missing.'}


def source_path(value, folder):
    p = (ROOT / value).resolve()
    if p.suffix != '.tex' or not p.is_file() or not p.is_relative_to((ROOT / folder).resolve()):
        raise ValueError(f'Expected an existing .tex file under {folder}/')
    return p


def compile_application(cv, cover):
    readiness = doctor()
    if not readiness['pdf_ready']:
        return {'success': False, 'readiness': readiness}
    env = runtime_env()
    paths = [('cv', source_path(cv, 'cv'), 'lualatex', 2),
             ('cover', source_path(cover, 'cover_letters'), 'xelatex', 1)]
    checks = []
    renders = ROOT / 'reports/previews' / paths[0][1].stem
    renders.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='job-search-tex-') as cache:
        env.update(TEXMFCACHE=cache, TEXMFVAR=cache)
        for kind, tex, engine, pages in paths:
            for _ in range(2):
                p = subprocess.run([readiness['tools'][engine]['path'], '-no-shell-escape', '-halt-on-error',
                                    '-interaction=nonstopmode', tex.name], cwd=tex.parent, env=env,
                                   capture_output=True, text=True, errors='replace', timeout=120)
                if p.returncode or 'Missing character:' in p.stdout + p.stderr:
                    return {'success': False, 'stage': 'compile', 'file': str(tex),
                            'log': str(tex.with_suffix('.log')), 'error': p.stdout[-3000:]}
            pdf = tex.with_suffix('.pdf')
            commands = [
                [sys.executable, str(ROOT / 'tools/verify_pdf.py'), str(pdf), '--pages', str(pages),
                 '--ascii-dates', '--dump-text', str(tex.with_suffix('.txt'))],
                [sys.executable, str(ROOT / 'tools/verify_layout.py'), str(pdf)],
                [readiness['tools']['pdftoppm']['path'], '-scale-to', '1400', '-png', str(pdf), str(renders / kind)],
            ]
            for command in commands:
                p = subprocess.run(command, env=env, capture_output=True, text=True, errors='replace', timeout=60)
                checks.append({'command': command, 'exit_code': p.returncode,
                               'stdout': p.stdout, 'stderr': p.stderr})
                if p.returncode:
                    return {'success': False, 'stage': 'verification', 'checks': checks}
    return {'success': True, 'checks': checks, 'renders': str(renders),
            'visual_review_required': True,
            'note': 'Mechanical checks passed. Inspect rendered pages and verify candidate claims, contacts and keyword coverage before calling the application reviewed.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('doctor', 'status', 'bootstrap', 'scratch'):
        sub.add_parser(name)
    compile_parser = sub.add_parser('compile')
    compile_parser.add_argument('--cv', required=True)
    compile_parser.add_argument('--cover', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'doctor':
            result = doctor()
        elif args.command == 'status':
            result = status()
        elif args.command == 'bootstrap':
            result = bootstrap()
        elif args.command == 'scratch':
            result = {'path': tempfile.mkdtemp(prefix='job-search-')}
        else:
            result = compile_application(args.cv, args.cover)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get('success') is False or args.command == 'doctor' and not result['full_workflow_ready'] else 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    raise SystemExit(main())
