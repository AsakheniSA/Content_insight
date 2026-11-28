# Tula Insights Layer

Layer 1 of Tula's content engine. It finds the questions South African and international
pet owners are actually asking, deduplicates them into insights, scores them, and
publishes a ranked daily feed that Irene and Gigi's content pipeline reads.

Everything is stored in one Google Sheet. Everything runs on GitHub Actions. The only
paid service is the Claude API.

**Contents:** [How it works](#how-it-works) · [The spreadsheet](#the-spreadsheet) ·
[Setup](#setup) · [Secrets](#secrets-github-and-vercel) · [Running it](#running-it) ·
[The web view](#the-web-view) · [Costs and caps](#costs-caps-and-reading-the-results) ·
[Configuration](#configuration) · [Adding a source or category](#adding-a-source-or-a-category) ·
[Known limitations](#known-limitations)

---

## How it works

Every day at 05:17 SAST a GitHub Actions workflow runs four stages, one after another:

```
                 ┌── web_questions  (Claude web search: forums, Q&A, vet and travel sites)
                 ├── youtube        (questions in comments on relevant videos)
  1. COLLECT  ───┼── news_rss       (pet and travel news feeds)
  (in parallel)  └── manual         (rows the team pastes into manual_submissions)
                          │
                          ▼  items tab, status "new"
  2. CLASSIFY    Claude Haiku: relevant? which category? what is the underlying question?
                          │
                          ▼  items tab, status "classified" or "irrelevant"
  3. SCORE       Merge repeat questions into insights (local embeddings, Claude judges
                 the close calls), then score each insight 0–1
                          │
                          ▼  insights tab
  4. PUBLISH     Pick today's top 6 (diversity rules, no repeats), write a content angle
                          │
                          ▼  feed tab  ──▶  web view (Vercel)  ──▶  /api/feed (JSON)
                                       ──▶  Irene and Gigi's content pipeline

  Around them:   plan and purge first · one combined report · an alert issue if anything failed
```

Every stage reads its input from the sheet and writes its output back, so a stage can be
re-run on its own and nothing depends on a runner's filesystem surviving between jobs.
One source failing never stops the others, and classify, score and publish carry on with
whatever arrived — there is a feed every day, even on a bad one.

---

## The spreadsheet

One spreadsheet, eight tabs. `scripts/setup_sheet.py` creates them.

| Tab | Holds | Who edits it |
|---|---|---|
| `items` | Every collected item and its classification | Pipeline only |
| `insights` | Deduplicated questions, with evidence and scores | Pipeline only |
| `feed` | The daily ranked output — **the contract with the content team** | Pipeline writes rows; reviewers edit four columns (below) |
| `manual_submissions` | Compliant input for Facebook, Instagram and anything else | Anyone on the team |
| `runs` | One row per run: counts, tokens, cost, errors | Pipeline only |
| `items_archive` | Items past the retention window | Pipeline only |
| `_embeddings` | Insight question vectors | Nobody — hidden |
| `_batches` | In-flight Claude batches | Nobody — hidden |

### The `feed` contract

Reviewers and the content pipeline may edit **only** these four columns:

`status` · `rejection_reason` · `post_ref` · `reviewed_by`

`status` is one of `served` (the pipeline wrote it), `used` (it became a post) or
`rejected` (it did not). Rejections feed back into scoring: insights close to a rejected
one are penalised for the next 30 days, so the same unwanted angle stops resurfacing.

The pipeline is prevented *in code* from writing those four columns — `SheetStore`
raises rather than overwriting a reviewer's decision. Everything else on the tab belongs
to the pipeline.

**Read only today's rows.** Each day gets up to 6 rows (3 posts, 2 spares each), ranked
1 to 6: at most 2 per category, 1 to 2 on general pet care, and nothing below a minimum
score — a thin day serves fewer rows, not weaker ones. A question is served once, and
again only when new evidence has arrived *and* 21 days have passed. Running publish
twice on the same day serves nothing new.

**`suggested_angle` is a direction, never a fact.** It says what a post could help an
owner do — "Help owners work out what to ask their airline before booking" — and is
deliberately free of numbers, rules, time frames, prices, airline policies and legal or
medical statements. An angle containing any digit is rejected automatically. Facts for
the post itself come from Tula's verified-claims ledger, with its sourcing rules, never
from the angle. If an angle could not be written, the row is still served with the
angle blank.

**Evidence links to YouTube comments are removed from feed rows after 30 days**
(YouTube's storage limit); other links stay.

**Reading it from code:** the same rows are available as JSON from the web view's
[`/api/feed`](#the-web-view) endpoint, without access to the sheet.

---

## Setup

You need Python 3.11 or newer.

### 1. Install

```bash
git clone https://github.com/AsakheniSA/Content_insight.git
```

```bash
cd Content_insight && python -m venv .venv && source .venv/bin/activate
```

On Windows the activate line is `.venv\Scripts\activate`.

```bash
pip install -e ".[dev]"
```

```bash
cp .env.example .env
```

### 2. Google service account and the spreadsheet

If you already have a service account from another Asakheni project you can reuse it —
it just needs access to this spreadsheet.

1. In the Google Cloud console, enable the **Google Sheets API** and the **Google Drive
   API** on the service account's project.
2. Create a Google Sheet named **Tula Insights** (any name works; the ID is what matters).
3. Click **Share** and add the service account's email — it ends in
   `iam.gserviceaccount.com` — as an **Editor**.
4. Copy the sheet ID out of its URL: the part between `/d/` and `/edit`. Put it in `.env`
   as `SHEET_ID`.
5. Base64-encode the service-account JSON key to a single line and put it in `.env` as
   `GOOGLE_SERVICE_ACCOUNT_JSON`:

```bash
base64 -w0 service_account.json
```

On macOS that is `base64 -i service_account.json`. On Windows PowerShell:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("service_account.json"))
```

Base64 rather than raw JSON because the private key contains newlines that `.env` files
mangle. Raw JSON is still accepted if you prefer it.

### 3. Anthropic API key

Create a key at `console.anthropic.com` and put it in `.env` as `ANTHROPIC_API_KEY`.
An organisation admin must also **enable the web search tool in the Claude Console** (in
the organisation's settings) — without it the `web_questions` source cannot run. Web
search is billed per search on top of tokens; see [Costs](#costs-caps-and-reading-the-results).

### 4. YouTube Data API key

In the same Google Cloud project: **APIs & Services → Library → YouTube Data API v3 →
Enable**, then **Credentials → Create credentials → API key**. Edit the key and set
**API restrictions → Restrict key → YouTube Data API v3**, so a leaked key cannot be
used for anything else. Add it as `YOUTUBE_API_KEY` (in `.env` locally, and as a
repository secret).

### 5. Create the tabs

The **Pipeline** workflow creates any missing tab itself on every run, so in Actions
there is nothing to do. Locally:

```bash
python scripts/setup_sheet.py --dry-run
```

```bash
python scripts/setup_sheet.py
```

Safe to run repeatedly. It creates what is missing and reports — but never rewrites —
headers that differ from the schema.

---

## Secrets: GitHub and Vercel

**GitHub** — repository → **Settings → Secrets and variables → Actions → New repository
secret**:

| Secret | What it is | Used by |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude API key, with web search enabled for the organisation | Collect, classify, score, publish |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The service-account key file, base64 on one line | Every job that touches the sheet; the Web smoke test |
| `SHEET_ID` | The spreadsheet ID. Can be a repository **variable** instead (Variables tab), since it is not secret | Same |
| `YOUTUBE_API_KEY` | YouTube Data API v3 key, restricted to that API | Collect (youtube) |

`GITHUB_TOKEN` is provided by GitHub automatically; the alert and keep-alive jobs use it
with only the permissions their workflow grants. CI (`ci.yml`) needs no secrets at all.

**Vercel** — project → **Settings → Environment Variables**, for Production and Preview:

| Variable | Required? | What it is |
|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Yes | The same base64 key as the GitHub secret |
| `SHEET_ID` | Yes | The spreadsheet ID |
| `VIEW_PASSWORD` | No | Set it to put the pages behind a shared password; unset means open |
| `FEED_API_TOKEN` | No | Set it to require a token on `/api/feed`; unset means open |

**Locally**, the same names go in `.env` (pipeline) and `web/.env.local` (web view). Both
files are git-ignored. Never paste a key into an issue, a commit or a chat.

**Rotating a key:** create the new key, update it in GitHub and Vercel (and redeploy on
Vercel), check the next run passes, then delete the old key.

---

## Running it

```bash
python -m tula_insights validate-config
```

```bash
python -m tula_insights setup-sheet
```

Collect, which runs real searches and spends real credit — start with `--dry-run`:

```bash
python -m tula_insights collect --dry-run --log-format plain
```

```bash
python -m tula_insights collect
```

Add `--source web_questions`, `--source youtube`, `--source news_rss` or
`--source manual` to run just one.


Classify, which reads every `new` item and fills in its category, underlying question
and pain point:

```bash
python -m tula_insights classify --dry-run --log-format plain
```

```bash
python -m tula_insights classify --sync
```

```bash
python -m tula_insights classify
```

`--dry-run` classifies a sample of 10 and writes nothing. `--sync` sends one request per
item and saves as it goes. With neither, items go as one Message Batch at half price:
the run waits up to 15 minutes for it, and anything unfinished is collected by the next
run.

Score, which merges repeat questions into insights and ranks them. It needs the
embedding model's runtime, which is a separate install because it is large:

```bash
pip install -e ".[embeddings]"
```

```bash
python scripts/calibrate_dedupe.py
```

```bash
python -m tula_insights score --dry-run --log-format plain
```

The first command downloads the model (about 67 MB) on first use.

Publish, which writes today's feed:

```bash
python -m tula_insights publish --dry-run --log-format plain
```

```bash
python -m tula_insights publish
```

Housekeeping commands, which the workflow also runs:

| Command | Does |
|---|---|
| `python -m tula_insights plan` | Lists the sources a run would collect from |
| `python -m tula_insights purge` | Applies YouTube's 30-day storage limit to the sheet |
| `python -m tula_insights budget` | Month-to-date and today's spend against every cap |
| `python -m tula_insights report` | Summarises a run from its `runs` row (in Actions, the current run) |

Every stage takes the same options:

| Option | Effect |
|---|---|
| `--source NAME` | Limit to one source from `config/sources.yaml` |
| `--category ID` | Limit to one category from `config/categories.yaml` |
| `--dry-run` | Process everything, write nothing to Sheets, print a summary |
| `--sync` | Use the standard Claude API instead of the Batch API |
| `--log-format plain` | Human-readable logs instead of JSON |

### In GitHub Actions: the Pipeline workflow

**Pipeline** (`.github/workflows/pipeline.yml`) runs **every day at 05:17 SAST** (03:17
UTC — off the hour, because scheduled runs near the top of the hour are often delayed).
GitHub can delay or even drop a scheduled run when it is busy. In the first days it
dropped one, fired another four hours late, and on 28 November fired none at all — so
the pipeline has **two kinds of trigger**:

1. **GitHub's own schedule:** 05:17 SAST, with backups at 07:47 and 10:17.
2. **A daily trigger from Vercel**, independent of GitHub's scheduler: the web app's
   `/api/cron/run-pipeline` is called once a day around 06:30 SAST — see
   [the daily trigger](#the-daily-trigger-from-vercel).

Whichever fires first does the day's run. Every later one first checks the `runs` tab
and stops at once, spending nothing, if today's full run (collect, classify, score and
publish, status `success` or `partial`) is already done. A manual run from the Actions
tab always goes ahead, unless you tick `only_if_not_done`.
It runs seven jobs:

| Job | What it does |
|---|---|
| Plan and prepare | Validates config, plans the collect jobs, creates missing tabs, runs the YouTube 30-day purge, shows month-to-date spend against the caps |
| Collect | **One job per source, in parallel.** One failing never stops the others |
| Classify | Batch mode; runs if at least one source succeeded |
| Score | Checks the dedupe thresholds against the real model, then scores |
| Publish | Today's feed |
| Report | Merges every job's figures into the run's single `runs` row; one combined summary, ending with the budget and the sheet's size |
| Alert | Opens, updates or closes the `pipeline-failure` issue (below) |

Classify, score and publish carry on with whatever arrived, so there is a feed every day
even when a source fails. Only one run writes to the sheet at a time; a second waits.

**To run it by hand:** Actions → **Pipeline** → **Run workflow**. Options:

| Option | Default | Effect |
|---|---|---|
| `source` | all | Collect from one source only |
| `category` | all | Collect for one category only |
| `only_stage` | all | Re-run one stage (collect, classify, score or publish) |
| `sync_mode` | off | Classify one request per item now, instead of a half-price batch |
| `dry_run` | **on** | Do the work but write nothing — untick for a real run |
| `break_source` | none | Isolation test: give one source invalid credentials and watch the rest carry on |

Scheduled runs are always real runs. **To pause the schedule:** Actions → Pipeline →
`···` → **Disable workflow**. The keep-alive leaves a workflow you disabled by hand alone.

**When a run fails** — any job, including a single source — the Alert job opens a GitHub
issue titled *Pipeline failing* with the label `pipeline-failure`: the run link, which
jobs and steps failed, and their short error lines. If that issue is already open, it
adds a comment instead, so one outage is one issue. After the next **fully successful
run** it comments and closes the issue. A fully successful run means every job passed
in the scheduled run, or in a manual run with `dry_run` off and every other option at
its default; a successful dry run or a single-stage run proves too little to close it.
GitHub emails issue activity to people watching the repository — set Watch → Custom →
Issues to be told. The issue is public, so it quotes error lines only, never collected
text.

**Keep-alive.** GitHub switches off scheduled workflows in a public repository after 60
days without repository activity. **Keep-alive** (`.github/workflows/keepalive.yml`)
runs every Monday at 06:41 SAST. It re-enables the Pipeline workflow through GitHub's
API and, **only if `main` has had no commit for 45 days**, commits a one-line date to
`.github/keepalive`. With normal development that commit never happens; on a quiet
repository it is at most one small commit every 45 days. Settings:
`settings.yaml → keepalive`.

**If the schedule stops anyway** (no run on the Actions tab for a day or more): Actions
→ **Pipeline** → the banner says the workflow was disabled → **Enable workflow**. Do the
same for **Keep-alive**. Nothing is lost; the next scheduled run carries on.

**What each run leaves behind:** each job's section on the run page; a counts-only
`run-summary` artifact; each job's logs as artifacts (counts and IDs only — the
repository is public). All kept for 14 days.

**The other workflows:**

| Workflow | Runs | By hand |
|---|---|---|
| **CI** (`ci.yml`) | On every push and pull request: ruff, pytest, config validation, the secrets check | — |
| **Web** (`web.yml`) | On changes to `web/`: tests, build, then a counts-only read of the real sheet | Actions → Web → Run workflow |
| **Keep-alive** (`keepalive.yml`) | Mondays 06:41 SAST | Actions → Keep-alive → Run workflow (`dry_run` on by default) |

### Tests and linting

```bash
pytest -q
```

```bash
ruff check .
```

```bash
python scripts/check_secrets.py
```

No test touches the network. The Sheets store is tested against an in-memory fake.

---

## The web view

A small read-only site in `web/` (Next.js), for the team to read the feed without opening
the sheet, and a JSON endpoint for Irene and Gigi's pipeline.

| Page | Shows |
|---|---|
| **Today** (`/`) | The day's feed, best first: question, category, tags, angle, evidence links, status. Step back through earlier days. Warns if the pipeline hasn't run for 36 hours |
| **Insights** (`/insights`) | Every insight. Filter by category, tag and status; search the text; sort by score, recency or mentions |
| **Runs** (`/runs`) | The last 30 runs, failures and budget-cap hits highlighted, with this month's estimated Claude spend |

**The pages are open to anyone with the link** — they are meant to be shared inside the
organisation. That is a link, not a lock: anyone the link reaches can read the feed,
the insights and the run costs. Search engines are told not to index the site, which
keeps it out of search results but does not stop a forwarded link. To require a shared
password later, set `VIEW_PASSWORD` and redeploy — no code change.

The app reads the sheet on the server only, with the pipeline's service account and the
**read-only** Sheets scope, so it cannot change anything; the key never reaches the
browser. Each tab is cached for five minutes.

**The feed API**, for Irene and Gigi's pipeline:

```bash
curl -H "Authorization: Bearer $FEED_API_TOKEN" "https://<your-app>/api/feed?date=2026-11-26"
```

The `Authorization` header is only needed if `FEED_API_TOKEN` is set; without it the
endpoint is open like the pages. `date` defaults to today (SAST). The reply is JSON: `feed_date`, `count`, `fetched_at`,
`stale`, and `items` — each with the `feed` tab's columns (`rank`, `insight_id`,
`question`, `category`, `tags` and `evidence_urls` as lists, `suggested_angle`,
`total_score`, `served_at`, `status`, `rejection_reason`, `post_ref`). `reviewed_by` is
left out. With a token set, a wrong or missing one gets 401.

### Deploying on Vercel

1. In Vercel, import (or reconnect) this repository and set **Root Directory** to `web`.
   Vercel detects Next.js; leave the build settings at their defaults.
2. Under **Settings → Environment Variables**, add these for Production and Preview:

   | Variable | Value |
   |---|---|
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | Required. The same base64 key as the GitHub secret |
   | `SHEET_ID` | Required. The spreadsheet ID |
   | `VIEW_PASSWORD` | Optional. Leave unset for open pages; set it to require a shared password |
   | `FEED_API_TOKEN` | Optional. Leave unset for an open `/api/feed`; set it (e.g. `openssl rand -hex 32`) to require it |

3. Deploy, and share the URL inside the organisation.

### The daily trigger from Vercel

`web/vercel.json` asks Vercel to call `/api/cron/run-pipeline` once a day at 04:30 UTC
(06:30 SAST). On the free Hobby plan Vercel fires it some time within that hour. The
route asks GitHub to start the Pipeline workflow as a real run with `only_if_not_done`,
so it does nothing if the day's run already happened. To switch it on, add two
environment variables in Vercel (Production) and redeploy:

| Variable | Value |
|---|---|
| `CRON_SECRET` | A long random string, e.g. `openssl rand -hex 32`. Vercel sends it with every scheduled call, and the route refuses any call without it |
| `GITHUB_DISPATCH_TOKEN` | A **fine-grained** GitHub personal access token: GitHub → Settings → Developer settings → Fine-grained tokens → Generate. Repository access: **only** `Content_insight`. Permissions: **Actions — Read and write**, nothing else. Note its expiry date and replace it before then |

Without them the route returns 503 and only GitHub's schedule runs the pipeline. Check
it works in Vercel: project → Settings → Cron Jobs shows the job and lets you run it
once by hand; the Actions tab then shows a Pipeline run.

The app uses nothing Vercel-specific apart from that optional `vercel.json`, so any host that runs Node.js 20.9 or later works:
`npm ci && npm run build && npm start` in `web/`, with the same four variables. Vercel's
free Hobby plan is for non-commercial use under its terms — check them against how Tula
uses this, and move hosts if needed.

**Locally:** copy `web/.env.example` to `web/.env.local`, fill in the two required values, then:

```bash
cd web && npm ci && npm run dev
```

The **Web** workflow (`.github/workflows/web.yml`) tests and builds the app on every
change to `web/`, then reads the real sheet the way the app does and prints counts.

---

## Costs, caps and reading the results

**What a day costs.** Everything paid runs on Claude API credit. The real runs on
26 November 2026 cost about **US$0.20 each**, and most of that is web search fees
(US$10 per 1,000 searches, from `budget.yaml`), about 18 searches a run. At one run a
day that is roughly **US$6 a month** against a US$25 cap — an estimate from two days of
data, so watch the first month's figures. YouTube, Google Sheets, GitHub Actions (public
repository) and Vercel Hobby are free.

The figures are estimates, computed from token counts and the price table in
`budget.yaml` (`prices_checked_on` records when it was last checked against
Anthropic's pricing page). The Claude Console's billing page is the real bill.

**Caps.** Three of them apply to every search, and whichever bites first wins:
`sources.yaml` → `daily_request_cap` per source, `budget.yaml` → `daily_caps` and
`monthly_caps`, and `max_uses_per_request` per search request. The ledger reads what
previous runs recorded in the `runs` tab, so there is no separate counter to drift.

**To change a cap,** edit `config/budget.yaml` (`monthly_caps`, `daily_caps`,
`on_cap_exceeded`) or a source's `daily_request_cap` in `config/sources.yaml`, and push
to `main`. The next run uses the new values; CI rejects an invalid file before it lands.

Before each paid step the pipeline checks month-to-date spend against `budget.yaml`:

| Step | What stops at the cap |
|---|---|
| Collect | The web search and YouTube collectors stop searching; what they found so far is kept |
| Classify | No new classification this run; items wait as `new` |
| Score | The dedupe judge is switched off; questions in the judge band wait for a later run |
| Publish | The feed is still published, without suggested angles |

What happens next is `budget.yaml → on_cap_exceeded`:

- `skip` (the default, as the brief asks): the step is flagged in its job summary and
  in the `runs` row's `errors` (`budget: …`), and the run carries on and passes.
- `fail`: the step still saves what it has, then fails — which opens a
  `pipeline-failure` issue.

A source's own `daily_request_cap`, or YouTube refusing a request, is reported but never
counts as a budget breach.

**Checking spend.** Every run's summary ends with month-to-date spend against each cap,
and warns at 80%. To check at any time:

```bash
python -m tula_insights budget
```

**Reading a run's summary.** Open the run on the Actions tab; the **Report** job's
summary has the whole run:

| Line | Means |
|---|---|
| Collected by source | New items per source this run, after duplicates were dropped |
| classified (n irrelevant) | Items the classifier handled; irrelevant ones are kept but never scored |
| new insights / merged | Questions seen for the first time, and repeats folded into existing insights (with how many needed the Claude judge) |
| served today | Rows this run added to today's feed (0 if the day was already full) |
| tokens / est. cost | Claude usage and its estimated cost |
| Budget | Month-to-date and today's use against every cap; `!` lines are at 80% or more |
| Sheet size | Cells used against the archive threshold |
| Warnings and errors | Short notes: a source that failed, a budget skip (`budget: …`), a feed that could not be read |

Each collect job's own summary adds per-source detail (searches, feeds read, comments
kept).

**Reading the `runs` tab.** One row per pipeline run, written by the Report job:

| Column | Means |
|---|---|
| `run_id` | `gha-<run id>-<attempt>`; matches the Actions run |
| `trigger` | `schedule` or `dispatch` |
| `status` | `success`, `partial` (a source failed but the run carried on) or `failed` |
| `items_collected_by_source` | JSON: new items per source |
| `items_classified`, `items_irrelevant` | Classifier output |
| `new_insights`, `merged_into_existing`, `llm_judge_calls` | Dedupe output |
| `served_today` | Feed rows added |
| `tokens_in`, `tokens_out`, `tokens_cache_read` | Claude usage |
| `est_cost_usd` | Estimated cost — the budget caps are summed from this column |
| `api_calls_by_service` | JSON: web searches, Claude calls, YouTube searches and quota units |
| `actions_run_url`, `git_sha` | Where the run is, and which code it ran |
| `errors` | Up to 10 short notes, no collected text |

The web view's **Runs** page shows the same rows with problems highlighted.

---

## Configuration

Everything tunable lives in `config/`. No magic numbers in code.

| File | Controls |
|---|---|
| `categories.yaml` | The 10 categories, their descriptions and example questions. Feeds the classifier prompt. |
| `sources.yaml` | Which collectors run, their rate limits and daily caps |
| `keywords.yaml` | Keyword sets per category and region; seed phrases |
| `scoring.yaml` | Dedupe thresholds, score weights, publishing rules |
| `budget.yaml` | Monthly and daily spend caps, and the price table |
| `settings.yaml` | Sheet sizing, retention and platform storage limits, Sheets rate limits, the classifier (model, effort, batch timing), logging |

**Maintaining the news feeds.** The feeds live in `sources.yaml` under `news_rss →
settings → feeds`, each with an `id`, `url` and `region`. Every run reports each feed's
outcome by id — read, HTTP error, not a feed, or blocked by robots.txt — in the
collect job's summary. Switch a failing feed off with `enabled: false` rather than
deleting it, so the record of what was tried stays. Mark a feed `general: true` if it
covers more than pets; only its entries that mention a pet term are kept.

**Adding manual submissions.** Anyone on the team can add rows to the
`manual_submissions` tab: paste the question or quote into `question_or_quote`, the link
into `url`, and the site into `platform` (`reddit`, `facebook`, `instagram`, …). Leave
`processed` blank; the pipeline sets it to `TRUE` once the row is safely in `items`.
`submitted_by` and `notes` stay on that tab and are never copied into `items`, so it's
safe to write who found it and why. Don't paste the poster's username into the quote —
`u/` handles, `@handles`, emails and phone numbers are scrubbed anyway.

Config errors fail at startup with the file, the field and the reason, before a run
spends anything:

```
Config error:
budget.yaml is invalid:
  - limits: max_input_tokens_per_request must stay below long_context_threshold_tokens,
    where requests are billed at a multiple of standard rates
```

---

## Adding a source or a category

### A new source

1. **Write the collector** in `src/tula_insights/collectors/<name>.py`: a class with a
   `name` and a `collect(cfg, ctx)` method that returns `RawItem`s. Copy the shape of
   `news_rss.py` (fetching) or `web_search.py` (search). Scrub text with
   `utils.pii.scrub`, never log item text, and check the budget through `ctx.ledger`
   before anything paid.
2. **Register it** with one line in `src/tula_insights/collectors/registry.py`.
3. **Configure it** with an entry in `config/sources.yaml`: `name`, `enabled`,
   `regions`, `requests_per_minute`, `daily_request_cap`, `max_items_per_run`, and its
   own `settings`. If its platform limits how long data may be kept, add it to
   `settings.yaml → retention.api_data_max_days`.
4. **Test it** in `tests/test_<name>.py` with recorded responses — no network in tests.
5. **Add it to the workflow's choices:** the `source` and `break_source` lists at the
   top of `.github/workflows/pipeline.yml`.
6. **Try it:** `python -m tula_insights collect --source <name> --dry-run`, then a
   dispatch with `source=<name>`.

A source enabled in YAML without a registered module fails at planning time rather than
silently collecting nothing.

### A new category

1. **Add it to `config/categories.yaml`**: `id`, `name`, `description` and a few
   `example_questions`. The classifier's prompt and JSON schema are built from this
   file, so no code changes.
2. **Add at least one keyword set** for it in `config/keywords.yaml` — config
   validation refuses a category nothing can discover.
3. **Bump `version`** at the top of `categories.yaml`. Every classified row records
   `classify_extract:<prompt version>/categories:<categories version>`, so rows stay
   traceable to the rules that made them. (Changing `prompts/classify_extract.md`
   bumps that file's own `version` the same way.)
4. Run `python -m tula_insights validate-config`, then a classify dry run.

Existing items are not reclassified automatically.

---

## This repository is public

Which has consequences the code enforces rather than trusts people to remember:

- **Logs carry counts and IDs only** — never item text, questions or URLs. The JSON log
  formatter redacts URLs and email addresses as a backstop.
- **`.gitignore` covers** `.env`, service-account JSON, key material and local caches.
- **`scripts/check_secrets.py` runs in CI** on every push and pull request and fails the
  build on anything credential-shaped. Annotate a deliberate example with
  `# pragma: allowlist secret`.
- **CI needs no secrets at all**, so a pull request from a fork cannot exfiltrate one.
- Prompts and config are public. That is accepted.

If a real key is ever committed: rotate it first, then remove it from git history.
Deleting it in a later commit is not enough — the old commit still holds it.

---

## Build progress

| # | Stage | Status |
|---|---|---|
| 1 | Scaffold, config, Sheets schema | **Done** |
| 2 | Search collector + `SearchProvider` (`web_questions`; `youtube` and `manual` brought forward from stage 7) | **Done** |
| 3 | Classifier (Claude Haiku, batch + sync) | **Done** |
| 4 | Dedupe + scoring | **Done** |
| 5 | Publish feed | **Done** |
| 6 | Full `pipeline.yml` | **Done** |
| 7 | `news_rss` (the rest landed in stage 2) | **Done** |
| 8 | Alerting, keep-alive, budget guardrails | **Done** |
| 9 | Web view on Vercel | **Done** |
| 10 | Full README (brief section 12) | **Done** |

---

## How classification works

Each `new` item goes to Claude Haiku 5.5 with a system prompt built from
`prompts/classify_extract.md` and the categories in `categories.yaml`. The reply is
constrained to a JSON schema built from the same YAML, so the category is always one of
the listed ids. It is then checked in code for what the schema can't enforce:
confidence between 0 and 1, a question whenever an item is relevant, and tags only on
`general_pet_care`. A reply that fails is retried once with the reason stated, then the
item is marked `error`.

Item text is treated as data: the prompt tells the model to ignore any instructions
inside it, and items are wrapped so their text can't break out. Questions are rewritten
neutrally, in South African English, with personal details removed and scrubbed again
in code.

Every row records the `model` and a `prompt_version` such as
`classify_extract:1/categories:1`. **Change the prompt or the categories, bump the
version**, so rows stay traceable to the rules that made them.

Classification is a small part of the daily cost: batch mode is half price and the
system prompt (about 1,700 tokens) is cached. The run checks the monthly cap before it
starts.

## How dedupe and scoring work

Each classified question is turned into a vector by a small local model
(`BAAI/bge-small-en-v1.5`, run on the GitHub runner, no API cost) and compared with every
insight's question:

| Similarity | What happens |
|---|---|
| 0.93 or more | Merged into that insight — no API call |
| 0.80 to 0.93 | Claude Haiku judges whether it is really the same question (up to 150 a run; the rest wait for the next run) |
| below 0.80 | A new insight |

Before scoring, the workflow checks these thresholds against the real model on a fixed
set of question pairs with known answers (`tests/fixtures/dedupe_pairs.yaml`). If they
sort those pairs wrongly, the run stops rather than merging distinct questions.

Every insight is then scored from its items, each part from 0 to 1 and weighted in
`scoring.yaml`: **recurrence** (how many items), **cross-source** (how many sources),
**engagement** (ranked within each source, so YouTube is compared with YouTube),
**recency** (half-life 14 days) and **trend** (the last 7 days against the 21 before).
Insights close to anything a reviewer marked `rejected` in the feed lose 0.15.

Insight figures are recomputed from their items on every run, so the items stay the
source of truth. YouTube evidence links therefore leave an insight on their own when
the 30-day purge blanks those items.

## Known limitations

Recorded as they are designed in, not discovered later.

- **Reddit cannot be collected automatically.** Reddit blocks Anthropic's crawler, so
  the Claude web search tool rejects any search restricted to `reddit.com` (confirmed on
  25 November 2026). The Reddit Data API is out of scope (§13): commercial use needs a
  contract, and self-service access closed in November 2025. The `reddit_search` source
  is kept but disabled. **Reddit threads come in by hand** through `manual_submissions`.
- **YouTube data may be kept for 30 days at most.** YouTube's Developer Policies allow
  non-authorised API data — comment text, titles, view and like counts — to be stored
  for no more than 30 calendar days. Every collect run first **purges** YouTube rows
  older than that: their text and numbers are blanked and their status becomes
  `purged`. They are never archived. The limit lives in `settings.yaml →
  retention.api_data_max_days`. Insight evidence links to those comments drop out too, because
  insights are recomputed from their items on every run; feed rows lose them after
  30 days.
- **YouTube search is the scarce quota.** Since June 2026 `search.list` has its own
  bucket of 100 calls a day, separate from the 10,000-unit pool. The collector makes at
  most 50 a day. Quota resets at midnight Pacific time.
- **An insight's question can come from a YouTube comment.** The purge blanks the
  comment and the classifier's rewrite of it after 30 days, but the insight keeps its
  canonical question, which is that rewrite. Whether YouTube treats a paraphrase as API
  data is unresolved — worth a check before the feed goes live.
- **The news-feed list is only as good as its sources.** The first feeds came from web
  searches and could not be opened from the build environment, so each one was
  confirmed by a real run before being kept. No official government or veterinary feed
  (gov.za, NICD, AVMA) could be confirmed; adding one is a good next step once its
  address is checked on the publisher's own page. Feeds carry no engagement signal, so
  their items compete on recurrence, recency and trend.
- **Search engagement signals are weak.** The search tool returns no view, vote or
  comment counts, so web items are scored on result position and on how often a page
  recurs across a run's queries.
- **Search results carry no snippet.** A `web_search_result` returns only `url`, `title`,
  `page_age` and an opaque encrypted blob — there is no readable excerpt. Where an item
  has a snippet it came from a *citation*, which is text the model quoted from the page,
  and `engagement_json.snippet_source` records that. Items the model did not cite have
  no snippet, and the classifier works from the title alone.
- **`page_age` is not always a date.** It is a free-text string. It is parsed where the
  format is recognised and left blank otherwise, rather than guessed at — a wrong date
  would poison the recency score. The raw string is always kept in `engagement_json`.
- **No "People also ask" or autocomplete data.** The Claude web search tool is not
  Google and does not expose them. Those collectors were dropped from the design.
- **No Facebook or Instagram collectors.** The compliant path is the
  `manual_submissions` tab. Comments on Tula's own posts via Meta's official API are a
  possible later addition.
- **Google Sheets is the only store**, which assumes a single writer. The Actions
  concurrency group enforces that. Collectors append only their own source's rows, so
  parallel collection is safe. A spreadsheet has a cell limit (10 million is assumed;
  Google's figure has varied, so check it) counted over every tab's whole grid, empty
  cells included, and the Sheets API allows about 60 reads and 60 writes a minute — the
  pipeline stays under 50.
- **Vercel's Hobby plan is for non-commercial use** under Vercel's terms. If Tula's use
  counts as commercial, move to a paid Vercel plan or another host; the web app uses
  nothing Vercel-specific.
- **The web view is open to anyone with its link** unless `VIEW_PASSWORD` is set.
- **Scheduled workflows in public repositories are auto-disabled after 60 days of no
  repository activity.** GitHub does not define "activity", and nothing documented says
  re-enabling a workflow through the API resets the clock. So the keep-alive does both:
  it re-enables through the API weekly, and commits a marker file only after 45 quiet
  days, since a commit unambiguously counts. If the schedule stops anyway: Actions →
  Pipeline → **Enable workflow**.
- **The `items` archive is not built.** The brief's plan is to move old `items` rows to
  an `items_archive` tab once the sheet passes 70% of its cell limit. Two problems: a
  tab in the same spreadsheet does not reduce the cell count, and scoring recomputes
  each insight from its items, so archived items would drop out of their insights'
  counts. At the first runs' rates the threshold is roughly 2½ to 6 years away (an
  estimate). Every run's summary reports the sheet's size against the threshold, so
  there is plenty of warning to decide on a second spreadsheet before it matters.

---

## Where things are

```
config/                    all tunable values
prompts/                   model prompts, each with a version
scripts/setup_sheet.py     create and validate the tabs
scripts/check_secrets.py   the CI credential tripwire
scripts/calibrate_dedupe.py  checks the dedupe thresholds against the real model
src/tula_insights/
  schema.py                the tabs and their columns — the single source of truth
  models.py                domain objects, and how each maps to a row
  config.py                YAML loading and validation
  store/sheets.py          all Google Sheets I/O; nothing else imports gspread
  search/                  the SearchProvider interface and its Claude implementation
  collectors/              one module per source, plus the registry
  llm/                     the Claude client, token accounting and prompt loading
  stages/                  one module per pipeline stage
  budget.py                spend caps, read from the runs tab
  alerts.py                the pipeline-failure issue: open, comment, close
  keepalive.py             re-enable the schedule; the 45-day marker commit
  github_api.py            the few GitHub REST calls the two above need
  utils/                   serialisation, clock, logging, PII, retry, rate limits
tests/                     pytest; no network
web/                       the read-only view and /api/feed (Next.js)
```
