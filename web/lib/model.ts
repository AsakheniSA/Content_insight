/**
 * The three tabs this app reads, parsed from rows. Column names match
 * src/tula_insights/schema.py; list cells are `;`-separated with `;` escaped as `%3B`.
 */

import type { Row } from "./sheets.ts";

export interface FeedEntry {
  feed_date: string;
  rank: number;
  insight_id: string;
  question: string;
  category: string;
  tags: string[];
  suggested_angle: string;
  evidence_urls: string[];
  total_score: number | null;
  served_at: string;
  status: string;
  rejection_reason: string;
  post_ref: string;
  reviewed_by: string;
}

export interface Insight {
  insight_id: string;
  canonical_question: string;
  category: string;
  tags: string[];
  item_count: number;
  source_count: number;
  sources: string[];
  evidence_urls: string[];
  first_seen: string;
  last_seen: string;
  total_score: number | null;
  suggested_angle: string;
  status: string;
  last_served_at: string;
  times_served: number;
}

export interface Run {
  run_id: string;
  trigger: string;
  started_at: string;
  finished_at: string;
  status: string;
  items_collected_by_source: Record<string, number>;
  items_collected: number;
  items_classified: number;
  items_irrelevant: number;
  new_insights: number;
  merged_into_existing: number;
  served_today: number;
  est_cost_usd: number;
  actions_run_url: string;
  errors: string[];
  budget_warnings: string[];
}

export function splitList(cell: string | undefined): string[] {
  return (cell ?? "")
    .split(";")
    .map((p) => p.trim().replaceAll("%3B", ";"))
    .filter(Boolean);
}

export function num(cell: string | undefined): number | null {
  const text = (cell ?? "").trim();
  if (!text) return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

function int(cell: string | undefined): number {
  return Math.round(num(cell) ?? 0);
}

function jsonObject(cell: string | undefined): Record<string, unknown> {
  try {
    const value = JSON.parse(cell ?? "");
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
}

const s = (row: Row, key: string) => (row[key] ?? "").trim();

export function toFeedEntry(row: Row): FeedEntry {
  return {
    feed_date: s(row, "feed_date"),
    rank: int(row.rank),
    insight_id: s(row, "insight_id"),
    question: s(row, "question"),
    category: s(row, "category"),
    tags: splitList(row.tags),
    suggested_angle: s(row, "suggested_angle"),
    evidence_urls: splitList(row.evidence_urls).filter(isWebUrl),
    total_score: num(row.total_score),
    served_at: s(row, "served_at"),
    status: s(row, "status") || "served",
    rejection_reason: s(row, "rejection_reason"),
    post_ref: s(row, "post_ref"),
    reviewed_by: s(row, "reviewed_by"),
  };
}

export function toInsight(row: Row): Insight {
  return {
    insight_id: s(row, "insight_id"),
    canonical_question: s(row, "canonical_question"),
    category: s(row, "category"),
    tags: splitList(row.tags),
    item_count: int(row.item_count),
    source_count: int(row.source_count),
    sources: splitList(row.sources),
    evidence_urls: splitList(row.evidence_urls).filter(isWebUrl),
    first_seen: s(row, "first_seen"),
    last_seen: s(row, "last_seen"),
    total_score: num(row.total_score),
    suggested_angle: s(row, "suggested_angle"),
    status: s(row, "status") || "active",
    last_served_at: s(row, "last_served_at"),
    times_served: int(row.times_served),
  };
}

export function toRun(row: Row): Run {
  const bySource: Record<string, number> = {};
  for (const [name, value] of Object.entries(jsonObject(row.items_collected_by_source))) {
    const n = Number(value);
    if (Number.isFinite(n)) bySource[name] = n;
  }
  const errors = splitList(row.errors);
  return {
    run_id: s(row, "run_id"),
    trigger: s(row, "trigger"),
    started_at: s(row, "started_at"),
    finished_at: s(row, "finished_at"),
    status: s(row, "status"),
    items_collected_by_source: bySource,
    items_collected: Object.values(bySource).reduce((a, b) => a + b, 0),
    items_classified: int(row.items_classified),
    items_irrelevant: int(row.items_irrelevant),
    new_insights: int(row.new_insights),
    merged_into_existing: int(row.merged_into_existing),
    served_today: int(row.served_today),
    est_cost_usd: num(row.est_cost_usd) ?? 0,
    actions_run_url: isWebUrl(s(row, "actions_run_url")) ? s(row, "actions_run_url") : "",
    errors,
    budget_warnings: errors.filter((e) => e.toLowerCase().startsWith("budget:")),
  };
}

export function isWebUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:";
  } catch {
    return false;
  }
}

export function isRedditUrl(value: string): boolean {
  try {
    const host = new URL(value).hostname.toLowerCase();
    return host === "reddit.com" || host.endsWith(".reddit.com") || host === "redd.it";
  } catch {
    return false;
  }
}

/** `https://www.reddit.com/r/PetAdvice/comments/…` → `r/PetAdvice`; "" if none. */
export function subredditOf(value: string): string {
  if (!isRedditUrl(value)) return "";
  const match = /\/r\/([A-Za-z0-9_]+)/.exec(new URL(value).pathname);
  return match ? `r/${match[1]}` : "";
}

/** The Reddit threads behind an entry, and their subreddits. */
export function redditEvidence(urls: string[]): { threads: string[]; subreddits: string[] } {
  const threads = urls.filter(isRedditUrl);
  const subreddits = [...new Set(threads.map(subredditOf).filter(Boolean))];
  return { threads, subreddits };
}

export function hostOf(value: string): string {
  try {
    return new URL(value).hostname.replace(/^www\./, "");
  } catch {
    return value;
  }
}

/** `airline_transport` → `Airline transport`. */
export function label(id: string): string {
  const text = id.replaceAll("_", " ").trim();
  return text ? text[0].toUpperCase() + text.slice(1) : "—";
}

/** Newest first, by an ISO timestamp field; blanks last. */
export function newestFirst<T>(items: T[], key: (item: T) => string): T[] {
  return [...items].sort((a, b) => key(b).localeCompare(key(a)));
}
