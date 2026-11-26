"""Failure issues, the keep-alive, and the GitHub REST client underneath them."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from tula_insights import alerts, keepalive
from tula_insights.cli import main
from tula_insights.config import load_config
from tula_insights.github_api import GitHubClient, GitHubError, RunInfo

NOW = datetime(2026, 11, 26, 3, 40, tzinfo=UTC)
RUN = RunInfo(repository="owner/repo", run_id="555", run_attempt="1")


@pytest.fixture
def settings(config_dir):
    return load_config().settings


class FakeGitHub:
    """Stands in for GitHubClient and records what it was asked to do."""

    def __init__(self, *, open_issues=(), jobs=(), annotations=None, workflows=None,
                 last_commit=NOW - timedelta(days=3)):
        self.open = [dict(i) for i in open_issues]
        self.jobs = list(jobs)
        self.notes = annotations or {}
        self.workflows = workflows or {"pipeline.yml": "active", "keepalive.yml": "active"}
        self.last_commit = last_commit
        self.calls: list[tuple] = []

    def run_jobs(self, run_id):
        return self.jobs

    def annotations(self, url):
        return self.notes.get(url, [])

    def open_issues_with_label(self, label):
        return self.open

    def ensure_label(self, name, *, color, description):
        self.calls.append(("label", name))
        return True

    def create_issue(self, title, body, labels):
        self.calls.append(("create", title, body, labels))
        return {"number": 7}

    def comment(self, number, body):
        self.calls.append(("comment", number, body))

    def close_issue(self, number):
        self.calls.append(("close", number))

    def workflow(self, name):
        return {"state": self.workflows[name]}

    def enable_workflow(self, name):
        self.calls.append(("enable", name))

    def default_branch(self):
        return "main"

    def latest_commit_date(self, branch):
        return self.last_commit.strftime("%Y-%m-%dT%H:%M:%SZ")

    def put_file(self, path, text, *, message, branch):
        self.calls.append(("put", path, text, message, branch))
        return "abc123"

    def kinds(self):
        return [c[0] for c in self.calls]


NEEDS_OK = {name: {"result": "success", "outputs": {}}
            for name in ("plan", "collect", "classify", "score", "publish", "report")}


def needs(**overrides):
    data = json.loads(json.dumps(NEEDS_OK))
    for name, result in overrides.items():
        data[name]["result"] = result
    return json.dumps(data)


FULL_DISPATCH = json.dumps({"source": "all", "category": "all", "only_stage": "all",
                            "sync_mode": False, "dry_run": False, "break_source": "none"})

FAILED_JOB = {
    "name": "Collect (youtube)", "conclusion": "failure", "check_run_url": "cr/1",
    "steps": [{"name": "Install", "conclusion": "success"},
              {"name": "Collect", "conclusion": "failure"}],
}
NOTES = {"cr/1": [
    {"annotation_level": "failure", "title": "Collect failed (youtube)",
     "message": "API key not valid"},
    {"annotation_level": "failure", "message": "Process completed with exit code 1."},
    {"annotation_level": "warning", "message": "Budget: nearly there"},
]}


# --- deciding what happened ----------------------------------------------------------


def test_a_failed_or_cancelled_job_is_a_failure():
    outcome = alerts.RunOutcome.from_json(needs(collect="failure", score="cancelled"),
                                          event="schedule")
    assert outcome.failed and outcome.failed_jobs == ["collect", "score"]


def test_only_a_full_real_run_counts_as_fully_successful():
    assert alerts.RunOutcome.from_json(needs(), event="schedule").fully_successful
    assert alerts.RunOutcome.from_json(needs(), event="workflow_dispatch",
                                       inputs_json=FULL_DISPATCH).fully_successful
    for change in ({"dry_run": True}, {"only_stage": "score"}, {"source": "youtube"},
                   {"break_source": "news_rss"}, {"category": "gear_packing"}):
        inputs = {**json.loads(FULL_DISPATCH), **change}
        outcome = alerts.RunOutcome.from_json(needs(), event="workflow_dispatch",
                                              inputs_json=json.dumps(inputs))
        assert not outcome.fully_successful, change


def test_a_skipped_job_means_not_fully_successful():
    assert not alerts.RunOutcome.from_json(needs(collect="skipped"),
                                           event="schedule").fully_successful


# --- the issue -----------------------------------------------------------------------


def test_the_first_failure_opens_a_labelled_issue_with_the_details(settings):
    gh = FakeGitHub(jobs=[FAILED_JOB, {"name": "Score", "conclusion": "success"}],
                    annotations=NOTES)
    outcome = alerts.RunOutcome.from_json(needs(collect="failure"), event="schedule")
    result = alerts.handle(gh, RUN, outcome, settings.alerts, now=NOW)
    assert result.action == "opened" and result.issue_number == 7
    assert gh.kinds() == ["label", "create"]
    _, title, body, labels = gh.calls[1]
    assert title == "Pipeline failing" and labels == ["pipeline-failure"]
    assert "https://github.com/owner/repo/actions/runs/555" in body
    assert "Collect (youtube) — step “Collect” — failure" in body
    assert "Collect failed (youtube): API key not valid" in body
    assert "exit code 1" not in body, "the generic line is dropped when a better one exists"
    assert "nearly there" not in body, "warnings are not failures"
    assert "closes itself" in body


def test_a_later_failure_comments_on_the_open_issue(settings):
    gh = FakeGitHub(open_issues=[{"number": 3}], jobs=[FAILED_JOB], annotations=NOTES)
    outcome = alerts.RunOutcome.from_json(needs(collect="failure"), event="schedule")
    result = alerts.handle(gh, RUN, outcome, settings.alerts, now=NOW)
    assert result.action == "commented" and gh.kinds() == ["comment"]
    assert gh.calls[0][1] == 3 and "failed again" in gh.calls[0][2]


def test_a_fully_successful_run_comments_and_closes(settings):
    gh = FakeGitHub(open_issues=[{"number": 3}])
    result = alerts.handle(gh, RUN, alerts.RunOutcome.from_json(needs(), event="schedule"),
                           settings.alerts)
    assert result.action == "closed"
    assert gh.kinds() == ["comment", "close"]
    assert "actions/runs/555" in gh.calls[0][2]


def test_a_successful_dry_run_leaves_the_issue_open(settings):
    gh = FakeGitHub(open_issues=[{"number": 3}])
    inputs = json.dumps({**json.loads(FULL_DISPATCH), "dry_run": True})
    outcome = alerts.RunOutcome.from_json(needs(), event="workflow_dispatch", inputs_json=inputs)
    assert alerts.handle(gh, RUN, outcome, settings.alerts).action == "none"
    assert gh.calls == []


def test_success_with_no_open_issue_does_nothing(settings):
    gh = FakeGitHub()
    outcome = alerts.RunOutcome.from_json(needs(), event="schedule")
    assert alerts.handle(gh, RUN, outcome, settings.alerts).action == "none"


def test_error_lines_are_scrubbed_and_capped(settings):
    long_notes = {"cr/1": [{"annotation_level": "failure",
                            "message": f"error {n}: contact x@y.com " + "w" * 500}
                           for n in range(9)]}
    gh = FakeGitHub(jobs=[FAILED_JOB], annotations=long_notes)
    details = alerts.failure_details(gh, "555", settings.alerts)
    lines = details[0][2]
    assert len(lines) == settings.alerts.max_lines_per_job
    assert all(len(line) <= settings.alerts.max_line_chars for line in lines)
    assert not any("x@y.com" in line for line in lines)


def test_the_alert_command_reads_the_actions_context(config_dir, monkeypatch, capsys):
    gh = FakeGitHub(jobs=[FAILED_JOB], annotations=NOTES)
    monkeypatch.setattr(GitHubClient, "from_env", classmethod(lambda cls: gh))
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "555")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("NEEDS_JSON", needs(collect="failure"))
    monkeypatch.setenv("INPUTS_JSON", json.dumps({**json.loads(FULL_DISPATCH),
                                                  "break_source": "youtube", "dry_run": True}))
    assert main(["alert"]) == 0
    out = capsys.readouterr().out
    assert "Opened issue #7" in out and "Failed jobs: collect" in out
    assert "(dry run)" in gh.calls[1][2]


# --- keep-alive ----------------------------------------------------------------------


def test_keepalive_enables_active_workflows_and_does_not_commit_when_busy(settings):
    gh = FakeGitHub(last_commit=NOW - timedelta(days=10))
    result = keepalive.run(gh, settings.keepalive, now=NOW)
    assert gh.kinds() == ["enable", "enable"]
    assert result.days_since_commit == 10 and result.committed == ""


def test_keepalive_commits_the_marker_after_a_quiet_spell(settings):
    gh = FakeGitHub(last_commit=NOW - timedelta(days=45))
    result = keepalive.run(gh, settings.keepalive, now=NOW)
    assert result.committed == "abc123"
    put = next(c for c in gh.calls if c[0] == "put")
    assert put[1] == ".github/keepalive" and put[4] == "main"
    assert put[2].startswith("2026-11-26\n") and "45 days" in put[3]


def test_keepalive_leaves_a_hand_disabled_workflow_alone(settings):
    gh = FakeGitHub(workflows={"pipeline.yml": "disabled_manually",
                               "keepalive.yml": "disabled_inactivity"})
    result = keepalive.run(gh, settings.keepalive, now=NOW)
    assert ("enable", "pipeline.yml") not in gh.calls
    assert ("enable", "keepalive.yml") in gh.calls
    assert result.left_alone == ["pipeline.yml (disabled_manually)"]


def test_keepalive_dry_run_changes_nothing(settings):
    gh = FakeGitHub(last_commit=NOW - timedelta(days=50))
    result = keepalive.run(gh, settings.keepalive, now=NOW, dry_run=True)
    assert gh.calls == [] and "dry run" in result.committed
    assert "dry run" in keepalive.summary_lines(result, settings.keepalive)[0]


def test_quiet_days_must_stay_well_under_sixty(config_dir):
    path = config_dir / "settings.yaml"
    path.write_text(path.read_text().replace("quiet_days: 45", "quiet_days: 59"))
    from tula_insights.config import ConfigError

    with pytest.raises(ConfigError):
        load_config()


# --- the REST client -----------------------------------------------------------------


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.payload = payload
        self.headers = {}

    def json(self):
        if self.payload is None:
            raise ValueError("no body")
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []
        self.headers = {}

    def request(self, method, url, timeout=None, **kwargs):
        self.sent.append((method, url, kwargs))
        return self.responses.pop(0)


def client(*responses):
    session = FakeSession(responses)
    return GitHubClient("tok", "owner/repo", session=session), session


def test_the_client_sends_the_documented_headers():
    gh, session = client(FakeResponse(200, []))
    gh.open_issues_with_label("pipeline-failure")
    assert session.headers["Authorization"] == "Bearer tok"
    assert session.headers["Accept"] == "application/vnd.github+json"
    assert session.headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert "tula" not in session.headers["User-Agent"].lower()
    method, url, kwargs = session.sent[0]
    assert (method, url) == ("GET", "https://api.github.com/repos/owner/repo/issues")
    assert kwargs["params"]["labels"] == "pipeline-failure"


def test_pull_requests_are_not_mistaken_for_issues():
    gh, _ = client(FakeResponse(200, [{"number": 1, "pull_request": {}}, {"number": 2}]))
    assert [i["number"] for i in gh.open_issues_with_label("x")] == [2]


def test_a_missing_label_is_created():
    gh, session = client(FakeResponse(404, {"message": "Not Found"}), FakeResponse(201, {}))
    assert gh.ensure_label("pipeline-failure", color="d73a4a", description="d") is True
    assert session.sent[1][0] == "POST" and session.sent[1][2]["json"]["name"] == "pipeline-failure"


def test_closing_sets_the_reason():
    gh, session = client(FakeResponse(200, {}))
    gh.close_issue(4)
    method, url, kwargs = session.sent[0]
    assert method == "PATCH" and url.endswith("/issues/4")
    assert kwargs["json"] == {"state": "closed", "state_reason": "completed"}


def test_enabling_a_workflow_expects_no_content():
    gh, session = client(FakeResponse(204))
    gh.enable_workflow("pipeline.yml")
    assert session.sent[0][:2] == (
        "PUT", "https://api.github.com/repos/owner/repo/actions/workflows/pipeline.yml/enable")


def test_put_file_updates_an_existing_file_with_its_sha():
    gh, session = client(FakeResponse(200, {"sha": "old"}),
                         FakeResponse(200, {"commit": {"sha": "new"}}))
    assert gh.put_file(".github/keepalive", "2026-11-26\n", message="m", branch="main") == "new"
    body = session.sent[1][2]["json"]
    assert body["sha"] == "old" and body["branch"] == "main"
    assert base64.b64decode(body["content"]).decode() == "2026-11-26\n"


def test_errors_name_the_status_but_not_the_token(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    gh, _ = client(FakeResponse(403, {"message": "Resource not accessible by integration"}))
    with pytest.raises(GitHubError) as caught:
        gh.create_issue("t", "b", [])
    assert caught.value.status == 403 and "Resource not accessible" in str(caught.value)
    assert "tok" not in str(caught.value)


def test_server_errors_are_retried(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    gh, session = client(FakeResponse(502), FakeResponse(200, []))
    assert gh.open_issues_with_label("x") == []
    assert len(session.sent) == 2


def test_run_url_includes_a_rerun_attempt():
    assert RunInfo("o/r", "9", "2").run_url == "https://github.com/o/r/actions/runs/9/attempts/2"
    assert RunInfo("o/r", "9", "1").run_url == "https://github.com/o/r/actions/runs/9"
