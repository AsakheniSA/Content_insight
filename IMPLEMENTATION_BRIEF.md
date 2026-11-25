# Tula Insights Layer — Implementation Brief for Claude Code

> Read this whole file before writing any code. It is the source of truth for scope,
> decisions and build order. Where it says **VERIFY**, check the current official docs
> before implementing — do not rely on memory for endpoints, SDK syntax or limits.
> Brief written 25 Nov 2026; external facts below were checked on that date.

---

## 0. How to work with me (the engineer)

1. Build **one stage at a time** in the order in §11. After each stage, stop and give me:
   - what was built (files changed),
   - how to test it locally (exact commands),
   - how to test it in GitHub Actions (which workflow, which inputs),
   - any open questions or deviations from this brief.
   Then **wait for my approval** before starting the next stage.
2. Never invent API endpoints, parameters, SDK methods or library functions. If you are
   unsure, check the official docs; if you still can't confirm, say so and flag it.
3. If something in this brief turns out to be wrong or impossible (e.g. an API changed),
   stop and tell me with options — don't silently work around it.
4. Treat this as a production system: reliable, observable, modular, easy for teammates
   to plug into. Prefer simple and boring over clever.
5. Use South African English in docs and UI copy (e.g. "organisation", "colour").

---

## 1. Context

**Tula** is a new South African pet-care company. Its automated content engine has three layers:

1. **Context pull — research & insights (THIS PROJECT, owned by me).**
2. Content generation with the brand kit (owned by Irene and Gigi).
3. Human-in-the-loop review and rejection.

**Goal:** keep the content pipeline supplied with fresh, non-repeating, evidence-backed
insights. Target is **3 posts/day by week 2, sustained for 5 weeks**, starting with a
**pet travel campaign**, plus general pet-care tips.

**Market:** South Africa **plus international** (English-language sources).

**"Evidence-backed" means:** every insight is real and traceable — a genuine
question/pain point seen in public content, with source URL(s), date(s) and engagement
signals stored. No external fact-checking layer is required.

---

## 2. Decisions already made (do not revisit without asking)

| Topic | Decision |
|---|---|
| Language/runtime | Python 3.11+ |
| Persistent store | **Google Sheets is the ONLY store.** No Supabase, no other DB. |
| Orchestration | **GitHub Actions only.** No n8n, no other schedulers. |
| Repo visibility | **Public.** (Consequences in §9.) |
| Web/search discovery | **Claude API web search tool only** (server-side `web_search` tool, billed from existing Claude API credit). **No Brave, SerpApi, DataForSEO, Serper, Tavily or any other paid search provider.** No scraping of search engines. |
| Reddit | **Discovered via the Claude web search tool** restricted to `reddit.com` (`allowed_domains`). Use search results (title, URL, snippet/page age) only. No Reddit API, no fetching or scraping Reddit pages, no third-party Reddit resellers. |
| Facebook / Instagram | **No collectors and no scrapers.** Compliant path = the `manual_submissions` tab (§5). Possible later: comments on Tula's own posts via Meta's official API. |
| Human-readable view | Small read-only web page on **Vercel (free Hobby plan)** — my decision. Note: Vercel's fair-use terms say Hobby is for non-commercial use; keep the web app host-agnostic so it can move to another host with config changes only. |
| Alerts | Auto-opened **GitHub issue** on failed runs. |
| Feed consumer | Irene and Gigi's pipeline reads the `feed` tab (or a read-only JSON endpoint on the Vercel app). |
| Budget | **No budget for third-party paid services.** Everything paid runs on existing **Claude API credit** (tokens + web search fees). All other services must be free tiers. Still enforce configurable monthly caps on Claude spend (§6.5). |
| Categories | 10 categories in §4. |

---

## 3. Verified external constraints (as of 25 Nov 2026)

Build around these. Re-**VERIFY** endpoint details and SDK syntax when implementing.

- **Reddit:** Self-service Data API access closed Nov 2025 (approval required under the
  Responsible Builder Policy); unauthenticated `.json` endpoints return 403 since late May
  2026. → Use search-engine discovery only.
- **Google Custom Search JSON API:** closed to new customers; discontinued 1 Jan 2027. Don't use.
- **Bing Search API:** retired. Don't use.
- **Claude API web search tool:** server-side tool, **$10 per 1,000 searches plus
  standard token costs** (search results count as input tokens). Supports `max_uses`
  per request, `allowed_domains` and `blocked_domains`. An organisation admin must enable
  web search in the Claude Console. **VERIFY** in current docs: the tool version string,
  that it works with `claude-haiku-5-5`, whether it can run inside the Message Batches API
  (if not, discovery runs use the standard API), and what fields each result returns
  (URL, title, page age, snippets) so we store only what's returned.
  - Rough cost: ~40 searches/day ≈ 1,200/month ≈ $12 in search fees + tokens. Daily
    search cap in config.
  - **Limitation:** this is not Google — there is **no "People also ask" and no
    autocomplete data**. Those collectors are dropped (see §6.1).
- **Not used (no budget):** Brave Search API, SerpApi, DataForSEO, Serper, Tavily.
  Keep the search layer behind a `SearchProvider` interface so a provider could be added
  later without touching collectors.
- **YouTube Data API v3:** default 10,000 units/day; `commentThreads.list` and
  `videos.list` cost 1 unit. Google's docs are inconsistent on `search.list`
  (100 units vs a separate 100-calls/day bucket) — **treat search as scarce**: cap at
  ~50 searches/day, configurable. Quota resets midnight Pacific.
- **Claude API — Claude Haiku 5.5** (`claude-haiku-5-5`) for all bulk steps:
  - Standard: $0.10 / MTok input, $0.50 / MTok output; cache read $0.01 / MTok.
  - Batch: $0.05 input / $0.25 output per MTok.
  - **Prompts over 100,000 tokens are billed at 5× rates** → hard cap every request
    well under 100K tokens (enforce in code, e.g. max 20K input tokens per request).
  - Optional: Claude Sonnet 5.5 (`claude-sonnet-5-5`, $2/$10) only for the daily
    top-N angle generation if Haiku quality is insufficient — off by default.
  - **VERIFY** current SDK syntax for: Message Batches, prompt caching (`cache_control`,
    minimum cacheable length for Haiku 5.5), and structured outputs / JSON-schema
    output. If native structured outputs aren't available for the chosen model, use
    tool-use with a JSON schema. Always validate with Pydantic either way.
- **GitHub Actions:** free on standard runners for public repos. Scheduled workflows in
  public repos are **auto-disabled after 60 days of no repo activity**. Cron can be
  delayed (especially near the top of the hour) — schedule off the hour.
  In public repos, **logs, artifacts and job summaries are publicly visible.**
- **Google Sheets API:** has per-minute request quotas (**VERIFY** current numbers);
  spreadsheets have a cell limit (~10M cells, **VERIFY**). Design for batched writes.
- **Meta (FB/IG):** Instagram Hashtag Search needs App Review + IG Business account,
  30 unique hashtags per rolling 7 days, recent media = last 24h only. Not in scope now.

---

## 4. Categories (config-driven)

Each relevant item gets **one primary category** + optional **tags**. Store in
`config/categories.yaml` with `id`, `name`, `description` (used in the classifier
prompt), `example_questions`, and optional `tags`. Adding/renaming a category must be a
YAML-only change.

| id | Name | Scope |
|---|---|---|
| `documents_permits` | Documents & permits | Pet passports, import/export permits, microchips, paperwork |
| `vaccinations_health_certs` | Vaccinations & health certificates | Rabies timing, vet sign-off, titre tests, health certificates |
| `airline_transport` | Airline & transport rules | Cabin vs cargo, crate specs, breed restrictions, road/train travel |
| `travel_sickness_anxiety` | Travel sickness & anxiety | Motion sickness, sedation questions, calming, stress |
| `destination_disease_parasites` | Destination disease & parasite risk | Ticks, biliary, rabies areas, heartworm, regional risks |
| `pet_friendly_stays` | Pet-friendly stays & destinations | Accommodation, beaches, parks, rules at destinations |
| `sitters_boarding` | Sitters, boarding & kennels | Leaving the pet behind, choosing sitters, handover info |
| `gear_packing` | Gear & packing | Crates, harnesses, food/water on the road, checklists |
| `costs_planning` | Costs & planning | Budgets, timelines, insurance, planning ahead |
| `general_pet_care` | General pet care | Everyday care. Tags: `nutrition`, `grooming`, `behaviour`, `health`, `training` |

Items not about pets/pet care → `is_relevant = false` (kept for audit, never become insights).

---

## 5. Data model — Google Sheets ("Tula Insights" spreadsheet)

One spreadsheet; ID in env var `SHEET_ID`. Accessed via a Google Cloud **service account**
(the spreadsheet is shared with the service account's email). A setup script
(`scripts/setup_sheet.py`) must create missing tabs and header rows idempotently and
validate existing headers.

Column names are snake_case. Timestamps ISO 8601 UTC. Lists stored as `;`-separated
strings or JSON — pick one convention and use it everywhere.

### `items` — every collected item (+ its classification)
`item_id` (stable hash of source + external id/url) · `source` · `external_id` · `url` ·
`title` · `snippet` (≤500 chars, PII-scrubbed) · `published_at` · `collected_at` ·
`query` (what found it) · `region` · `engagement_json` (raw signals per source) ·
`engagement_pct` (0–1 percentile within its source, computed at scoring) · `run_id` ·
`status` (`new` / `batch_pending` / `classified` / `irrelevant` / `error`) ·
`is_relevant` · `primary_category` · `tags` · `question` (underlying question, rewritten
neutrally) · `pain_point` · `confidence` · `model` · `prompt_version` · `insight_id`
(after dedupe) · `error`

### `insights` — deduplicated questions
`insight_id` · `canonical_question` · `category` · `tags` · `item_count` · `source_count` ·
`sources` · `evidence_urls` (top 3 by engagement) · `first_seen` · `last_seen` ·
`recurrence_score` · `cross_source_score` · `engagement_score` · `recency_score` ·
`trend_score` · `feedback_adjustment` · `total_score` · `suggested_angle` ·
`angle_generated_at` · `status` (`active` / `retired`) · `refreshed_at` (last time new
evidence was added) · `last_served_at` · `times_served`

### `feed` — the daily ranked output (the contract with the content pipeline)
`feed_date` · `rank` · `insight_id` · `question` · `category` · `tags` · `suggested_angle` ·
`evidence_urls` · `total_score` · `served_at` · `status` (`served` / `used` / `rejected`) ·
`rejection_reason` · `post_ref` · `reviewed_by`

Reviewers and the content pipeline may edit **only** `status`, `rejection_reason`,
`post_ref`, `reviewed_by`. The pipeline must never overwrite those columns.

### `manual_submissions` — compliant input for FB/IG and anything else
`submitted_at` · `submitted_by` · `platform` · `url` · `question_or_quote` · `notes` ·
`processed` (pipeline sets to TRUE after ingesting into `items`)

### `runs` — one row per pipeline run
`run_id` · `trigger` (`schedule`/`dispatch`) · `inputs` · `started_at` · `finished_at` ·
`status` · `items_collected_by_source` (JSON) · `items_classified` · `items_irrelevant` ·
`new_insights` · `merged_into_existing` · `llm_judge_calls` · `served_today` ·
`tokens_in` · `tokens_out` · `tokens_cache_read` · `est_cost_usd` · `api_calls_by_service`
(JSON) · `actions_run_url` · `git_sha` · `errors` (short, no item text)

### `_embeddings` (hidden) — `insight_id` · `model` · `vector` (base64-encoded float32)
### `_batches` (hidden) — `batch_id` · `stage` · `submitted_at` · `item_ids` · `status` · `completed_at`

**Retention:** archive `items` rows older than 60 days to an `items_archive` tab (or a
second spreadsheet) when the sheet passes a configurable cell threshold.

**Concurrency model (replaces DB transactions):**
- The workflow `concurrency` group guarantees only one pipeline run at a time.
- Matrix collectors write only their own source's rows (append-only), so parallel appends
  don't conflict. Each collector reads existing `item_id`s for its source first to skip
  duplicates.
- **No double-serving:** the publish stage selects today's top N, appends them to `feed`
  and updates `insights.last_served_at`/`times_served` in the same stage. The consumer only
  reads `feed` rows for today's date — it never picks insights itself. An insight is
  eligible again only if `refreshed_at > last_served_at` (new evidence) and a configurable
  cool-down has passed.

---

## 6. Pipeline stages

### 6.1 Collect (one job per source)
Common interface in `collectors/base.py`:

```python
class Collector(Protocol):
    name: str
    def collect(self, cfg: SourceConfig, ctx: RunContext) -> list[RawItem]: ...
```

- Registry discovers enabled collectors from `config/sources.yaml`. Adding/removing a
  source = add a module + YAML entry; nothing else changes.
- Every collector: retries with exponential backoff + jitter, respects `Retry-After`,
  per-source rate limiter and **daily request caps from config**, timeouts on every
  HTTP call, structured logging (counts only).
- Store **only public content and metadata. No usernames, handles, author IDs, profile
  links or avatars.** Run a PII scrubber (emails, phone numbers, @handles, `u/` and `r/`
  user mentions → placeholder) before writing anything.
- Respect robots.txt and platform terms. Only official APIs or the user-submitted tab.

Collectors to build:
| Collector | Stage | Method |
|---|---|---|
| `reddit_search` | 2 (priority) | Claude web search tool with `allowed_domains: ["reddit.com"]`, driven by keyword sets from config (per region/category). Each request: one keyword set, `max_uses` capped (default 3), Claude returns a **structured list of the threads it found** (URL, title, snippet, date if available) — validate with Pydantic and keep **only URLs that appear in the tool's actual search results** (drop anything Claude didn't retrieve, to prevent invented URLs). Engagement = result position + recurrence across queries (no upvotes available — document this limitation). |
| `web_questions` | 7 | Same mechanism, general web: forums, Q&A sites, vet/pet-travel sites and airline/government pet pages (allow/block lists in config). Purpose: surface real questions people ask and current rules. Same URL-verification rule. |
| `youtube` | 7 | `search.list` for keyword sets (capped), `videos.list` for stats (views/likes/comments), `commentThreads.list` (top-level, relevance order, capped per video). Each comment that contains a question/pain point is its own item; store comment text but **no author fields**. Free (Google API key). |
| `news_rss` | 7 | Curated RSS feeds in config (pet/travel/vet outlets, SA + international) + optionally GDELT DOC API (**VERIFY** terms/endpoint). Store headline + summary snippet + URL + date. |
| `manual` | 7 | Ingests unprocessed rows from `manual_submissions`. |

### 6.2 Classify + extract (Claude Haiku 5.5)
- One call per item (or small groups if it measurably saves cost) returning JSON validated
  by Pydantic:
  `is_relevant, primary_category, tags[], question, pain_point, confidence (0–1)`.
- System prompt (categories + rules) loaded from `/prompts/classify_extract.md`, with
  categories injected from YAML; mark the stable prefix for **prompt caching**.
- **Scheduled runs use the Batch API.** Submit, record in `_batches`, poll up to a
  configurable limit (default 15 min); collect any finished batches (including from earlier
  runs); leave unfinished ones for the next run. `sync_mode` dispatch input → use the
  standard API instead.
- Invalid JSON → one retry with the validation error appended; then mark `error`.
- Log tokens (input/output/cache read/cache write) per call; aggregate per run.

### 6.3 Dedupe + score
- **Embeddings:** compute locally on the runner with a small model (suggested: `fastembed`
  with a ~384-dim English model; **VERIFY** package and model name). Cache the model files
  with `actions/cache`.
- For each new question: cosine similarity vs all vectors in `_embeddings` (in memory):
  - `≥ 0.90` → merge into that insight
  - `0.80–0.90` → Claude judge (`/prompts/dedupe_judge.md`, batched where possible): same
    underlying question? yes → merge, no → new
  - `< 0.80` → new insight
  Thresholds in `config/scoring.yaml`.
- Merging adds evidence, updates counts, `last_seen`, `refreshed_at`.
- **Score** (all weights/half-lives in `config/scoring.yaml`, each component normalised 0–1):
  - `recurrence` — log-scaled count of distinct items
  - `cross_source` — distinct sources / enabled sources
  - `engagement` — mean of items' within-source engagement percentiles
  - `recency` — exponential decay on `last_seen` (default half-life 14 days)
  - `trend` — items in last 7 days vs prior 21 days (ratio, capped)
  - `feedback_adjustment` — penalty for insights semantically close to `feed` rows marked
    `rejected` (reads reviewer decisions back in)
  - `total_score` = weighted sum + feedback adjustment.

### 6.4 Publish feed
- Pick top N eligible insights (default N = 6/day = 3 posts × 2 buffer; configurable),
  with a **category diversity rule** (max K per category per day, config) and a configurable
  mix of travel vs `general_pet_care`.
- Generate `suggested_angle` only for the selected N (`/prompts/angle.md`): one line, a
  content angle — not a full post. Model configurable (Haiku default).
- Append to `feed`; update `insights.last_served_at` / `times_served`.
- Write the `runs` row; write the GitHub job summary; upload a counts-only artifact.

### 6.5 Budget guardrails
- `config/budget.yaml`: monthly caps (Claude token spend USD, Claude web searches count,
  YouTube searches) and a price table (with the date the prices were checked). Track web
  search counts from the API response usage fields.
- Before each paid stage, compute month-to-date usage from `runs`; if a cap would be
  exceeded, skip that stage/source, log it, and flag it in the job summary (not a failure).

---

## 7. Repository layout

```
tula-insights/
├── IMPLEMENTATION_BRIEF.md
├── README.md
├── pyproject.toml            # deps + ruff + pytest config
├── .env.example
├── config/
│   ├── categories.yaml
│   ├── sources.yaml          # enabled flags, caps, regions, per-source settings
│   ├── keywords.yaml         # keyword sets per category & region; seed phrases
│   ├── scoring.yaml
│   └── budget.yaml
├── prompts/
│   ├── discover_search.md    # web search discovery (Reddit + web_questions)
│   ├── classify_extract.md   # versioned via a header line, e.g. "version: 1"
│   ├── dedupe_judge.md
│   └── angle.md
├── src/tula_insights/
│   ├── cli.py                # `python -m tula_insights <stage> [--source X] [--category Y] [--dry-run] [--sync]`
│   ├── config.py             # loads + validates YAML with Pydantic
│   ├── models.py             # RawItem, Classification, Insight, FeedRow, RunStats
│   ├── collectors/           # base.py, registry.py, reddit_search.py, web_questions.py,
│   │                         # youtube.py, news_rss.py, manual.py
│   ├── search/               # provider.py (SearchProvider interface), claude_web_search.py
│   ├── llm/                  # client.py (caching, batch, token accounting), prompts.py
│   ├── stages/               # collect.py, classify.py, dedupe_score.py, publish.py
│   ├── store/sheets.py       # all Sheets I/O, batched; the only module that touches Sheets
│   └── utils/                # retry.py, ratelimit.py, pii.py, logging.py, embeddings.py
├── scripts/setup_sheet.py
├── tests/                    # pytest; recorded API fixtures; no network in unit tests
├── web/                      # Next.js read-only view for Vercel (§10)
└── .github/workflows/
    ├── pipeline.yml
    ├── keepalive.yml         # if needed (§8)
    └── ci.yml                # ruff + pytest on PRs
```

`--dry-run` = collect and process but write nothing to Sheets (print a summary instead).
All Sheets access goes through `store/sheets.py` so the store could be swapped later.

---

## 8. GitHub Actions

### `pipeline.yml`
- Triggers: `schedule` (cron in UTC, **off the hour**, e.g. `17 3 * * *` = 05:17 SAST/CAT)
  and `workflow_dispatch` with inputs: `source` (default `all`), `category` (default `all`),
  `sync_mode` (bool), `dry_run` (bool).
- `concurrency: { group: tula-insights-pipeline, cancel-in-progress: false }`.
- `permissions:` least privilege (`contents: read`, `issues: write` only where needed).
- Jobs (each with `timeout-minutes`):
  1. `plan` — reads `sources.yaml` (+ inputs) → outputs matrix JSON.
  2. `collect` — `needs: plan`, `strategy.matrix` per source, **`fail-fast: false`**.
  3. `classify` — `needs: collect`, `if: always()` and proceed if ≥1 source succeeded.
  4. `score` — dedupe + score.
  5. `publish` — feed + runs row + job summary + artifact.
  6. `alert` — `if: failure()` (or any upstream failure) → GitHub issue (§ below).
- State between jobs lives **in Sheets only** (item `status` column drives each stage).
  Never rely on the runner filesystem for state.
- `actions/setup-python` with pip caching; cache the embedding model directory.
- Secrets from GitHub Secrets only (§9). Never echo secrets or item text.
- Job summary (`$GITHUB_STEP_SUMMARY`): per-source counts, classified, irrelevant,
  new/merged insights, served today, tokens, estimated cost, budget warnings.
- Artifacts: `run_summary.json` (counts only) and scrubbed logs. Retention 14 days.

### Alerts
On any failed job: open a GitHub issue labelled `pipeline-failure` with run link, failed
jobs and short error lines (no item text). If an open issue with that label exists,
comment on it instead of opening a new one. On the next fully successful run, comment and
close it.

### Keep-alive (public repo, 60-day rule)
Research the current recommended approach (e.g. a maintained keep-alive action that uses
the GitHub API rather than dummy commits) — **VERIFY** it works for this case — and
implement it. Document the manual fallback in the README (Actions tab → re-enable workflow).

### `ci.yml`
On PRs and pushes to `main`: ruff, pytest, and a config-validation step.

---

## 9. Secrets, config and public-repo hygiene

GitHub Secrets (and `.env.example` for local runs):
```
ANTHROPIC_API_KEY=              # also used for the web search tool
GOOGLE_SERVICE_ACCOUNT_JSON=     # full JSON key, single line or base64 — pick one and document it
SHEET_ID=
YOUTUBE_API_KEY=
```
Vercel env vars (web app): `GOOGLE_SERVICE_ACCOUNT_JSON`, `SHEET_ID`, `VIEW_PASSWORD`,
`FEED_API_TOKEN`.

Because the repo is **public**:
- Logs, artifacts and summaries must contain **counts and IDs only** — never item text,
  questions or URLs.
- `.gitignore` must cover `.env`, service-account JSON files, local caches, `*.xlsx`.
- Add a pre-commit or CI check that fails if a likely secret is committed.
- Prompts and config will be public — that's accepted.

---

## 10. Web view (`web/`, deployed to Vercel)

- Next.js (App Router), read-only. Reads Sheets **server-side only** with the service
  account; the key never reaches the browser.
- Simple shared-password gate (middleware + `VIEW_PASSWORD`).
- Pages:
  - **Today** — today's `feed` with question, category, angle, evidence links, status.
  - **Insights** — filter by category/tag/status, sort by score, search text.
  - **Runs** — recent `runs` rows, highlighting failures and budget warnings.
- `GET /api/feed?date=YYYY-MM-DD` — JSON for Irene and Gigi's pipeline, protected by
  `FEED_API_TOKEN` header.
- Short cache (e.g. 5 min) to stay within Sheets quotas. Brand: Tula purple `#270B51`,
  violet `#763CB0`, amber `#E4A45A`, cream; font Poppins.
- Keep it host-agnostic (no Vercel-only features that block moving hosts).

---

## 11. Build order and stage checkpoints

Stop after each stage for my approval (see §0).

| # | Stage | Done when | Local test | Actions test |
|---|---|---|---|---|
| 1 | Scaffold + config + Sheets schema | Repo layout, `pyproject`, config loaders with validation, `setup_sheet.py` creates/validates all tabs | `python scripts/setup_sheet.py` then inspect sheet; `pytest` | `ci.yml` green |
| 2 | `reddit_search` collector (Claude web search) + `SearchProvider` interface | Items land in `items` with `status=new`, deduped by `item_id`, PII scrubbed, caps honoured, only URLs present in actual search results kept, search count logged | `python -m tula_insights collect --source reddit_search --dry-run`, then without `--dry-run` | temporary dispatch-only workflow or step |
| 3 | Classifier | Items → classified/irrelevant; batch + sync modes; Pydantic validation; token logging | `classify --sync` on a handful of items; fixture tests | dispatch with `sync_mode` |
| 4 | Dedupe + scoring | `insights` + `_embeddings` populated; merges correct on a test set of near-duplicate questions | `score` + unit tests with known duplicate/non-duplicate pairs | dispatch |
| 5 | Publish feed | `feed` rows for today, no re-serving, diversity rule, angles, `runs` row | run twice in a row → second run serves nothing new unless refreshed | dispatch |
| 6 | Full `pipeline.yml` | Matrix, fail-fast off, concurrency, timeouts, caching, summary, artifacts | `act` optional; otherwise skip | dispatch full run + break one source deliberately to prove isolation |
| 7 | Remaining collectors | `web_questions`, `youtube`, `news_rss`, `manual` | per-source `--dry-run` | dispatch with `source=<name>` |
| 8 | Alerting + keep-alive + budget guardrails | Failure → issue; success → closes it; caps enforced | simulate a cap breach | force a failure via dispatch input |
| 9 | Web view | Pages + `/api/feed` working against the real sheet | `npm run dev` in `web/` | Vercel preview deploy |
| 10 | README | §12 complete | — | — |

---

## 12. README must cover

1. What the system does + diagram of stages.
2. Setup: Google Cloud service account, enabling the Sheets API, sharing the sheet with the
   service account, creating the sheet with `setup_sheet.py`, YouTube API key, Anthropic key
   and **enabling web search in the Claude Console**.
3. Every secret to add to GitHub and Vercel, and where.
4. How to run locally (each stage, dry-run, sync mode) and in Actions (dispatch inputs).
5. The `feed` contract for the content team (which columns they may edit, statuses).
6. How to **add a new source** (module + registry + YAML + test) and a **new category**
   (YAML only + prompt version bump).
7. Costs and caps: where to change them; how to read the job summary and `runs` tab.
8. Known limitations: Reddit via Claude web search only (no upvotes/comments, coverage
   depends on the search index); no "People also ask" or autocomplete data; YouTube search
   quota; Sheets as a store (single-writer assumption, size limits); Vercel
   Hobby terms; 60-day scheduled-workflow rule and how to re-enable.

---

## 13. Out of scope (for now)

Facebook/Instagram collectors; Reddit API; any scraping; any third-party paid search
provider (Brave, SerpApi, DataForSEO, Serper, Tavily); "People also ask" and autocomplete
data; Supabase or any database; n8n; content generation itself (owned by Irene and Gigi).
