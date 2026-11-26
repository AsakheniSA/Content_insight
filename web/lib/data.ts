// Page-level loading: read a tab, parse it, and turn failures into a message.

import { connection } from "next/server";
import { type FeedEntry, type Insight, type Run, toFeedEntry, toInsight, toRun } from "./model.ts";
import { readTab, SheetError } from "./sheets.ts";

export interface Loaded<T> {
  items: T[];
  fetchedAt: number;
  stale: boolean;
  error: string;
}

async function load<T>(tab: string, parse: (row: Record<string, string>) => T): Promise<Loaded<T>> {
  await connection();
  try {
    const result = await readTab(tab);
    return { items: result.rows.map(parse), fetchedAt: result.fetchedAt, stale: result.stale, error: "" };
  } catch (error) {
    const message =
      error instanceof SheetError ? error.message : "The spreadsheet could not be read. Try again in a minute.";
    console.error(`sheet read failed: ${tab}`, error instanceof Error ? error.name : "unknown");
    return { items: [], fetchedAt: Date.now(), stale: false, error: message };
  }
}

export const loadFeed = () => load<FeedEntry>("feed", toFeedEntry);
export const loadInsights = () => load<Insight>("insights", toInsight);
export const loadRuns = () => load<Run>("runs", toRun);
