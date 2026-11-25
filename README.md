# Tula Insights Layer

Layer 1 of Tula's content engine. It finds the questions South African and international
pet owners are actually asking, deduplicates them into insights, scores them, and
publishes a ranked daily feed that Irene and Gigi's content pipeline reads.

Everything is stored in one Google Sheet. Everything runs on GitHub Actions. The only
paid service is the Claude API.

> **Build status: stage 2 of 10 complete.** Scaffold, configuration, the Sheets schema
> and discovery (general web search plus manual submissions) are in place. The classifier, scoring and feed stages land next —
> see [`IMPLEMENTATION_BRIEF.md`](IMPLEMENTATION_BRIEF.md) section 11 for the order, and
> the [Build progress](#build-progress) table below for where things stand.

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
to the pipeline and will be overwritten.

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

### 4. Create the tabs

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

Add `--source web_questions` or `--source manual` to run just one.

In Actions, use the **Collect (manual)** workflow (Actions → Collect (manual) → Run
workflow). It is dispatch-only and defaults to a dry run; the scheduled pipeline that
chains every stage arrives in stage 6.

Stages that are not built yet exit with code 3 and say which build stage they arrive in.
Once they land, each takes the same options:

| Option | Effect |
|---|---|
| `--source NAME` | Limit to one source from `config/sources.yaml` |
| `--category ID` | Limit to one category from `config/categories.yaml` |
| `--dry-run` | Process everything, write nothing to Sheets, print a summary |
| `--sync` | Use the standard Claude API instead of the Batch API |
| `--log-format plain` | Human-readable logs instead of JSON |

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

## Configuration

Everything tunable lives in `config/`. No magic numbers in code.

| File | Controls |
|---|---|
| `categories.yaml` | The 10 categories, their descriptions and example questions. Feeds the classifier prompt. |
| `sources.yaml` | Which collectors run, their rate limits and daily caps |
| `keywords.yaml` | Keyword sets per category and region; seed phrases |
| `scoring.yaml` | Dedupe thresholds, score weights, publishing rules |
| `budget.yaml` | Monthly and daily spend caps, and the price table |
| `settings.yaml` | Sheet sizing, retention, Sheets rate limits, logging |

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
`monthly_caps`, and `max_uses_per_request` per search request. Hitting a cap skips the
rest of that source and flags it in the summary; it does not fail the run. The ledger
reads what previous runs recorded in the `runs` tab, so there is no separate counter to
drift.

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
| 2 | Search collector + `SearchProvider` (`web_questions`, plus `manual` brought forward) | **Done** |
| 3 | Classifier (Claude Haiku, batch + sync) | Next |
| 4 | Dedupe + scoring | — |
| 5 | Publish feed | — |
| 6 | Full `pipeline.yml` | — |
| 7 | `web_questions`, `youtube`, `news_rss`, `manual` | — |
| 8 | Alerting, keep-alive, budget guardrails | — |
| 9 | Web view on Vercel | — |
| 10 | Full README (brief section 12) | — |

---

## Known limitations

Recorded as they are designed in, not discovered later.

- **Reddit cannot be collected automatically.** Reddit blocks Anthropic's crawler, so
  the Claude web search tool rejects any search restricted to `reddit.com` (confirmed on
  25 November 2026). The Reddit Data API is out of scope (§13): commercial use needs a
  contract, and self-service access closed in November 2025. The `reddit_search` source
  is kept but disabled. **Reddit threads come in by hand** through `manual_submissions`.
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
- **YouTube search quota is scarce** and capped separately from other calls. Quota
  resets at midnight Pacific.
- **Google Sheets is the only store**, which assumes a single writer. The Actions
  concurrency group enforces that. Collectors append only their own source's rows, so
  parallel collection is safe.
- **Scheduled workflows in public repositories are auto-disabled after 60 days of no
  repository activity.** Handled in stage 8; the manual fallback is the Actions tab →
  re-enable the workflow.

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
  utils/                   serialisation, clock, logging, PII, retry, rate limits
tests/                     pytest; no network
web/                       the read-only view (stage 9)
```
