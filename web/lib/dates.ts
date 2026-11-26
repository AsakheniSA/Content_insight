/**
 * South Africa keeps UTC+2 all year, so "today" is computed with a fixed offset, as the
 * pipeline does (settings.yaml → run.local_utc_offset_hours).
 */

export const SAST_OFFSET_HOURS = 2;
/** A run older than this means the daily schedule has probably stopped. */
export const STALE_RUN_HOURS = 36;

export function todaySast(now: Date = new Date()): string {
  return new Date(now.getTime() + SAST_OFFSET_HOURS * 3600_000).toISOString().slice(0, 10);
}

export function isIsoDate(value: string | null | undefined): value is string {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

/** "2026-11-26T03:21:09Z" → "26 Nov 2026, 05:21 SAST". Blank or unparseable → "—". */
export function formatSast(value: string): string {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return "—";
  const local = new Date(date.getTime() + SAST_OFFSET_HOURS * 3600_000);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const hh = String(local.getUTCHours()).padStart(2, "0");
  const mm = String(local.getUTCMinutes()).padStart(2, "0");
  return `${local.getUTCDate()} ${months[local.getUTCMonth()]} ${local.getUTCFullYear()}, ${hh}:${mm} SAST`;
}

export function hoursSince(value: string, now: Date = new Date()): number | null {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return null;
  return (now.getTime() - date.getTime()) / 3600_000;
}
