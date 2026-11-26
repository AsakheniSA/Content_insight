"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { passwordMatches, SESSION_COOKIE, SESSION_DAYS, safeNext, sessionValue } from "@/lib/auth.ts";

export async function login(formData: FormData) {
  const next = safeNext(String(formData.get("next") ?? "/"));
  const given = String(formData.get("password") ?? "");
  const password = process.env.VIEW_PASSWORD;

  if (!passwordMatches(given, password)) {
    // A short pause makes guessing slower without bothering a person.
    await new Promise((resolve) => setTimeout(resolve, 600));
    redirect(`/login?error=1&next=${encodeURIComponent(next)}`);
  }
  (await cookies()).set(SESSION_COOKIE, sessionValue(password as string), {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_DAYS * 24 * 3600,
  });
  redirect(next);
}
