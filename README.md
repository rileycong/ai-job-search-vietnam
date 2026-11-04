# AI Job Search Vietnam

Find Vietnamese jobs, rank them against your CV and preferences, and generate
CV and cover-letter PDFs with Gemini CLI or Codex. Applications are submitted manually.

## Get started

You need Python 3.10+, curl, and Gemini CLI or Codex.
For installation on macOS, follow the [step-by-step guide](START_HERE_MAC.txt).

Keep your working copy private: setup saves personal data in tracked profile
files. Do not push your CV or profile to a public repository. For a private
remote, see [SETUP.md, section 8](SETUP.md#8-pulling-upstream-updates-into-your-fork).

```bash
git clone https://github.com/rileycong/ai-job-search-vietnam.git
cd ai-job-search-vietnam
python3 tools/job_runtime.py bootstrap
.venv/bin/python tools/job_runtime.py doctor
```

PDF generation also needs TeX and Poppler. The readiness check lists missing
tools; follow [SETUP.md](SETUP.md) to install them.

## Use with your agent

Open this folder in Gemini CLI (`gemini`) or Codex. Use these prompts in order:

1. **Add your profile.** Copy your CV into `documents/cv/`, then ask:
   > Read AGENTS.md and VIETNAM_WORKFLOW.md. Set up my profile from
   > documents/cv/my-cv.pdf. Ask for my preferred roles, locations, salary,
   > working languages and deal-breakers. Confirm the facts before saving.

2. **Find jobs.**
   > Search the enabled Vietnamese boards using my saved preferences. Combine
   > full descriptions, remove duplicates, rank by fit, and show source links and gaps.
   > Report unavailable sources and incomplete results.

3. **Create an application.**
   > Prepare a tailored CV and cover letter for [chosen job URL] using my
   > confirmed facts. Generate both PDFs and run the document checks.
   > Show the file paths and any checks needing my review. Keep it drafted.

Review the PDFs in `cv/` and `cover_letters/` before applying yourself.
To resume later, ask your agent to read the saved status and continue where you left off.

## Job sources

The shared search combines full descriptions, removes duplicate jobs, and
keeps results from working sources when another source fails.

| Source | Available use |
|---|---|
| CareerViet | Keyword search and full descriptions; first results page |
| Vieclam24h | IT and marketing categories; full descriptions with local keyword filtering |
| JoyWork | Optional reference links for manual review; excluded from AI ranking |
| TopCV, Indeed Vietnam, JobsGO | Disabled after blocked access checks; manual import available |
| VietnamWorks | Unverified; disabled by default; manual import available |

Searches have page and scan limits, and website access can be intermittent.
Blocked or unverified boards are probed only when explicitly requested.
See [source details](job_scraper/research-vietnam-job-sources.md).

## Useful commands

Run these from the project folder. Replace the example keywords and category
with your preferences; supported Vieclam24h categories are `it` and `marketing`.
For other fields, select CareerViet alone with `--sources careerviet`.

```bash
# Combined full-description search
.venv/bin/python tools/vietnam_search.py --query marketing --category marketing --limit-per-source 5

# Add separate JoyWork reference links
.venv/bin/python tools/vietnam_search.py --query marketing --category marketing --references

# Evaluate a complete posting you saved as JSON
.venv/bin/python tools/vietnam_jobs.py import --source topcv --input /path/to/postings.json

# Check saved progress before resuming
.venv/bin/python tools/job_runtime.py status
```

The search returns unranked jobs; your agent evaluates them against your profile.
A partial run reports the failed sources and keeps usable results. JoyWork
references stay separate from jobs eligible for ranking or application PDFs.

For the import format, PDF commands and full workflow, see
[VIETNAM_WORKFLOW.md](VIETNAM_WORKFLOW.md). Gemini CLI and Codex share the same
tools and instructions; only Codex has been tested end to end.

## Credit

Adapted from [ai-job-search-dev/ai-job-search](https://github.com/ai-job-search-dev/ai-job-search)
by Mads Lorentzen. This version adds shared Gemini CLI/Codex instructions,
Vietnamese source retrieval and import, and portable PDF tools.
[MIT license](LICENSE); bundled fonts retain their separate licenses.
