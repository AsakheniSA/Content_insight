import { handleDispatch } from "@/lib/dispatch.ts";

export async function GET(request: Request) {
  return handleDispatch(request, {
    cronSecret: process.env.CRON_SECRET,
    githubToken: process.env.GITHUB_DISPATCH_TOKEN,
    repo: process.env.PIPELINE_REPO,
  });
}
