import { Category, Chip, Freshness, Problem } from "@/components/bits.tsx";
import { loadInsights } from "@/lib/data.ts";
import { formatSast } from "@/lib/dates.ts";
import { redditEvidence } from "@/lib/model.ts";

export const metadata = { title: "Reddit · Tula Insights" };

export default async function RedditPage() {
  const data = await loadInsights();
  const rows = data.items
    .filter((i) => i.insight_id && i.canonical_question)
    .map((i) => ({ insight: i, ...redditEvidence(i.evidence_urls) }))
    .filter((r) => r.threads.length > 0)
    .sort((a, b) => (b.insight.total_score ?? -1) - (a.insight.total_score ?? -1));

  const bySubreddit = new Map<string, number>();
  for (const r of rows) for (const s of r.subreddits) bySubreddit.set(s, (bySubreddit.get(s) ?? 0) + 1);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>From Reddit</h1>
          <p className="muted">
            Questions that came from Reddit threads, best first. They arrive through the team's
            manual submissions, so this grows as threads are added.
          </p>
        </div>
        {!data.error && (
          <div className="card stat">
            <span className="muted small">Insights from Reddit</span>
            <strong>{rows.length}</strong>
          </div>
        )}
      </div>

      {data.error && <Problem message={data.error} />}

      {bySubreddit.size > 0 && (
        <div className="meta" style={{ marginBottom: 12 }}>
          {[...bySubreddit.entries()].sort((a, b) => b[1] - a[1]).map(([s, n]) => (
            <Chip key={s} tone="reddit">{s} · {n}</Chip>
          ))}
        </div>
      )}

      {!data.error && rows.length === 0 && (
        <div className="card empty">
          <p>No Reddit threads yet.</p>
          <p className="muted">
            Add one by pasting its link and question into the manual_submissions tab of the sheet,
            with platform set to reddit. The next run brings it in.
          </p>
        </div>
      )}

      {rows.length > 0 && (
        <div className="table-wrap card">
          <table>
            <thead>
              <tr>
                <th>Question</th>
                <th>Category</th>
                <th className="num">Score</th>
                <th className="num">Mentions</th>
                <th>Thread</th>
                <th>Last seen</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ insight: i, threads, subreddits }) => (
                <tr key={i.insight_id}>
                  <td className="question-cell">
                    <div>{i.canonical_question}</div>
                    <div className="meta">{i.tags.map((t) => <Chip key={t}>{t}</Chip>)}</div>
                  </td>
                  <td><Category id={i.category} /></td>
                  <td className="num">{i.total_score === null ? "—" : i.total_score.toFixed(2)}</td>
                  <td className="num">{i.item_count}</td>
                  <td className="small">
                    {threads.slice(0, 2).map((url, n) => (
                      <div key={url}>
                        <a href={url} target="_blank" rel="noopener noreferrer nofollow">
                          {subreddits[0] ?? "reddit"} thread{threads.length > 1 ? ` ${n + 1}` : ""}
                        </a>
                      </div>
                    ))}
                  </td>
                  <td className="small">{formatSast(i.last_seen)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!data.error && <Freshness fetchedAt={data.fetchedAt} stale={data.stale} />}
    </>
  );
}
