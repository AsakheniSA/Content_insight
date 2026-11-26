// The shared-password gate (Next.js 16 renamed `middleware` to `proxy`).
// Every page needs the session cookie; /login and /api/feed handle their own access.

import { type NextRequest, NextResponse } from "next/server";
import { hasValidSession, SESSION_COOKIE } from "./lib/auth.ts";

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  if (pathname === "/login" || pathname.startsWith("/api/feed")) return NextResponse.next();

  const password = process.env.VIEW_PASSWORD;
  if (!password) {
    return new NextResponse("VIEW_PASSWORD is not set on the server, so the pages are locked.", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  }
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
