/**
 * The shared-password gate and the feed API token.
 *
 * The session cookie holds an HMAC of a fixed label keyed by VIEW_PASSWORD — never the
 * password itself. It can't be forged without the password, and changing the password
 * signs everyone out. Every comparison is constant-time. With VIEW_PASSWORD or
 * FEED_API_TOKEN unset, access is refused rather than left open.
 */

import { createHash, createHmac, timingSafeEqual } from "node:crypto";

export const SESSION_COOKIE = "tula_view";
export const SESSION_DAYS = 30;
const SESSION_LABEL = "tula-insights-view-v1";

export function sessionValue(password: string): string {
  return createHmac("sha256", password).update(SESSION_LABEL).digest("hex");
}

/** Constant-time string comparison (hashing first makes the lengths equal). */
export function safeEqual(a: string, b: string): boolean {
  const left = createHash("sha256").update(a).digest();
  const right = createHash("sha256").update(b).digest();
  return timingSafeEqual(left, right) && a.length === b.length;
}

export function hasValidSession(cookie: string | undefined, password: string | undefined): boolean {
  if (!password || !cookie) return false;
  return safeEqual(cookie, sessionValue(password));
}

export function passwordMatches(given: string, password: string | undefined): boolean {
  if (!password) return false;
  return safeEqual(given, password);
}

/** The token from `Authorization: Bearer …` or `X-Feed-Token: …`. */
export function tokenFrom(headers: Headers): string {
  const auth = headers.get("authorization") ?? "";
  const match = /^Bearer\s+(.+)$/i.exec(auth.trim());
  if (match) return match[1].trim();
  return (headers.get("x-feed-token") ?? "").trim();
}

/** Only same-site paths, so a crafted login link can't send people elsewhere. */
export function safeNext(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return "/";
  return value;
}
