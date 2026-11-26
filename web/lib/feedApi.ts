/**
 * GET /api/feed?date=YYYY-MM-DD — the day's feed as JSON, for Irene and Gigi's pipeline.
 *
 * Send the token as `Authorization: Bearer <FEED_API_TOKEN>` (or `X-Feed-Token`).
 * `date` defaults to today in SAST. Kept free of Next.js imports so it can be tested and
 * moved to another host unchanged.
 */

import { safeEqual, tokenFrom } from "./auth.ts";
import { isIsoDate, todaySast } from "./dates.ts";
import { type FeedEntry, toFeedEntry } from "./model.ts";
import type { TabResult } from "./sheets.ts";

export interface FeedApiDeps {
  token: string | undefined;
  readTab: (tab: string) => Promise<TabResult>;
  now?: () => Date;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

export function publicEntry(entry: FeedEntry) {
  // `reviewed_by` names a person on the team; the consuming pipeline doesn't need it.
  const { reviewed_by: _omit, ...rest } = entry;
  return rest;
}

export async function handleFeedRequest(request: Request, deps: FeedApiDeps): Promise<Response> {
  if (!deps.token) {
    return json(503, { error: "FEED_API_TOKEN is not set on the server, so the feed API is switched off." });
  }
  const given = tokenFrom(request.headers);
  if (!given || !safeEqual(given, deps.token)) {
    return json(401, { error: "Missing or wrong token. Send Authorization: Bearer <FEED_API_TOKEN>." });
  }

  const params = new URL(request.url).searchParams;
  const requested = params.get("date");
  if (requested !== null && !isIsoDate(requested)) {
    return json(400, { error: "date must be a real date in the form YYYY-MM-DD." });
  }
  const date = requested ?? todaySast(deps.now?.() ?? new Date());

  let result: TabResult;
  try {
    result = await deps.readTab("feed");
  } catch {
    return json(502, { error: "The feed could not be read from the spreadsheet. Try again shortly." });
  }

  const items = result.rows
    .map(toFeedEntry)
    .filter((e) => e.feed_date === date && e.insight_id)
    .sort((a, b) => a.rank - b.rank)
    .map(publicEntry);

  return json(200, {
    feed_date: date,
    count: items.length,
    fetched_at: new Date(result.fetchedAt).toISOString(),
    stale: result.stale,
    items,
  });
}
