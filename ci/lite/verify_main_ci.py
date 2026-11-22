"""Verify the public GitHub Actions gate for one exact Perlimen main push."""

import argparse
import json
import re
import sys
from typing import Any
from urllib.request import ProxyHandler, Request, build_opener

REPOSITORY = "fbialogrecki/perlimen"
ACTIONS = f"https://api.github.com/repos/{REPOSITORY}/actions"


def fetch(path: str) -> dict[str, Any]:
    request = Request(
        f"{ACTIONS}/{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "perlimen-lite-ci",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
        payload: dict[str, Any] = json.load(response)
    return payload


def verify(sha: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("expected a full lowercase commit SHA")
    result = fetch(f"workflows/ci.yml/runs?event=push&branch=main&head_sha={sha}&per_page=100")
    runs = result["workflow_runs"]
    if not runs or len(runs) != result["total_count"]:
        raise ValueError("no complete list of CI runs for this main push")
    run = max(runs, key=lambda item: item["run_number"])
    if (
        run["head_sha"] != sha
        or run["path"].removesuffix("@main") != ".github/workflows/ci.yml"
        or run["event"] != "push"
        or run["head_branch"] != "main"
        or run["repository"]["full_name"] != REPOSITORY
        or run["head_repository"]["full_name"] != REPOSITORY
        or run["status"] != "completed"
        or run["conclusion"] != "success"
    ):
        raise ValueError("newest CI run is not a successful push of this SHA to main")
    run_id, attempt = run["id"], run["run_attempt"]
    if type(run_id) is not int or type(attempt) is not int or min(run_id, attempt) < 1:
        raise ValueError("invalid CI run or attempt ID")
    result = fetch(f"runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    jobs = result["jobs"]
    gates = [job for job in jobs if job["name"] == "ci-ok"]
    if len(jobs) != result["total_count"] or len(gates) != 1:
        raise ValueError("expected one ci-ok in the current run attempt")
    gate = gates[0]
    if (
        gate["head_sha"] != sha
        or gate["head_branch"] != "main"
        or gate["run_id"] != run_id
        or gate["run_attempt"] != attempt
        or gate["status"] != "completed"
        or gate["conclusion"] != "success"
    ):
        raise ValueError("ci-ok did not pass on this SHA in the current run attempt")
    return run_id, attempt, gate["id"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sha", help="full lowercase commit SHA")
    sha = parser.parse_args().sha
    try:
        run, attempt, job = verify(sha)
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(f"Lite CI gate rejected: {error}", file=sys.stderr)
        return 1
    print(f"ci-ok passed: sha={sha} run={run} attempt={attempt} job={job}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
