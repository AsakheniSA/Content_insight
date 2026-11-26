import { handleFeedRequest } from "@/lib/feedApi.ts";
import { readTab } from "@/lib/sheets.ts";

export async function GET(request: Request) {
  return handleFeedRequest(request, { token: process.env.FEED_API_TOKEN, readTab });
}
