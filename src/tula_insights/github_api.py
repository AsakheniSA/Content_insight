"""A small client for the GitHub REST API, for the alert and keep-alive jobs.

Only the handful of endpoints those jobs need, each documented in GitHub's REST
reference. It authenticates with the workflow's own `GITHUB_TOKEN`, so what it may do
is exactly what the job's `permissions:` block grants — nothing more.

Reads the standard variables every Actions job has: `GITHUB_API_URL`,
`GITHUB_REPOSITORY`, `GITHUB_SERVER_URL`, `GITHUB_RUN_ID` and `GITHUB_RUN_ATTEMPT`.
"""

from __future__ import annotations

import base64
import contextlib
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from .utils.logging import get_logger
from .utils.retry import call_with_retry

log = get_logger(__name__)

API_VERSION = "2022-11-28"
#: Generic on purpose: nothing about the project or its owner goes in request headers.
USER_AGENT = "pipeline-ops/0.1"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _Transient(Exception):
    def __init__(self, response: requests.Response) -> None:
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


@dataclass
class RunInfo:
    """Where this job runs, from the standard Actions variables."""

    repository: str
    run_id: str
    run_attempt: str = "1"
    server_url: str = "https://github.com"

    @classmethod
    def from_env(cls) -> RunInfo:
        repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if not repository:
            raise GitHubError("GITHUB_REPOSITORY is not set: this only runs inside Actions.")
        return cls(
            repository=repository,
            run_id=os.environ.get("GITHUB_RUN_ID", "").strip(),
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "1").strip() or "1",
            server_url=os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/"),
        )

    @property
    def run_url(self) -> str:
        url = f"{self.server_url}/{self.repository}/actions/runs/{self.run_id}"
        return url if self.run_attempt in ("", "1") else f"{url}/attempts/{self.run_attempt}"


class GitHubClient:
    def __init__(
        self,
        token: str,
        repository: str,
        *,
        api_url: str = "https://api.github.com",
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not token:
            raise GitHubError("GITHUB_TOKEN is not set.")
        self.repository = repository
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        })

    @classmethod
    def from_env(cls) -> GitHubClient:
        info = RunInfo.from_env()
        return cls(
            os.environ.get("GITHUB_TOKEN", "").strip(),
            info.repository,
            api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        )

    # --- plumbing --------------------------------------------------------------------

    def _request(
        self, method: str, path: str, *, ok: tuple[int, ...] = (200,), **kwargs: Any
    ) -> requests.Response:
        url = path if path.startswith("http") else f"{self.api_url}{path}"

        def attempt() -> requests.Response:
            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            if response.status_code in RETRYABLE_STATUS:
                raise _Transient(response)
            return response

        try:
            response = call_with_retry(
                attempt, what=f"github {method}", attempts=4, retry_on=(_Transient,)
            )
        except _Transient as error:
            response = error.response
        if response.status_code not in ok:
            message = ""
            with contextlib.suppress(ValueError, AttributeError):
                message = str(response.json().get("message", ""))[:200]
            raise GitHubError(
                f"GitHub API {method} {path.split('?')[0]} returned HTTP "
                f"{response.status_code}{': ' + message if message else ''}",
                status=response.status_code,
            )
        return response

    def _repo(self, suffix: str = "") -> str:
        return f"/repos/{self.repository}{suffix}"

    # --- runs ------------------------------------------------------------------------

    def run_jobs(self, run_id: str) -> list[dict[str, Any]]:
        """Jobs of a workflow run, latest attempt only."""
        jobs: list[dict[str, Any]] = []
        for page in range(1, 6):
            data = self._request(
                "GET", self._repo(f"/actions/runs/{run_id}/jobs"),
                params={"filter": "latest", "per_page": 100, "page": page},
            ).json()
            batch = data.get("jobs", [])
            jobs.extend(batch)
            if len(batch) < 100:
                break
        return jobs

    def annotations(self, check_run_url: str) -> list[dict[str, Any]]:
        """Annotations (the `::error::` lines and runner errors) on one job."""
        if not check_run_url:
            return []
        return list(self._request(
            "GET", f"{check_run_url}/annotations", params={"per_page": 50}
        ).json())

    # --- issues ----------------------------------------------------------------------

    def open_issues_with_label(self, label: str) -> list[dict[str, Any]]:
        issues = self._request(
            "GET", self._repo("/issues"),
            params={"labels": label, "state": "open", "per_page": 20,
                    "sort": "created", "direction": "asc"},
        ).json()
        # The issues endpoint also returns pull requests.
        return [i for i in issues if "pull_request" not in i]

    def ensure_label(self, name: str, *, color: str, description: str) -> bool:
        """Create the label if it is missing. Returns True if it was created."""
        try:
            self._request("GET", self._repo(f"/labels/{quote(name, safe='')}"))
            return False
        except GitHubError as error:
            if error.status != 404:
                raise
        self._request(
            "POST", self._repo("/labels"), ok=(201,),
            json={"name": name, "color": color, "description": description},
        )
        return True

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        return self._request(
            "POST", self._repo("/issues"), ok=(201,),
            json={"title": title, "body": body, "labels": labels},
        ).json()

    def comment(self, number: int, body: str) -> None:
        self._request(
            "POST", self._repo(f"/issues/{number}/comments"), ok=(201,), json={"body": body}
        )

    def close_issue(self, number: int) -> None:
        self._request(
            "PATCH", self._repo(f"/issues/{number}"),
            json={"state": "closed", "state_reason": "completed"},
        )

    # --- workflows, commits, contents ------------------------------------------------

    def workflow(self, workflow_file: str) -> dict[str, Any]:
        return self._request(
            "GET", self._repo(f"/actions/workflows/{quote(workflow_file, safe='')}")
        ).json()

    def enable_workflow(self, workflow_file: str) -> None:
        self._request(
            "PUT", self._repo(f"/actions/workflows/{quote(workflow_file, safe='')}/enable"),
            ok=(204,),
        )

    def default_branch(self) -> str:
        return str(self._request("GET", self._repo()).json()["default_branch"])

    def latest_commit_date(self, branch: str) -> str:
        """ISO 8601 committer date of the newest commit on `branch`."""
        commits = self._request(
            "GET", self._repo("/commits"), params={"sha": branch, "per_page": 1}
        ).json()
        if not commits:
            raise GitHubError(f"No commits found on {branch!r}.")
        return str(commits[0]["commit"]["committer"]["date"])

    def file_sha(self, path: str, branch: str) -> str | None:
        try:
            data = self._request(
                "GET", self._repo(f"/contents/{quote(path)}"), params={"ref": branch}
            ).json()
        except GitHubError as error:
            if error.status == 404:
                return None
            raise
        return str(data.get("sha") or "") or None

    def put_file(self, path: str, text: str, *, message: str, branch: str) -> str:
        """Create or replace one file with a commit. Returns the new commit's sha."""
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        sha = self.file_sha(path, branch)
        if sha:
            payload["sha"] = sha
        data = self._request(
            "PUT", self._repo(f"/contents/{quote(path)}"), ok=(200, 201), json=payload
        ).json()
        return str(data.get("commit", {}).get("sha", ""))
