import { Category, Chip, Evidence, Freshness, Problem, Status } from "@/components/bits.tsx";
import { loadInsights } from "@/lib/data.ts";
import { formatSast } from "@/lib/dates.ts";
import { type Insight, label } from "@/lib/model.ts";

export const metadata = { title: "Insights · Tula Insights" };

const SORTS: Record<string, { label: string; key: (i: Insight) => number | string }> = {
  score: { label: "Score", key: (i) => i.total_score ?? -1 },
  recent: { label: "Most recently seen", key: (i) => i.last_seen },
  items: { label: "Most mentions", key: (i) => i.item_count },
};
const SHOW = 200;

export default async function InsightsPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const params = await searchParams;
  const pick = (k: string) => (typeof params[k] === "string" ? (params[k] as string).trim() : "");
  const q = pick("q");
  const category = pick("category");
  const tag = pick("tag");
  const status = pick("status") || "active";
  const sort = SORTS[pick("sort")] ? pick("sort") : "score";

  const data = await loadInsights();
  const all = data.items.filter((i) => i.insight_id && i.canonical_question);
  const categories = [...new Set(all.map((i) => i.category).filter(Boolean))].sort();
  const tags = [...new Set(all.flatMap((i) => i.tags))].sort();

  const needle = q.toLowerCase();
  const matches = all
    .filter((i) => status === "all" || i.status === status)
    .filter((i) => !category || i.category === category)
    .filter((i) => !tag || i.tags.includes(tag))
    .filter((i) => !needle || i.canonical_question.toLowerCase().includes(needle))
    .sort((a, b) => {
      const ka = SORTS[sort].key(a);
      const kb = SORTS[sort].key(b);
      return typeof ka === "number" && typeof kb === "number" ? kb - ka : String(kb).localeCompare(String(ka));
    });

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Insights</h1>
          <p className="muted">Every distinct question found so far, merged across sources.</p>
        </div>
      </div>

      <form className="card filters" method="get">
        <input type="search" name="q" defaultValue={q} placeholder="Search questions" aria-label="Search questions" />
        <select name="category" defaultValue={category} aria-label="Category">
          <option value="">All categories</option>
          {categories.map((c) => <option key={c} value={c}>{label(c)}</option>)}
        </select>
        <select name="tag" defaultValue={tag} aria-label="Tag">
          <option value="">All tags</option>
          {tags.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <select name="status" defaultValue={status} aria-label="Status">
          <option value="active">Active</option>
          <option value="retired">Retired</option>
          <option value="all">Any status</option>
        </select>
        <select name="sort" defaultValue={sort} aria-label="Sort by">
          {Object.entries(SORTS).map(([k, s]) => <option key={k} value={k}>{s.label}</option>)}
        </select>
        <button type="submit">Apply</button>
      </form>

      {data.error && <Problem message={data.error} />}
      {!data.error && (
        <p className="muted small">
          {matches.length} of {all.length} insights{matches.length > SHOW ? ` — showing the first ${SHOW}` : ""}.
        </p>
      )}

      <div className="table-wrap card">
        <table>
          <thead>
            <tr>
              <th>Question</th>
              <th>Category</th>
              <th className="num">Score</th>
              <th className="num">Mentions</th>
              <th>Sources</th>
              <th>Last seen</th>
              <th>Served</th>
            </tr>
          </thead>
          <tbody>
            {matches.slice(0, SHOW).map((i) => (
              <tr key={i.insight_id}>
                <td className="question-cell">
                  <div>{i.canonical_question}</div>
                  <div className="meta">
                    {i.tags.map((t) => <Chip key={t}>{t}</Chip>)}
                    {i.status !== "active" && <Status value={i.status} />}
                  </div>
                  <Evidence urls={i.evidence_urls} limit={2} />
                </td>
                <td><Category id={i.category} /></td>
                <td className="num">{i.total_score === null ? "—" : i.total_score.toFixed(2)}</td>
                <td className="num">{i.item_count}</td>
                <td className="small">{i.sources.join(", ") || "—"}</td>
                <td className="small">{formatSast(i.last_seen)}</td>
                <td className="small">{i.times_served ? `${i.times_served}× · ${formatSast(i.last_served_at)}` : "Not yet"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {!data.error && <Freshness fetchedAt={data.fetchedAt} stale={data.stale} />}
    </>
  );
}
