// Reads the real sheet the way the web app does and prints counts only (this repository
// is public). Needs GOOGLE_SERVICE_ACCOUNT_JSON and SHEET_ID. Run: npm run smoke

import { handleFeedRequest } from "../lib/feedApi.ts";
import { toFeedEntry, toInsight, toRun } from "../lib/model.ts";
import { readTab } from "../lib/sheets.ts";
import { todaySast } from "../lib/dates.ts";

const [feed, insights, runs] = await Promise.all([readTab("feed"), readTab("insights"), readTab("runs")]);
const entries = feed.rows.map(toFeedEntry);
const today = todaySast();
const dates = entries.map((e) => e.feed_date).filter(Boolean).sort();
const parsedRuns = runs.rows.map(toRun).sort((a, b) => b.started_at.localeCompare(a.started_at));

console.log(`feed rows        ${entries.length} (today ${today}: ${entries.filter((e) => e.feed_date === today).length}; latest date ${dates.at(-1) ?? "none"})`);
console.log(`insights         ${insights.rows.map(toInsight).filter((i) => i.insight_id).length}`);
console.log(`runs             ${parsedRuns.length} (latest started ${parsedRuns[0]?.started_at || "never"}, status ${parsedRuns[0]?.status || "-"})`);

const token = "smoke-test-token";
const response = await handleFeedRequest(
  new Request("https://smoke.test/api/feed", { headers: { Authorization: `Bearer ${token}` } }),
  { token, readTab },
);
const body = await response.json();
console.log(`/api/feed        HTTP ${response.status}, ${body.count} item(s) for ${body.feed_date}`);
if (response.status !== 200) process.exit(1);
