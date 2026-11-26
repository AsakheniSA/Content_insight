import { redirect } from "next/navigation";
import { safeNext } from "@/lib/auth.ts";
import { login } from "./actions.ts";

export const metadata = { title: "Sign in · Tula Insights" };

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const params = await searchParams;
  const next = safeNext(typeof params.next === "string" ? params.next : "/");
  const failed = params.error === "1";
  if (!process.env.VIEW_PASSWORD) redirect(next);
  return (
    <div className="login">
      <form action={login} className="card login-card">
        <h1>Tula Insights</h1>
        <p className="muted">Enter the team password to see the daily feed.</p>
        <input type="hidden" name="next" value={next} />
        <label htmlFor="password">Password</label>
        <input id="password" name="password" type="password" autoComplete="current-password" required autoFocus />
        {failed && <p className="error-text">That password is not right.</p>}
        <button type="submit">Sign in</button>
      </form>
    </div>
  );
}
