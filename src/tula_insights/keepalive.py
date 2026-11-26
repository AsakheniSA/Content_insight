"""Keep the scheduled pipeline from being switched off by GitHub's 60-day rule.

GitHub's documentation: in a public repository, scheduled workflows are automatically
disabled when no repository activity has occurred in 60 days. It does not say what
counts as activity, and nothing documented says that re-enabling a workflow through the
API resets that clock. So this does two things, weekly:

1. **Re-enable through the API** every listed workflow that is active or was disabled
   for inactivity. Harmless if it changes nothing; it may reset the clock. A workflow
   someone disabled *by hand* is left alone — that was a decision, not an accident.
2. **Commit only when the repository has gone quiet.** If the default branch has had no
   commit for `quiet_days` (45 by default), commit a one-line date to the marker file.
   A commit is the one thing that unambiguously counts as activity. With normal
   development this never fires; on a quiet repository it is at most one small commit
   every 45 days.

If the pipeline is ever disabled anyway: Actions tab → Pipeline → "Enable workflow".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from .config import KeepAliveSettings
from .github_api import GitHubClient
from .utils.logging import get_logger

log = get_logger(__name__)

#: Workflow states the keep-alive re-enables. `disabled_manually` is deliberately absent.
ENABLE_STATES = frozenset({"active", "disabled_inactivity"})


@dataclass
class KeepAliveResult:
    enabled: list[str] = field(default_factory=list)
    left_alone: list[str] = field(default_factory=list)
    branch: str = ""
    days_since_commit: int = 0
    committed: str = ""  # the marker commit's sha, if one was made
    dry_run: bool = False


def parse_github_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def run(
    client: GitHubClient,
    settings: KeepAliveSettings,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> KeepAliveResult:
    now = now or datetime.now(UTC)
    result = KeepAliveResult(dry_run=dry_run)

    for workflow_file in settings.workflows:
        state = str(client.workflow(workflow_file).get("state", ""))
        if state not in ENABLE_STATES:
            result.left_alone.append(f"{workflow_file} ({state or 'unknown state'})")
            log.warning("keepalive.left_alone", extra={"workflow": workflow_file, "state": state})
            continue
        if not dry_run:
            client.enable_workflow(workflow_file)
        result.enabled.append(workflow_file)

    result.branch = client.default_branch()
    last = parse_github_time(client.latest_commit_date(result.branch))
    result.days_since_commit = max(0, (now - last).days)

    if result.days_since_commit >= settings.quiet_days:
        text = (
            f"{now:%Y-%m-%d}\n"
            f"# Written by .github/workflows/keepalive.yml after {result.days_since_commit} "
            f"days without a commit, so GitHub keeps the scheduled pipeline switched on.\n"
        )
        if dry_run:
            result.committed = "(dry run — not committed)"
        else:
            result.committed = client.put_file(
                settings.marker_path, text, branch=result.branch,
                message=(
                    f"Keep the scheduled pipeline enabled "
                    f"({result.days_since_commit} days without a commit)"
                ),
            )
    log.info(
        "keepalive.done",
        extra={
            "enabled": len(result.enabled),
            "left_alone": len(result.left_alone),
            "days_since_commit": result.days_since_commit,
            "committed": bool(result.committed),
        },
    )
    return result


def summary_lines(result: KeepAliveResult, settings: KeepAliveSettings) -> list[str]:
    suffix = " (dry run — nothing changed)" if result.dry_run else ""
    lines = [f"Keep-alive{suffix}"]
    lines.append(f"  re-enabled        {', '.join(result.enabled) or '(none)'}")
    if result.left_alone:
        lines.append(f"  left alone        {', '.join(result.left_alone)}")
    lines.append(
        f"  last commit       {result.days_since_commit} day(s) ago on {result.branch} "
        f"(marker commit after {settings.quiet_days})"
    )
    lines.append(f"  marker commit     {result.committed or 'not needed'}")
    return lines
