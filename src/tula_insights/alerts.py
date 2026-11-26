"""Failure alerts: a failed run opens a GitHub issue, a fully successful run closes it.

The pipeline's last job, `alert`, runs after everything else, whatever happened. It
reads every job's result from the `needs` context and:

* **any job failed or was cancelled** → comments on the open `pipeline-failure` issue,
  or opens one if there is none. One issue collects a whole outage, however long.
* **every job succeeded, in a full run** → comments on the open issue and closes it.
  A full run is the scheduled one, or a manual run that writes (not a dry run) with
  every stage, source and category and nothing broken on purpose. A successful dry run
  or a single-stage run proves too little to close an outage.
* otherwise → nothing.

**Public repository.** The issue quotes job names and the error annotations GitHub
already shows on the run page, scrubbed and cut short. Never collected text: the
pipeline's own error messages carry source names, counts and API errors only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import AlertSettings
from .github_api import GitHubClient, RunInfo
from .utils.logging import get_logger
from .utils.pii import scrub

log = get_logger(__name__)

ISSUE_TITLE = "Pipeline failing"
LABEL_COLOUR = "d73a4a"
LABEL_DESCRIPTION = "Opened and closed automatically by the pipeline's alert job"

#: Job results, from the `needs` context, that count as a failure.
FAILED_RESULTS = frozenset({"failure", "cancelled"})
#: Job conclusions, from the REST API, that count as a failure.
FAILED_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out", "startup_failure"})

#: The runner's generic last line. Dropped when a job has a more useful one.
GENERIC_ANNOTATION = "Process completed with exit code"


@dataclass
class RunOutcome:
    """What the alert job needs to know about this run."""

    results: dict[str, str]
    event: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, needs_json: str, *, event: str = "", inputs_json: str = "") -> RunOutcome:
        needs = json.loads(needs_json or "{}") or {}
        inputs = json.loads(inputs_json or "{}") or {}
        results = {name: str((job or {}).get("result", "")) for name, job in needs.items()}
        return cls(results=results, event=event, inputs=inputs)

    @property
    def failed_jobs(self) -> list[str]:
        return sorted(n for n, r in self.results.items() if r in FAILED_RESULTS)

    @property
    def failed(self) -> bool:
        return bool(self.failed_jobs)

    @property
    def is_full_run(self) -> bool:
        """A run complete enough that its success means the outage is over."""
        if self.event == "schedule":
            return True
        if self.event != "workflow_dispatch":
            return False

        def value(key: str, default: Any) -> str:
            return str(self.inputs.get(key, default)).strip().lower()

        return (
            value("dry_run", True) == "false"
            and value("only_stage", "all") == "all"
            and value("source", "all") == "all"
            and value("category", "all") == "all"
            and value("break_source", "none") == "none"
        )

    @property
    def fully_successful(self) -> bool:
        return (
            bool(self.results)
            and all(r == "success" for r in self.results.values())
            and self.is_full_run
        )


@dataclass
class AlertResult:
    action: str  # "opened", "commented", "closed", "none"
    issue_number: int | None = None
    failed_jobs: list[str] = field(default_factory=list)


def failure_details(
    client: GitHubClient, run_id: str, settings: AlertSettings
) -> list[tuple[str, str, list[str]]]:
    """(job name, conclusion, short error lines) for each failed job of the run."""
    details: list[tuple[str, str, list[str]]] = []
    for job in client.run_jobs(run_id):
        conclusion = str(job.get("conclusion") or "")
        if conclusion not in FAILED_CONCLUSIONS:
            continue
        lines: list[str] = []
        try:
            annotations = client.annotations(str(job.get("check_run_url") or ""))
        except Exception as error:  # details are a nicety; the alert must still go out
            log.warning("alerts.annotations_unreadable", extra={"error_type": type(error).__name__})
            annotations = []
        for note in annotations:
            if note.get("annotation_level") != "failure":
                continue
            message = " ".join(scrub(str(note.get("message") or "")).split())
            title = " ".join(str(note.get("title") or "").split())
            if message and title and not message.startswith(title):
                message = f"{title}: {message}"
            if message:
                lines.append(_shorten(message, settings.max_line_chars))
        specific = [line for line in lines if not line.startswith(GENERIC_ANNOTATION)]
        lines = (specific or lines)[: settings.max_lines_per_job]
        failed_step = next(
            (s.get("name", "") for s in job.get("steps", []) or []
             if s.get("conclusion") in FAILED_CONCLUSIONS),
            "",
        )
        name = str(job.get("name") or "job")
        if failed_step:
            name = f"{name} — step “{failed_step}”"
        details.append((name, conclusion, lines))
    return details


def failure_body(
    run: RunInfo,
    outcome: RunOutcome,
    details: list[tuple[str, str, list[str]]],
    *,
    first: bool,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    kind = "Scheduled run" if outcome.event == "schedule" else "Manual run"
    dry = str(outcome.inputs.get("dry_run", "")).lower() == "true"
    lead = "The pipeline failed." if first else "The pipeline failed again."
    parts = [
        lead,
        "",
        f"**Run:** {run.run_url}",
        f"**When:** {now:%Y-%m-%d %H:%M} UTC · {kind}{' (dry run)' if dry else ''}",
        "",
        "**Failed jobs**",
    ]
    if details:
        for name, conclusion, lines in details:
            parts.append(f"- {name} — {conclusion}")
            parts += [f"  - `{line.replace('`', '')}`" for line in lines]
    else:
        parts += [f"- {name}" for name in outcome.failed_jobs]
    if first:
        parts += [
            "",
            "Job names and short error lines only — this repository is public, so no "
            "collected text is posted here. Open the run for the full logs.",
            "",
            "This issue closes itself after the next fully successful run.",
        ]
    return "\n".join(parts)


def handle(
    client: GitHubClient,
    run: RunInfo,
    outcome: RunOutcome,
    settings: AlertSettings,
    *,
    now: datetime | None = None,
) -> AlertResult:
    """Open, comment on, or close the failure issue as this run's outcome requires."""
    if outcome.failed:
        details = failure_details(client, run.run_id, settings) if run.run_id else []
        open_issues = client.open_issues_with_label(settings.label)
        if open_issues:
            number = int(open_issues[0]["number"])
            client.comment(number, failure_body(run, outcome, details, first=False, now=now))
            log.info("alerts.commented", extra={"issue": number})
            return AlertResult("commented", number, outcome.failed_jobs)
        client.ensure_label(settings.label, color=LABEL_COLOUR, description=LABEL_DESCRIPTION)
        issue = client.create_issue(
            ISSUE_TITLE,
            failure_body(run, outcome, details, first=True, now=now),
            [settings.label],
        )
        number = int(issue["number"])
        log.info("alerts.opened", extra={"issue": number})
        return AlertResult("opened", number, outcome.failed_jobs)

    if outcome.fully_successful:
        open_issues = client.open_issues_with_label(settings.label)
        if not open_issues:
            return AlertResult("none")
        for issue in open_issues:
            number = int(issue["number"])
            client.comment(
                number,
                f"Fully successful run: {run.run_url}\n\nEvery job passed, so this is "
                f"closed. A new failure opens a new issue.",
            )
            client.close_issue(number)
            log.info("alerts.closed", extra={"issue": number})
        return AlertResult("closed", int(open_issues[0]["number"]))

    return AlertResult("none")


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
