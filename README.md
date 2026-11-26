# Tula Insights Layer

Layer 1 of Tula's content engine. It finds the questions South African and international
pet owners are actually asking, deduplicates them into insights, scores them, and
publishes a ranked daily feed that Irene and Gigi's content pipeline reads.

Everything is stored in one Google Sheet. Everything runs on GitHub Actions. The only
paid service is the Claude API.

> **Build status: stage 8 of 10 complete.** The full pipeline — discovery (general web
> search, YouTube comments, news feeds, manual submissions), classification, dedupe and
> scoring, and the daily feed — runs every day at 05:17 SAST in GitHub Actions, with
> failure alerts, a keep-alive and budget caps. Still to come: the web view and the full
> README — see [`IMPLEMENTATION_BRIEF.md`](IMPLEMENTATION_BRIEF.md) section 11 for the
> order, and the [Build progress](#build-progress) table below for where things stand.

---

## How a run will work

```
collect  ──▶  classify  ──▶  dedupe + score  ──▶  publish
   │              │                │                  │
   │              │                │                  └─▶  feed tab        (the contract)
   │              │                └──────────────────────▶  insights tab
   │              └───────────────────────────────────────▶  items tab (classified)
   └──────────────────────────────────────────────────────▶  items tab (new)
```

Every stage reads its input from the sheet and writes its output back, so a stage can be
re-run on its own and nothing depends on a runner's filesystem surviving between jobs.

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
An organisation admin must also **enable the web search tool in the Claude Console** —
without it the discovery stages cannot run. Not needed until stage 2.

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

Add `--source web_questions`, `--source youtube` or `--source manual` to run just one.


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

Stages that are not built yet exit with code 3 and say which build stage they arrive in.
Once they land, each takes the same options:

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

Everything sits behind one shared password (`VIEW_PASSWORD`). The app reads the sheet on
the server only, with the pipeline's service account and the **read-only** Sheets scope,
so it cannot change anything; the key never reaches the browser. Each tab is cached for
five minutes. Search engines are told not to index it.

**The feed API**, for Irene and Gigi's pipeline:

```bash
curl -H "Authorization: Bearer $FEED_API_TOKEN" "https://<your-app>/api/feed?date=2026-11-26"
```

`date` defaults to today (SAST). The reply is JSON: `feed_date`, `count`, `fetched_at`,
`stale`, and `items` — each with the `feed` tab's columns (`rank`, `insight_id`,
`question`, `category`, `tags` and `evidence_urls` as lists, `suggested_angle`,
`total_score`, `served_at`, `status`, `rejection_reason`, `post_ref`). `reviewed_by` is
left out. A wrong or missing token gets 401; with no `FEED_API_TOKEN` set on the server
the API is off (503).

### Deploying on Vercel

1. In Vercel, import (or reconnect) this repository and set **Root Directory** to `web`.
   Vercel detects Next.js; leave the build settings at their defaults.
2. Under **Settings → Environment Variables**, add these four for Production and Preview:

   | Variable | Value |
   |---|---|
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | The same base64 key as the GitHub secret |
   | `SHEET_ID` | The spreadsheet ID |
   | `VIEW_PASSWORD` | A password to share with the team |
   | `FEED_API_TOKEN` | A long random string, e.g. from `openssl rand -hex 32`; give it only to whoever runs the consuming pipeline |

3. Deploy. Without `VIEW_PASSWORD` the pages stay locked; without `FEED_API_TOKEN` the
   API stays off. Changing `VIEW_PASSWORD` (and redeploying) signs everyone out.

The app uses nothing Vercel-specific, so any host that runs Node.js 20.9 or later works:
`npm ci && npm run build && npm start` in `web/`, with the same four variables. Vercel's
free Hobby plan is for non-commercial use under its terms — check them against how Tula
uses this, and move hosts if needed.

**Locally:** copy `web/.env.example` to `web/.env.local`, fill it in, then:

```bash
cd web && npm ci && npm run dev
```

The **Web** workflow (`.github/workflows/web.yml`) tests and builds the app on every
change to `web/`, then reads the real sheet the way the app does and prints counts.

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

**Adding a category** is a YAML-only change in `categories.yaml`, plus at least one
keyword set in `keywords.yaml` — config validation refuses a category that no keyword
set can ever discover. Bump the classifier prompt version when you do.

**Adding a source** means a module in `src/tula_insights/collectors/`, an entry in
`sources.yaml`, a line in `collectors/registry.py`, and a test. Nothing else changes. A
source that is enabled in YAML but has no module fails at planning time rather than
silently collecting nothing.

**Caps.** Three of them apply to every search, and whichever bites first wins:
`sources.yaml` → `daily_request_cap` per source, `budget.yaml` → `daily_caps` and
`monthly_caps`, and `max_uses_per_request` per search request. The ledger reads what
previous runs recorded in the `runs` tab, so there is no separate counter to drift.

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

Config errors fail at startup with the file, the field and the reason, before a run
spends anything:

```
Config error:
budget.yaml is invalid:
  - limits: max_input_tokens_per_request must stay below long_context_threshold_tokens,
    where requests are billed at a multiple of standard rates
```

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
| 9 | Web view on Vercel | **Built** — Vercel deploy pending |
| 10 | Full README (brief section 12) | — |

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

Cost is roughly US$0.05 a day for about 400 items in batch mode. The system prompt
(about 1,700 tokens) is cached, and the run checks the monthly cap before it starts.

## How dedupe and scoring work

Each classified question is turned into a vector by a small local model
(`BAAI/bge-small-en-v1.5`, run on the GitHub runner, no API cost) and compared with every
insight's question:

| Similarity | What happens |
|---|---|
| 0.90 or more | Merged into that insight — no API call |
| 0.80 to 0.90 | Claude Haiku judges whether it is really the same question (up to 60 a run; the rest wait for the next run) |
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
  parallel collection is safe.
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
  counts. At about 250 items a day the threshold is roughly three years away (an
  estimate). Every run's summary reports the sheet's size against the threshold, so
  there is plenty of warning to decide on a second spreadsheet before it matters.

---

## Where things are

```
config/                    all tunable values
prompts/                   model prompts, versioned (from stage 2)
scripts/setup_sheet.py     create and validate the tabs
scripts/check_secrets.py   the CI credential tripwire
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
