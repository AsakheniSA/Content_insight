import Link from "next/link";
import { Category, Chip, Evidence, Freshness, LastRun, Problem, Status } from "@/components/bits.tsx";
import { loadFeed, loadRuns } from "@/lib/data.ts";
import { isIsoDate, todaySast } from "@/lib/dates.ts";
import { redditEvidence } from "@/lib/model.ts";

export const metadata = { title: "Today · Tula Insights" };

function shiftDay(date: string, days: number): string {
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export default async function TodayPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const params = await searchParams;
  const today = todaySast();
  const date = typeof params.date === "string" && isIsoDate(params.date) ? params.date : today;
  const [feed, runs] = await Promise.all([loadFeed(), loadRuns()]);

  const entries = feed.items.filter((e) => e.feed_date === date).sort((a, b) => a.rank - b.rank);
  const latestDate = feed.items.map((e) => e.feed_date).filter(isIsoDate).sort().at(-1);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{date === today ? "Today's feed" : `Feed for ${date}`}</h1>
          <p className="muted">The questions to write about, best first. Mark them used or rejected in the sheet.</p>
        </div>
        <div className="day-nav">
          <Link href={`/?date=${shiftDay(date, -1)}`}>← Previous day</Link>
          {date !== today && <Link href="/">Today</Link>}
          {date < today && <Link href={`/?date=${shiftDay(date, 1)}`}>Next day →</Link>}
        </div>
      </div>

      {feed.error ? <Problem message={feed.error} /> : null}
      {runs.error ? null : <LastRun runs={runs.items} />}

      {!feed.error && entries.length === 0 && (
        <div className="card empty">
          <p>No feed for {date} yet.</p>
          {date === today && <p className="muted">The pipeline runs daily at 05:17 SAST and publishes when it finishes.</p>}
          {latestDate && latestDate !== date && (
            <p><Link href={`/?date=${latestDate}`}>See the latest feed ({latestDate}) →</Link></p>
          )}
        </div>
      )}

      <ol className="feed">
        {entries.map((e) => (
          <li key={`${e.feed_date}-${e.insight_id}`} className="card feed-item">
            <div className="rank">{e.rank}</div>
            <div className="feed-body">
              <h2>{e.question}</h2>
              <div className="meta">
                <Category id={e.category} />
                {e.tags.map((t) => <Chip key={t}>{t}</Chip>)}
                {redditEvidence(e.evidence_urls).subreddits.map((r) => <Chip key={r} tone="reddit">{r}</Chip>)}
                <Status value={e.status} />
                {e.total_score !== null && <span className="muted small">score {e.total_score.toFixed(2)}</span>}
              </div>
              {e.suggested_angle && (
                <p className="angle"><span className="angle-label">Angle</span> {e.suggested_angle}</p>
              )}
              {e.status === "rejected" && e.rejection_reason && (
                <p className="muted small">Rejected: {e.rejection_reason}</p>
              )}
              {e.post_ref && <p className="muted small">Post: {e.post_ref}</p>}
              <Evidence urls={e.evidence_urls} />
            </div>
          </li>
        ))}
      </ol>

      {!feed.error && <Freshness fetchedAt={feed.fetchedAt} stale={feed.stale} />}
    </>
  );
}
