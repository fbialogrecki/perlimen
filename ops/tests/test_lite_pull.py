import runpy
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from ci.lite.pull_main import Release, Target
else:
    module = runpy.run_path(str(Path(__file__).resolve().parents[2] / "ci/lite/pull_main.py"))
    Release = module["Release"]
    Target = module["Target"]

A: Release = {"sha": "a" * 40, "ci": "baseline evidence"}
B: Release = {"sha": "b" * 40, "ci": "candidate evidence"}
C: Release = {"sha": "c" * 40, "ci": "later evidence"}


class LocalTarget(Target):
    """Model the external Git, CI and systemd boundaries; persist real state on disk."""

    running: Release | None = None
    candidate = A
    rejected = False
    fail_activation = False
    unreachable = False
    incompatible = False
    starts: list[Release]
    requests: list[str]

    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.starts = []
        self.requests = []

    def tip(self) -> str:
        self.requests.append("git")
        if self.unreachable:
            raise OSError("GitHub unreachable")
        return self.candidate["sha"]

    def git(self, *args: str, timeout: int = 30) -> str:
        assert args[:2] == ("merge-base", "--is-ancestor")
        return ""

    def compatible(self, a: str, b: str) -> None:
        if self.incompatible:
            raise RuntimeError("incompatible")

    def eligible(self, sha: str) -> Release:
        self.requests.append("CI")
        assert sha == self.candidate["sha"]
        if self.rejected:
            raise subprocess.CalledProcessError(1, "verify_main_ci.py")
        return self.candidate

    def activate(self, release: Release, phase: str) -> None:
        self.starts.append(release)
        self.running = release
        if release == self.candidate and self.fail_activation:
            raise RuntimeError("not ready")

    def live(self, release: Release, phase: str) -> None:
        if self.running != release:
            raise RuntimeError("not running the expected SHA")

    def stop(self) -> None:
        self.running = None


def test_exact_gate_and_compatibility_precede_any_restart(tmp_path: Path) -> None:
    target = LocalTarget(tmp_path)
    target.poll(target.load())
    target.candidate = B
    target.rejected = True
    with pytest.raises(subprocess.CalledProcessError):
        target.poll(target.load())
    target.incompatible = True
    with pytest.raises(RuntimeError, match="incompatible"):
        target.poll(target.load())
    assert target.running == A
    assert target.starts == [A]
    assert target.load() == {"held": False, "current": A, "previous": None, "blocked": None}


def test_failure_restarts_known_good_and_does_not_retry_same_sha(tmp_path: Path) -> None:
    target = LocalTarget(tmp_path)
    target.poll(target.load())
    target.candidate = B
    target.fail_activation = True
    with pytest.raises(RuntimeError, match="not ready"):
        target.poll(target.load())
    assert target.starts == [A, B, A]
    assert target.load()["current"] == A
    assert target.load()["blocked"] == B["sha"]
    target.poll(target.load())
    assert target.starts == [A, B, A]
    target.candidate = C
    target.fail_activation = False
    target.poll(target.load())
    assert target.starts[-1] == C
    assert target.load()["previous"] == A


def test_interrupted_switch_and_offline_restart_restore_last_committed_sha(tmp_path: Path) -> None:
    target = LocalTarget(tmp_path)
    target.poll(target.load())
    state = target.load()
    state["blocked"] = B["sha"]
    target.save(state)
    restarted = LocalTarget(tmp_path)
    restarted.running = B  # Candidate started, but the controller never committed it.
    restarted.unreachable = True
    with pytest.raises(OSError, match="GitHub unreachable"):
        restarted.poll(restarted.load())
    assert restarted.starts == [A]
    assert restarted.load() == state


def test_rollback_persists_hold_and_recovers_without_remote(tmp_path: Path) -> None:
    target = LocalTarget(tmp_path)
    target.poll(target.load())
    target.candidate = B
    target.poll(target.load())
    target.rollback(target.load())
    assert target.starts == [A, B, A]
    assert target.load() == {"held": True, "current": A, "previous": B, "blocked": None}
    restarted = LocalTarget(tmp_path)
    restarted.poll(restarted.load())
    assert restarted.starts == [A]
    assert restarted.requests == []


def test_main_advancing_after_gate_blocks_promotion(tmp_path: Path) -> None:
    target = Target(tmp_path)
    with (
        patch.object(subprocess, "check_output", return_value="valid CI") as verify,
        patch.object(target, "git", return_value=f"{B['sha']}\trefs/heads/main"),
        pytest.raises(RuntimeError, match="main moved"),
    ):
        target.eligible(A["sha"])
    assert verify.call_args.args[0][-1] == A["sha"]
    assert verify.call_args.kwargs["env"] == {}
