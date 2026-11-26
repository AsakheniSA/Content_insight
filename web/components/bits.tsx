import { formatSast, hoursSince, STALE_RUN_HOURS } from "@/lib/dates.ts";
import { hostOf, label, newestFirst, type Run } from "@/lib/model.ts";

export function Problem({ message }: { message: string }) {
  return (
    <div className="banner banner-error" role="alert">
      <strong>Could not load the sheet.</strong> {message}
    </div>
  );
}

export function Freshness({ fetchedAt, stale }: { fetchedAt: number; stale: boolean }) {
  return (
    <p className="muted small">
      {stale ? "Showing an older copy — the sheet could not be refreshed. " : ""}
      Read from the sheet {formatSast(new Date(fetchedAt).toISOString())}; refreshes every 5 minutes.
    </p>
  );
}

export function Chip({ children, tone = "plain" }: { children: React.ReactNode; tone?: string }) {
  return <span className={`chip chip-${tone}`}>{children}</span>;
}

export function Category({ id }: { id: string }) {
  return <Chip tone="violet">{label(id)}</Chip>;
}

const STATUS_TONE: Record<string, string> = {
  served: "plain", used: "good", rejected: "bad",
  success: "good", partial: "warn", failed: "bad", running: "plain",
  active: "good", retired: "plain",
};

export function Status({ value }: { value: string }) {
  return <Chip tone={STATUS_TONE[value] ?? "plain"}>{value || "—"}</Chip>;
}

export function Evidence({ urls, limit = 3 }: { urls: string[]; limit?: number }) {
  if (!urls.length) return <span className="muted small">No links kept</span>;
  return (
    <ul className="evidence">
      {urls.slice(0, limit).map((url) => (
        <li key={url}>
          <a href={url} target="_blank" rel="noopener noreferrer nofollow">{hostOf(url)}</a>
        </li>
      ))}
    </ul>
  );
}

/** Warns when the daily pipeline hasn't run lately — the sign the schedule stopped. */
export function LastRun({ runs }: { runs: Run[] }) {
  const latest = newestFirst(runs, (r) => r.started_at)[0];
  if (!latest) return null;
  const hours = hoursSince(latest.started_at);
  const late = hours !== null && hours > STALE_RUN_HOURS;
  return (
    <div className={`banner ${late ? "banner-warn" : "banner-info"}`}>
      Last pipeline run: <strong>{formatSast(latest.started_at)}</strong> · <Status value={latest.status} />
      {late && (
        <>
          {" "}No run for over {STALE_RUN_HOURS} hours. GitHub may have switched the schedule off: in the
          repository, open Actions → Pipeline and choose <em>Enable workflow</em>.
        </>
      )}
    </div>
  );
}
