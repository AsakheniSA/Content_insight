/**
 * The daily trigger: asks GitHub to start the pipeline, as a backup to GitHub's own
 * schedule, which has been firing hours late or not at all.
 *
 * Vercel calls GET /api/cron/run-pipeline once a day (vercel.json) and, when CRON_SECRET
 * is set, sends `Authorization: Bearer <CRON_SECRET>`. This starts the Pipeline workflow
 * with `only_if_not_done`, so if the day's run already happened it stops at once and
 * spends nothing. Kept free of Next.js imports so it can move hosts unchanged: any
 * scheduler that can make an HTTP request with that header will do.
 */

import { safeEqual, tokenFrom } from "./auth.ts";

export const DEFAULT_REPO = "AsakheniSA/Content_insight";
export const WORKFLOW = "pipeline.yml";

export interface DispatchDeps {
  cronSecret: string | undefined;
  githubToken: string | undefined;
  repo?: string;
  ref?: string;
  fetchFn?: typeof fetch;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

export async function handleDispatch(request: Request, deps: DispatchDeps): Promise<Response> {
  if (!deps.cronSecret) {
    return json(503, { error: "CRON_SECRET is not set, so the daily trigger is switched off." });
  }
  const given = tokenFrom(request.headers);
  if (!given || !safeEqual(given, deps.cronSecret)) {
    return json(401, { error: "Not authorised." });
  }
  if (!deps.githubToken) {
    return json(503, { error: "GITHUB_DISPATCH_TOKEN is not set, so the pipeline cannot be started." });
  }

  const repo = deps.repo || DEFAULT_REPO;
  const url = `https://api.github.com/repos/${repo}/actions/workflows/${WORKFLOW}/dispatches`;
  let response: Response;
  try {
    response = await (deps.fetchFn ?? fetch)(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${deps.githubToken}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "pipeline-trigger/0.1",
      },
      body: JSON.stringify({
        ref: deps.ref || "main",
        // dry_run defaults to on for manual runs, so a real run says so explicitly.
        inputs: { dry_run: "false", only_if_not_done: "true" },
      }),
    });
  } catch {
    return json(502, { error: "GitHub could not be reached." });
  }
  if (response.status !== 204) {
    const hint =
      response.status === 401 || response.status === 403 || response.status === 404
        ? " Check GITHUB_DISPATCH_TOKEN: it needs Actions read and write on this repository."
        : "";
    return json(502, { error: `GitHub refused to start the pipeline (HTTP ${response.status}).${hint}` });
  }
  return json(200, { ok: true, started: WORKFLOW, note: "It stops at once if today's run is already done." });
}
