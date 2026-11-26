/**
 * Read-only access to the Tula Insights spreadsheet. Server-side only.
 *
 * Authenticates as the pipeline's service account with the read-only Sheets scope, so
 * this app cannot change the sheet even if it wanted to. Each tab is cached in memory for
 * five minutes per server instance, which keeps the app far inside the Sheets API's
 * per-minute read quota. If a refresh fails, the last good copy is served (marked stale)
 * for up to an hour rather than an error page.
 */

import { JWT } from "google-auth-library";

export type Row = Record<string, string>;

export const SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly";
export const TTL_MS = 5 * 60 * 1000;
export const STALE_MAX_MS = 60 * 60 * 1000;

export class SheetError extends Error {}

export interface ServiceAccount {
  client_email: string;
  private_key: string;
}

/** Decode GOOGLE_SERVICE_ACCOUNT_JSON: base64 (preferred) or raw JSON, like the pipeline. */
export function parseServiceAccount(raw: string | undefined): ServiceAccount {
  const text = (raw ?? "").trim();
  if (!text) throw new SheetError("GOOGLE_SERVICE_ACCOUNT_JSON is not set.");
  let json = text;
  if (!text.startsWith("{")) {
    json = Buffer.from(text, "base64").toString("utf8");
    if (!json.trim().startsWith("{")) {
      throw new SheetError("GOOGLE_SERVICE_ACCOUNT_JSON is neither base64 of the key file nor raw JSON.");
    }
  }
  let info: Record<string, unknown>;
  try {
    info = JSON.parse(json);
  } catch {
    throw new SheetError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON.");
  }
  if (typeof info.client_email !== "string" || typeof info.private_key !== "string") {
    throw new SheetError("GOOGLE_SERVICE_ACCOUNT_JSON is missing client_email or private_key.");
  }
  return { client_email: info.client_email, private_key: info.private_key };
}

/** Turn a tab's values into rows keyed by its header, skipping blank rows. */
export function rowsFromValues(values: unknown[][] | undefined): Row[] {
  if (!values || values.length === 0) return [];
  const header = values[0].map((h) => String(h ?? "").trim());
  const rows: Row[] = [];
  for (const raw of values.slice(1)) {
    const cells = raw.map((c) => (c === null || c === undefined ? "" : String(c)));
    if (!cells.some((c) => c.trim())) continue;
    const row: Row = {};
    header.forEach((name, i) => {
      if (name) row[name] = cells[i] ?? "";
    });
    rows.push(row);
  }
  return rows;
}

export type FetchValues = (tab: string) => Promise<unknown[][]>;

export interface TabResult {
  rows: Row[];
  fetchedAt: number;
  stale: boolean;
}

/** A cached reader around any fetcher. Exported so it can be tested without Google. */
export function makeReader(fetchValues: FetchValues, now: () => number = Date.now) {
  const cache = new Map<string, { rows: Row[]; at: number }>();
  const inFlight = new Map<string, Promise<TabResult>>();

  return async function read(tab: string): Promise<TabResult> {
    const hit = cache.get(tab);
    if (hit && now() - hit.at < TTL_MS) return { rows: hit.rows, fetchedAt: hit.at, stale: false };
    const pending = inFlight.get(tab);
    if (pending) return pending;

    const job = (async () => {
      try {
        const rows = rowsFromValues(await fetchValues(tab));
        const at = now();
        cache.set(tab, { rows, at });
        return { rows, fetchedAt: at, stale: false };
      } catch (error) {
        if (hit && now() - hit.at < STALE_MAX_MS) {
          return { rows: hit.rows, fetchedAt: hit.at, stale: true };
        }
        throw error;
      } finally {
        inFlight.delete(tab);
      }
    })();
    inFlight.set(tab, job);
    return job;
  };
}

let client: JWT | null = null;

function googleFetcher(): FetchValues {
  return async (tab: string) => {
    const sheetId = (process.env.SHEET_ID ?? "").trim();
    if (!sheetId) throw new SheetError("SHEET_ID is not set.");
    if (!client) {
      const account = parseServiceAccount(process.env.GOOGLE_SERVICE_ACCOUNT_JSON);
      client = new JWT({ email: account.client_email, key: account.private_key, scopes: [SCOPE] });
    }
    let token: string | null | undefined;
    try {
      token = (await client.getAccessToken()).token;
    } catch {
      throw new SheetError("Could not sign in to Google with the service account.");
    }
    if (!token) throw new SheetError("Google returned no access token for the service account.");

    const url =
      `https://sheets.googleapis.com/v4/spreadsheets/${encodeURIComponent(sheetId)}` +
      `/values/${encodeURIComponent(tab)}?majorDimension=ROWS&valueRenderOption=UNFORMATTED_VALUE`;
    const response = await fetch(url, { headers: { Authorization: `Bearer ${token}` }, cache: "no-store" });
    if (!response.ok) {
      const hint =
        response.status === 403 || response.status === 404
          ? " Check SHEET_ID and that the sheet is shared with the service account."
          : "";
      throw new SheetError(`Google Sheets returned HTTP ${response.status} reading the ${tab} tab.${hint}`);
    }
    const body = (await response.json()) as { values?: unknown[][] };
    return body.values ?? [];
  };
}

let reader: ((tab: string) => Promise<TabResult>) | null = null;

/** Read one tab of the real sheet, through the five-minute cache. */
export function readTab(tab: string): Promise<TabResult> {
  if (!reader) reader = makeReader(googleFetcher());
  return reader(tab);
}
