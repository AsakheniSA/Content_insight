import { Chip, Freshness, LastRun, Problem, Status } from "@/components/bits.tsx";
import { loadRuns } from "@/lib/data.ts";
import { formatSast, todaySast } from "@/lib/dates.ts";
import { newestFirst } from "@/lib/model.ts";

export const metadata = { title: "Runs · Tula Insights" };
const SHOW = 30;

export default async function RunsPage() {
  const data = await loadRuns();
  const runs = newestFirst(data.items.filter((r) => r.run_id), (r) => r.started_at);
  const month = todaySast().slice(0, 7);
  const monthSpend = runs
    .filter((r) => r.started_at.slice(0, 7) === month)
    .reduce((sum, r) => sum + r.est_cost_usd, 0);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Runs</h1>
          <p className="muted">Recent pipeline runs. Failures and budget warnings are highlighted.</p>
        </div>
        {!data.error && (
          <div className="card stat">
            <span className="muted small">Claude spend this month (estimate)</span>
            <strong>${monthSpend.toFixed(2)}</strong>
          </div>
        )}
      </div>

      {data.error ? <Problem message={data.error} /> : <LastRun runs={runs} />}

      <div className="table-wrap card">
        <table>
          <thead>
            <tr>
              <th>Started</th>
              <th>Trigger</th>
              <th>Status</th>
              <th className="num">Collected</th>
              <th className="num">Classified</th>
              <th className="num">New insights</th>
              <th className="num">Served</th>
              <th className="num">Cost</th>
              <th>Warnings</th>
            </tr>
          </thead>
          <tbody>
            {runs.slice(0, SHOW).map((r) => {
              const tone = r.status === "failed" ? "row-bad" : r.status === "partial" || r.budget_warnings.length ? "row-warn" : "";
              return (
                <tr key={r.run_id} className={tone}>
                  <td className="small">
                    {r.actions_run_url ? (
                      <a href={r.actions_run_url} target="_blank" rel="noopener noreferrer">{formatSast(r.started_at)}</a>
                    ) : formatSast(r.started_at)}
                  </td>
                  <td className="small">{r.trigger || "—"}</td>
                  <td><Status value={r.status} /></td>
                  <td className="num" title={Object.entries(r.items_collected_by_source).map(([k, v]) => `${k}: ${v}`).join("\n")}>
                    {r.items_collected}
                  </td>
                  <td className="num">{r.items_classified}</td>
                  <td className="num">{r.new_insights}</td>
                  <td className="num">{r.served_today}</td>
                  <td className="num">${r.est_cost_usd.toFixed(2)}</td>
                  <td>
                    {r.budget_warnings.length > 0 && <Chip tone="warn">budget cap hit</Chip>}
                    {r.errors.length > 0 ? (
                      <details>
                        <summary>{r.errors.length} note{r.errors.length === 1 ? "" : "s"}</summary>
                        <ul className="errors">{r.errors.map((e, n) => <li key={n}>{e}</li>)}</ul>
                      </details>
                    ) : <span className="muted small">None</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {!data.error && <Freshness fetchedAt={data.fetchedAt} stale={data.stale} />}
    </>
  );
}
