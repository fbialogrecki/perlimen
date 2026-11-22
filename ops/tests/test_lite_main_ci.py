import importlib.util
import runpy
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import call, patch

import pytest

LITE_CI = Path(__file__).resolve().parents[2] / "ci/lite"
spec = importlib.util.spec_from_file_location("lite_main_ci", LITE_CI / "verify_main_ci.py")
assert spec is not None and spec.loader is not None
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

SHA = "a" * 40
RUNS = f"workflows/ci.yml/runs?event=push&branch=main&head_sha={SHA}&per_page=100"
JOBS = "runs/123/attempts/2/jobs?per_page=100"
RUN = {
    "id": 123,
    "run_number": 10,
    "run_attempt": 2,
    "path": ".github/workflows/ci.yml",
    "event": "push",
    "head_branch": "main",
    "head_sha": SHA,
    "repository": {"full_name": gate.REPOSITORY},
    "head_repository": {"full_name": gate.REPOSITORY},
    "status": "completed",
    "conclusion": "success",
}
JOB = {
    "id": 456,
    "name": "ci-ok",
    "run_id": 123,
    "run_attempt": 2,
    "head_branch": "main",
    "head_sha": SHA,
    "status": "completed",
    "conclusion": "success",
}


def runs(*items):
    return {"workflow_runs": list(items), "total_count": len(items)}


def jobs(*items):
    return {"jobs": list(items), "total_count": len(items)}


class MainCIGateTest(unittest.TestCase):
    def test_accepts_exact_main_sha_on_latest_attempt(self):
        with patch.object(gate, "fetch", side_effect=[runs(RUN), jobs(JOB), runs(RUN)]) as fetch:
            self.assertEqual(gate.verify(SHA), (123, 2, 456))
            self.assertEqual(fetch.call_args_list, [call(RUNS), call(JOBS), call(RUNS)])

    def test_rejects_non_main_or_unsuccessful_runs(self):
        for change in (
            {"head_sha": "b" * 40},
            {"head_branch": "other"},
            {"event": "pull_request"},
            {"path": ".github/workflows/other.yml"},
            {"head_repository": {"full_name": "fork/perlimen"}},
            {"status": "queued", "conclusion": None},
            {"conclusion": "cancelled"},
        ):
            with (
                self.subTest(change=change),
                patch.object(gate, "fetch", return_value=runs(RUN | change)) as fetch,
                self.assertRaises(ValueError),
            ):
                gate.verify(SHA)
            fetch.assert_called_once_with(RUNS)

    def test_older_success_cannot_override_newer_run_or_prior_attempt(self):
        latest = RUN | {"run_number": 11, "conclusion": "failure"}
        with (
            patch.object(gate, "fetch", return_value=runs(latest, RUN)),
            self.assertRaises(ValueError),
        ):
            gate.verify(SHA)
        with (
            patch.object(gate, "fetch", side_effect=[runs(RUN), jobs(JOB | {"run_attempt": 1})]),
            self.assertRaises(ValueError),
        ):
            gate.verify(SHA)

    def test_requires_one_exact_successful_gate(self):
        for result in (
            jobs(),
            jobs(JOB, JOB),
            jobs(JOB | {"head_sha": "b" * 40}),
            jobs(JOB | {"run_id": 122}),
            jobs(JOB | {"conclusion": "skipped"}),
            jobs(JOB) | {"total_count": 2},
        ):
            with (
                self.subTest(result=result),
                patch.object(gate, "fetch", side_effect=[runs(RUN), result]),
                self.assertRaises(ValueError),
            ):
                gate.verify(SHA)

    def test_unknown_sha_and_api_failure_do_not_authorize(self):
        with (
            patch.object(gate, "fetch", return_value=runs()),
            self.assertRaises(ValueError),
        ):
            gate.verify(SHA)
        with patch.object(gate, "fetch", side_effect=TimeoutError), self.assertRaises(TimeoutError):
            gate.verify(SHA)
        with patch.object(gate, "fetch") as fetch, self.assertRaises(ValueError):
            gate.verify("77f3fe")
        fetch.assert_not_called()


@pytest.mark.parametrize("has_baseline", [False, True])
@pytest.mark.parametrize(
    "final_runs",
    [
        pytest.param(
            runs(RUN | {"run_attempt": 3, "status": "in_progress", "conclusion": None}),
            id="rerun-started",
        ),
        pytest.param(runs(RUN | {"run_attempt": 3}), id="new-green-attempt"),
        pytest.param(runs(RUN, RUN | {"id": 124, "run_number": 11}), id="new-green-run"),
        pytest.param(runs(RUN) | {"total_count": 2}, id="incomplete"),
        pytest.param(TimeoutError("API unavailable"), id="unavailable"),
    ],
)
def test_failed_run_recheck_after_candidate_readiness_recovers(
    tmp_path, capsys, has_baseline, final_runs
):
    target = runpy.run_path(str(LITE_CI / "pull_main.py"))["Target"](tmp_path)
    state = target.load()
    if has_baseline:
        prior_sha = "b" * 40
        state["current"] = {"sha": prior_sha, "checkout": f"{prior_sha}-test", "ci": "prior proof"}
        target.save(state)
    before = target.load()
    release = {
        "sha": SHA,
        "checkout": f"{SHA}-test",
        "ci": f"ci-ok passed: sha={SHA} run=123 attempt=2 job=456",
    }

    def run_verifier(args, **_kwargs):
        # Exercise the real CLI without spawning a process or making real API calls.
        with patch.object(sys, "argv", args[2:]):
            code = gate.main()
        if code:
            raise subprocess.CalledProcessError(code, args)
        return capsys.readouterr().out

    with (
        patch.object(gate, "fetch", side_effect=[runs(RUN), jobs(JOB), final_runs]) as fetch,
        patch.object(subprocess, "check_output", side_effect=run_verifier),
        patch.object(target, "activate") as activate,
        patch.object(target, "live", side_effect=RuntimeError("prior release not running")),
        patch.object(target, "stop") as stop,
        patch.object(target, "main_matches", return_value=True) as main_matches,
        pytest.raises(subprocess.CalledProcessError),
    ):
        target.replace(state, release)

    assert fetch.call_args_list == [call(RUNS), call(JOBS), call(RUNS)]
    main_matches.assert_not_called()
    expected_starts = [call(release, "candidate verified")]
    if has_baseline:
        expected_starts.append(call(before["current"], "recovered"))
        stop.assert_not_called()
    else:
        stop.assert_called_once_with()
    assert activate.call_args_list == expected_starts
    assert state == target.load() == before
    assert not capsys.readouterr().out
