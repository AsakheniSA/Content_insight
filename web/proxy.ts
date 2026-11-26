// The optional shared-password gate (Next.js 16 renamed `middleware` to `proxy`).
// With VIEW_PASSWORD unset (the default) the pages are open to anyone with the link.
// Set it to require a password; /login and /api/feed handle their own access.

import { type NextRequest, NextResponse } from "next/server";
import { hasValidSession, SESSION_COOKIE } from "./lib/auth.ts";

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  if (pathname === "/login" || pathname.startsWith("/api/feed")) return NextResponse.next();

  const password = process.env.VIEW_PASSWORD;
  if (!password) return NextResponse.next();
  if (hasValidSession(request.cookies.get(SESSION_COOKIE)?.value, password)) {
    return NextResponse.next();
  }
  const login = new URL("/login", request.url);
  login.searchParams.set("next", `${pathname}${search}`);
  return NextResponse.redirect(login);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|robots.txt).*)"],
};
