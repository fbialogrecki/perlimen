import os
import runpy
import shutil
import socket
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from time import monotonic, sleep
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

CONTROLLER = Path(__file__).resolve().parents[2] / "ci/lite/pull_main.py"

if TYPE_CHECKING:
    from ci.lite.pull_main import Deployment, Release, Target
else:
    module = runpy.run_path(str(CONTROLLER))
    Deployment = module["Deployment"]
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

    def git(self, *args: str, cwd: Path | None = None, timeout: int = 30) -> str:
        assert args[:2] == ("merge-base", "--is-ancestor")
        return ""

    def compatible(self, a: str, b: str) -> None:
        if self.incompatible:
            raise RuntimeError("incompatible")

    def verify_ci(self, sha: str) -> str:
        self.requests.append("CI")
        assert sha == self.candidate["sha"]
        if self.unreachable:
            raise OSError("GitHub unreachable")
        if self.rejected:
            raise subprocess.CalledProcessError(1, "verify_main_ci.py")
        return self.candidate["ci"]

    def prepare(self, release: Release) -> Deployment:
        return {**release, "checkout": f"{release['sha']}-test"}

    def main_matches(self, sha: str) -> bool:
        self.requests.append("main")
        return self.candidate["sha"] == sha

    def activate(self, release: Deployment, phase: str) -> None:
        self.running = {"sha": release["sha"], "ci": release["ci"]}
        self.starts.append(self.running)
        if self.running == self.candidate and self.fail_activation:
            raise RuntimeError("not ready")
        if phase == "candidate verified":
            self.requests.append("ready")

    def live(self, release: Deployment, phase: str) -> None:
        if self.running is None or self.running["sha"] != release["sha"]:
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
    assert target.load() == {
        "held": False,
        "current": target.prepare(A),
        "previous": None,
        "blocked": None,
    }


def test_failure_restarts_known_good_and_does_not_retry_same_sha(tmp_path: Path) -> None:
    target = LocalTarget(tmp_path)
    target.poll(target.load())
    target.candidate = B
    target.fail_activation = True
    with pytest.raises(RuntimeError, match="not ready"):
        target.poll(target.load())
    assert target.starts == [A, B, A]
    assert target.load()["current"] == target.prepare(A)
    assert target.load()["blocked"] == B["sha"]
    target.poll(target.load())
    assert target.starts == [A, B, A]
    target.candidate = C
    target.fail_activation = False
    target.poll(target.load())
    assert target.starts[-1] == C
    assert target.load()["previous"] == target.prepare(A)


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


@pytest.mark.parametrize("has_baseline", [False, True])
@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        ("non-green", subprocess.CalledProcessError),
        ("replaced", RuntimeError),
        ("unavailable", OSError),
    ],
)
def test_rerun_during_readiness_does_not_promote_unchanged_tip(
    tmp_path: Path, has_baseline: bool, outcome: str, error: type[Exception]
) -> None:
    target = LocalTarget(tmp_path)
    if has_baseline:
        target.poll(target.load())
    baseline = target.load()
    target.candidate = B
    replacement: Release = {"sha": B["sha"], "ci": "new attempt evidence"}
    activate = target.activate

    def rerun_after_readiness(release: Deployment, phase: str) -> None:
        activate(release, phase)
        if phase == "candidate verified":
            target.candidate = replacement
            target.rejected = outcome == "non-green"
            target.unreachable = outcome == "unavailable"

    target.requests.clear()
    with patch.object(target, "activate", side_effect=rerun_after_readiness), pytest.raises(error):
        target.poll(target.load())
    assert target.requests == ["git", "CI", "main", "ready", "CI"]
    assert target.load() == baseline
    assert target.running == (A if has_baseline else None)

    target.rejected = False
    target.unreachable = False
    target.requests.clear()
    target.poll(target.load())
    assert target.requests == ["git", "CI", "main", "ready", "CI", "main"]
    assert target.load()["current"] == target.prepare(replacement)


def test_rollback_persists_hold_and_recovers_without_remote(tmp_path: Path) -> None:
    target = LocalTarget(tmp_path)
    target.poll(target.load())
    target.candidate = B
    target.poll(target.load())
    target.requests.clear()
    target.rollback(target.load())
    assert target.requests == []
    assert target.starts == [A, B, A]
    assert target.load() == {
        "held": True,
        "current": target.prepare(A),
        "previous": target.prepare(B),
        "blocked": None,
    }
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


class GitTarget(Target):
    """Use real intake, worktrees, release state and remote refs; fake the runtime only."""

    running: Path | None = None
    dirty: Path | None = None
    fail = False
    on_ready: str | None = None

    def __init__(self, directory: Path, remote: Path) -> None:
        super().__init__(directory)
        self.remote = remote
        self.data = directory / "synthetic-data"

    def verify_ci(self, sha: str) -> str:
        return "verified"

    def run(self, *args: str, cwd: Path | None = None, timeout: int = 30) -> str:
        if args[0] != "systemd-run":
            return super().run(*args, cwd=cwd, timeout=timeout)
        assert f"--property=StateDirectory={self.instance}" in args
        self.running = Path(args[-4])
        assert self.data.read_text() == "keep this synthetic account"
        if self.fail:
            self.fail = False
            self.dirty = self.running
            (self.running / "tracked").write_text("candidate edit")
            (self.running / "operator-notes").write_text("keep this file too")
        return ""

    def live(self, release: Deployment, phase: str) -> None:
        if self.clean(release) != self.running:
            raise RuntimeError("not running this checkout")
        if phase == "candidate verified" and self.on_ready is not None:
            self.git("update-ref", "refs/heads/main", self.on_ready, cwd=self.remote)
            self.on_ready = None

    def stop(self) -> None:
        self.running = None


@pytest.fixture
def git_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[GitTarget, list[str]]:
    remote = tmp_path / "remote"
    remote.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    target = GitTarget(state, remote)
    monkeypatch.setitem(Target.tip.__globals__, "REMOTE", str(remote))
    target.git("init", "--template=", "-b", "main", cwd=remote)
    shas = []
    for content in ("good", "candidate", "later"):
        (remote / "tracked").write_text(content)
        target.git("add", "tracked", cwd=remote)
        target.git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-m",
            content,
            cwd=remote,
        )
        shas.append(target.git("rev-parse", "HEAD", cwd=remote))
    target.git("switch", "--detach", cwd=remote)
    target.data.write_text("keep this synthetic account")
    return target, shas


def test_dirty_candidate_cannot_poison_recovery_or_the_next_poll(
    git_target: tuple[GitTarget, list[str]],
) -> None:
    target, (good, bad, later) = git_target
    target.git("update-ref", "refs/heads/main", good, cwd=target.remote)
    target.poll(target.load())
    known_good = target.running
    target.git("update-ref", "refs/heads/main", bad, cwd=target.remote)
    target.fail = True
    with pytest.raises(RuntimeError, match="tracked edits"):
        target.poll(target.load())
    assert target.running == known_good
    assert known_good is not None and (known_good / "tracked").read_text() == "good"
    assert target.load()["blocked"] == bad
    target.git("update-ref", "refs/heads/main", later, cwd=target.remote)
    # A newly constructed timer poll still has the original clean checkout available.
    restarted = GitTarget(target.directory, target.remote)
    restarted.poll(restarted.load())
    assert restarted.running is not None and (restarted.running / "tracked").read_text() == "later"
    assert target.dirty is not None
    assert (target.dirty / "tracked").read_text() == "candidate edit"
    assert (target.dirty / "operator-notes").read_text() == "keep this file too"
    assert restarted.data.read_text() == "keep this synthetic account"


@pytest.mark.parametrize("has_baseline", [False, True])
def test_main_is_read_again_after_readiness_without_rejecting_superseded_sha(
    git_target: tuple[GitTarget, list[str]],
    has_baseline: bool,
) -> None:
    target, (good, candidate, later) = git_target
    if has_baseline:
        target.git("update-ref", "refs/heads/main", good, cwd=target.remote)
        target.poll(target.load())
    baseline = target.load()
    running = target.running
    target.git("update-ref", "refs/heads/main", candidate, cwd=target.remote)
    target.on_ready = later
    target.poll(target.load())
    assert target.load() == baseline  # No promotion, rollback-history change or rejection.
    assert target.running == running
    target.poll(target.load())
    current = target.load()["current"]
    assert current is not None and current["sha"] == later


@pytest.fixture
def runtime_stub(tmp_path: Path) -> tuple[dict[str, str], Path, list[str], list[int]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    checkout = tmp_path / "checkout"
    (checkout / "ops").mkdir(parents=True)
    (checkout / "apps/web").mkdir(parents=True)
    bun = bin_dir / "bun"
    bun.write_text('#!/bin/sh\ntouch "$CANDIDATE_RAN.install"\n')
    bun.chmod(0o700)
    podman = bin_dir / "podman"
    podman.write_text(
        '#!/bin/sh\n'
        '# Fail before touching any Podman state, even against the old controller.\n'
        '[ "$1" = --tmpdir ] && [ "$3" = --remote=false ] || exit 1\n'
        'if [ "$#" -gt 3 ]; then shift 2; printf "%s\\n" "$@"; exit; fi\n'
        'printf "%s\\n" "$2" "$XDG_RUNTIME_DIR" "$CONTAINERS_CONF_OVERRIDE" '
        '"$_CONTAINERS_ROOTLESS_UID" "$_CONTAINERS_ROOTLESS_GID"\n'
    )
    podman.chmod(0o700)
    state = tmp_path / "state"
    state.mkdir()
    runtime = tmp_path / "private runtime"
    runtime.mkdir()
    ready = tmp_path / "candidate-ran"
    # Reserve only loopback ports; even a broken controller must not cause a wildcard test bind.
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(5)]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        ports = [sock.getsockname()[1] for sock in sockets]
    env = {
        "STATE_DIRECTORY": str(state),
        "RUNTIME_DIRECTORY": str(runtime),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOST_NET": os.readlink("/proc/self/ns/net"),
        "HOST_UID": str(os.geteuid()),
        "HOST_GID": str(os.getegid()),
        "CANDIDATE_RAN": str(ready),
        "EXTRA_TCP": str(ports[3]),
        "EXTRA_UDP": str(ports[4]),
        "TEST_PYTHON": sys.executable,
    }
    (checkout / "ops/lite.sh").write_text(
        'exec "$TEST_PYTHON" -I - "$@" <<\'PY\'\n'
        "import os, socket, subprocess, sys\n"
        "from pathlib import Path\n"
        'assert os.readlink("/proc/self/ns/net") != os.environ["HOST_NET"]\n'
        'assert sys.argv[1] == "run" and sys.argv[5:] == ["", "isolated"]\n'
        'assert os.geteuid() == os.getegid() == 0\n'
        '# Model the checkout launcher clearing Podman environment before the call.\n'
        'result = subprocess.check_output(["podman", "--remote=false"], text=True,\n'
        '    env={k: os.environ[k] for k in ("PATH", "_CONTAINERS_USERNS_CONFIGURED")})\n'
        'tmp, runtime, config, uid, gid = result.splitlines()\n'
        'private = Path(os.environ["RUNTIME_DIRECTORY"]) / "podman"\n'
        'assert Path(tmp) == private / "tmp" and Path(runtime) == private\n'
        'assert Path(config).parent == private and Path(config).is_file()\n'
        'assert (uid, gid) == (os.environ["HOST_UID"], os.environ["HOST_GID"])\n'
        'pg = ["podman", "--remote=false", "--root", "state path", "--runroot", "run path"]\n'
        'run = ["--rm", "--name", "postgres", "--network=host", "postgres:17-alpine"]\n'
        'args = subprocess.check_output([*pg, "run", *run], text=True).splitlines()\n'
        'mount = args[7].rsplit(":", 2)\n'
        'assert args[:7] == [*pg[1:], "run", "--volume"]\n'
        'assert args[8:] == [*run, "-c", "hba_file=/run/lite-pg_hba.conf"]\n'
        'assert mount[1:] == ["/run/lite-pg_hba.conf", "ro,Z"]\n'
        'hba = Path(mount[0])\n'
        'assert hba.parent == private\n'
        'assert [line.split() for line in hba.read_text().splitlines()] == [\n'
        '    ["local", "all", "lite_bootstrap", "scram-sha-256"],\n'
        '    ["host", "all", "all", "127.0.0.1/32", "scram-sha-256"],\n'
        ']\n'
        'assert subprocess.check_output([*pg, "stop", "postgres"], text=True).splitlines() == [\n'
        '    *pg[1:], "stop", "postgres",\n'
        ']\n'
        "expected = socket.socket()\n"
        'expected.bind(("127.0.0.1", int(sys.argv[2])))\n'
        "expected.listen()\n"
        "extra = socket.socket()\n"
        'extra.bind(("127.0.0.1", int(os.environ["EXTRA_TCP"])))\n'
        "extra.listen()\n"
        "udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        'udp.bind(("127.0.0.1", int(os.environ["EXTRA_UDP"])))\n'
        'Path(os.environ["CANDIDATE_RAN"]).touch()\n'
        "expected.settimeout(8)\n"
        'with expected.accept()[0] as peer: peer.sendall(b"isolated")\n'
        "PY\n"
    )
    command = [
        sys.executable,
        "-I",
        str(CONTROLLER),
        "isolated-runtime",
        str(checkout),
        *map(str, ports[:3]),
    ]
    return env, ready, command, ports


def test_no_candidate_code_runs_when_namespace_is_unavailable(
    runtime_stub: tuple[dict[str, str], Path, list[str], list[int]],
) -> None:
    env, ready, command, _ = runtime_stub
    unshare = Path(env["PATH"].split(":")[0]) / "unshare"
    # Simulate a launcher reporting a PID without actually entering a new network namespace.
    unshare.write_text('#!/bin/sh\nwhile [ "${1#--}" != "$1" ]; do shift; done\nexec "$@"\n')
    unshare.chmod(0o700)
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0 and "No network isolation" in result.stderr
    assert not ready.with_suffix(".install").exists() and not ready.exists()


def test_runtime_forwards_only_explicit_loopback_ports(
    runtime_stub: tuple[dict[str, str], Path, list[str], list[int]],
) -> None:
    env, ready, command, ports = runtime_stub
    if any(not shutil.which(binary) for binary in ("unshare", "pasta", "ss")):
        pytest.skip("Rootless namespace tools not installed")
    pasta_help = subprocess.check_output(["pasta", "--help"], text=True)
    if any(option not in pasta_help for option in ("--host-lo-to-ns-lo", "--no-netns-quit")):
        pytest.skip("pasta lacks required namespace and forwarding options")
    probe = ["unshare", "--map-root-user", "--map-auto", "--net", "--mount", "true"]
    if subprocess.run(probe, env=env, capture_output=True, timeout=15).returncode:
        pytest.skip("Mapped rootless user/network/mount namespaces are unavailable")
    # Reject permissive forwarding arguments before invoking the real proxy, even on a regression.
    pasta = Path(env["PATH"].split(":")[0]) / "pasta"
    ftp = "127.0.0.1/" + ",".join(map(str, ports[:3]))
    pasta.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "assert [(args[i], args[i+1]) for i in range(len(args)-1) if args[i] in "
        f"('-t', '-u', '-T', '-U')] == [('-t', {ftp!r}), ('-u', 'none'), "
        "('-T', 'none'), ('-U', 'none')]\n"
        "assert not any('ports' in arg or '-ns' in arg for arg in args if arg.startswith('--') "
        "and arg not in ('--host-lo-to-ns-lo', '--no-netns-quit'))\n"
        f"os.execv({shutil.which('pasta')!r}, [{shutil.which('pasta')!r}, *args])\n"
    )
    pasta.chmod(0o700)
    with subprocess.Popen(command, env=env) as process:
        try:
            deadline = monotonic() + 12
            while process.poll() is None and not ready.exists() and monotonic() < deadline:
                sleep(0.05)
            assert ready.exists()
            for port in ports:
                rows = subprocess.check_output(
                    ["ss", "-H", "-lntu", f"sport = :{port}"], text=True
                ).splitlines()
                assert [(row.split()[0], row.split()[4]) for row in rows] == (
                    [("tcp", f"127.0.0.1:{port}")] if port in ports[:3] else []
                )
            with socket.create_connection(("127.0.0.1", ports[0]), timeout=3) as peer:
                assert peer.recv(64) == b"isolated"
            assert process.wait(timeout=5) == 0
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=25)
