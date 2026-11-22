import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "lite_main_ci", Path(__file__).resolve().parents[2] / "ci/lite/verify_main_ci.py"
)
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
        with patch.object(gate, "fetch", side_effect=[runs(RUN), jobs(JOB)]) as fetch:
            self.assertEqual(gate.verify(SHA), (123, 2, 456))
            self.assertEqual([call.args[0] for call in fetch.call_args_list], [RUNS, JOBS])

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
