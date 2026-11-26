import assert from "node:assert/strict";
import { test } from "node:test";
import { hasValidSession, passwordMatches, safeEqual, safeNext, sessionValue, tokenFrom } from "../lib/auth.ts";
import { formatSast, hoursSince, isIsoDate, todaySast } from "../lib/dates.ts";
import { type FeedApiDeps, handleFeedRequest } from "../lib/feedApi.ts";
import { label, splitList, toFeedEntry, toInsight, toRun } from "../lib/model.ts";
import { makeReader, parseServiceAccount, rowsFromValues, STALE_MAX_MS, TTL_MS, type TabResult } from "../lib/sheets.ts";

// --- sheets ------------------------------------------------------------------------

test("rows are keyed by the header and blank rows are skipped", () => {
  const rows = rowsFromValues([["a", "b"], ["1", 2], [], ["", " "], ["3"]]);
  assert.deepEqual(rows, [{ a: "1", b: "2" }, { a: "3", b: "" }]);
});

test("the service account key is accepted as base64 or raw JSON", () => {
  const key = JSON.stringify({ client_email: "x@y.iam.gserviceaccount.com", private_key: "k" });
  assert.equal(parseServiceAccount(key).client_email, "x@y.iam.gserviceaccount.com");
  assert.equal(parseServiceAccount(Buffer.from(key).toString("base64")).private_key, "k");
  assert.throws(() => parseServiceAccount(""), /not set/);
  assert.throws(() => parseServiceAccount("bm90IGpzb24="), /neither/);
  assert.throws(() => parseServiceAccount("{}"), /missing/);
});

test("the cache serves for five minutes, then refreshes", async () => {
  let clock = 0;
  let calls = 0;
  const read = makeReader(async () => { calls++; return [["x"], [String(calls)]]; }, () => clock);
  assert.equal((await read("feed")).rows[0].x, "1");
  clock = TTL_MS - 1;
  assert.equal((await read("feed")).rows[0].x, "1");
  clock = TTL_MS + 1;
  assert.equal((await read("feed")).rows[0].x, "2");
  assert.equal(calls, 2);
});

test("a failed refresh serves the last good copy, marked stale, for up to an hour", async () => {
  let clock = 0;
  let fail = false;
  const read = makeReader(async () => { if (fail) throw new Error("down"); return [["x"], ["ok"]]; }, () => clock);
  await read("feed");
  fail = true;
  clock = TTL_MS + 1;
  const stale = await read("feed");
  assert.equal(stale.stale, true);
  assert.equal(stale.rows[0].x, "ok");
  clock = STALE_MAX_MS + 1;
  await assert.rejects(read("feed"), /down/);
});

test("simultaneous reads share one request", async () => {
  let calls = 0;
  const read = makeReader(async () => { calls++; return [["x"], ["1"]]; });
  await Promise.all([read("feed"), read("feed"), read("feed")]);
  assert.equal(calls, 1);
});

// --- model -------------------------------------------------------------------------

test("list cells split on ; and unescape %3B", () => {
  assert.deepEqual(splitList("a; b%3Bc ;;"), ["a", "b;c"]);
  assert.deepEqual(splitList(""), []);
});

test("feed rows parse, and only web links survive", () => {
  const e = toFeedEntry({
    feed_date: "2026-11-26", rank: "2", insight_id: "ins1", question: "Q?", category: "airline_transport",
    tags: "cats;flying", evidence_urls: "https://a.co/x;javascript:alert(1);http://b.org", total_score: "0.81234",
    status: "",
  });
  assert.equal(e.rank, 2);
  assert.deepEqual(e.tags, ["cats", "flying"]);
  assert.deepEqual(e.evidence_urls, ["https://a.co/x", "http://b.org"]);
  assert.equal(e.total_score, 0.81234);
  assert.equal(e.status, "served");
});

test("insights parse with sensible defaults", () => {
  const i = toInsight({ insight_id: "i", canonical_question: "Q", item_count: "", total_score: "x" });
  assert.equal(i.item_count, 0);
  assert.equal(i.total_score, null);
  assert.equal(i.status, "active");
});

test("runs sum collected items and pick out budget warnings", () => {
  const r = toRun({
    run_id: "gha-1-1", items_collected_by_source: '{"web_questions": 90, "youtube": 154}',
    errors: "budget: web_questions: daily cap;youtube: HTTP 400", est_cost_usd: "0.21",
    actions_run_url: "https://github.com/o/r/actions/runs/1",
  });
  assert.equal(r.items_collected, 244);
  assert.deepEqual(r.budget_warnings, ["budget: web_questions: daily cap"]);
  assert.equal(r.est_cost_usd, 0.21);
  assert.equal(toRun({ items_collected_by_source: "not json" }).items_collected, 0);
});

test("category ids read as words", () => {
  assert.equal(label("airline_transport"), "Airline transport");
  assert.equal(label(""), "—");
});

// --- dates -------------------------------------------------------------------------

test("today is the SAST date, which turns over at 22:00 UTC", () => {
  assert.equal(todaySast(new Date("2026-11-26T21:59:00Z")), "2026-11-26");
  assert.equal(todaySast(new Date("2026-11-26T22:00:00Z")), "2026-11-27");
});

test("only real dates are accepted", () => {
  assert.ok(isIsoDate("2026-11-26"));
  for (const bad of ["2026-02-30", "26-11-2026", "2026-11-26T00:00", "", null]) assert.ok(!isIsoDate(bad));
});

test("times show in SAST", () => {
  assert.equal(formatSast("2026-11-26T03:21:09Z"), "26 Nov 2026, 05:21 SAST");
  assert.equal(formatSast(""), "—");
  assert.equal(hoursSince("2026-11-26T00:00:00Z", new Date("2026-11-26T12:00:00Z")), 12);
});

// --- auth --------------------------------------------------------------------------

test("the session cookie proves the password without containing it", () => {
  const cookie = sessionValue("hunter2");
  assert.ok(!cookie.includes("hunter2"));
  assert.ok(hasValidSession(cookie, "hunter2"));
  assert.ok(!hasValidSession(cookie, "changed"), "changing the password signs everyone out");
  assert.ok(!hasValidSession(undefined, "hunter2"));
  assert.ok(!hasValidSession(cookie, undefined), "no password set means locked, not open");
});

test("passwords and tokens compare exactly", () => {
  assert.ok(passwordMatches("abc", "abc"));
  assert.ok(!passwordMatches("abc", "abcd"));
  assert.ok(!passwordMatches("", undefined));
  assert.ok(safeEqual("x", "x") && !safeEqual("x", "y"));
});

test("the token is read from either header", () => {
  assert.equal(tokenFrom(new Headers({ Authorization: "Bearer t0k" })), "t0k");
  assert.equal(tokenFrom(new Headers({ "X-Feed-Token": "t0k" })), "t0k");
  assert.equal(tokenFrom(new Headers()), "");
});

test("login only redirects within the site", () => {
  assert.equal(safeNext("/insights?q=x"), "/insights?q=x");
  for (const bad of ["https://evil.example", "//evil.example", "/\\evil.example", "", null]) assert.equal(safeNext(bad), "/");
});

// --- the feed API ------------------------------------------------------------------

const FEED: TabResult = {
  fetchedAt: Date.parse("2026-11-26T04:00:00Z"),
  stale: false,
  rows: [
    { feed_date: "2026-11-26", rank: "2", insight_id: "b", question: "Second?", reviewed_by: "Someone" },
    { feed_date: "2026-11-26", rank: "1", insight_id: "a", question: "First?" },
    { feed_date: "2026-11-25", rank: "1", insight_id: "c", question: "Yesterday?" },
  ],
};
const deps: FeedApiDeps = { token: "secret", readTab: async () => FEED, now: () => new Date("2026-11-26T10:00:00Z") };
const call = (path: string, headers: Record<string, string> = { Authorization: "Bearer secret" }, d = deps) =>
  handleFeedRequest(new Request(`https://x.test${path}`, { headers }), d);

test("the feed API returns today's feed in rank order, without reviewer names", async () => {
  const response = await call("/api/feed");
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("cache-control"), "no-store");
  const body = await response.json();
  assert.equal(body.feed_date, "2026-11-26");
  assert.deepEqual(body.items.map((i: { insight_id: string }) => i.insight_id), ["a", "b"]);
  assert.ok(!("reviewed_by" in body.items[1]));
});

test("the feed API takes a date", async () => {
  const body = await (await call("/api/feed?date=2026-11-25")).json();
  assert.equal(body.count, 1);
  assert.equal((await call("/api/feed?date=yesterday")).status, 400);
});

test("the feed API refuses a missing or wrong token, and is off without one", async () => {
  assert.equal((await call("/api/feed", {})).status, 401);
  assert.equal((await call("/api/feed", { Authorization: "Bearer nope" })).status, 401);
  assert.equal((await call("/api/feed", { "X-Feed-Token": "secret" })).status, 200);
  assert.equal((await call("/api/feed", { Authorization: "Bearer secret" }, { ...deps, token: undefined })).status, 503);
});

test("the feed API reports a sheet failure without details", async () => {
  const broken = { ...deps, readTab: async () => { throw new Error("private key xyz"); } };
  const response = await call("/api/feed", undefined, broken);
  assert.equal(response.status, 502);
  assert.ok(!(await response.text()).includes("xyz"));
});
