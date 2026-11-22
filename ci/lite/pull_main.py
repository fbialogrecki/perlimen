"""Poll protected main and reconcile one dedicated loopback-only Lite target."""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from contextlib import closing, suppress
from fcntl import LOCK_EX, LOCK_NB, flock
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from select import select
from shlex import quote
from shutil import which
from tempfile import NamedTemporaryFile, mkdtemp
from time import monotonic, sleep
from typing import TypedDict

REMOTE = "https://github.com/fbialogrecki/perlimen.git"
MAIN = "refs/heads/main"
INTAKE = "refs/heads/lite-intake-main"
VERIFIER = Path(__file__).resolve().with_name("verify_main_ci.py")
CONTROLLER = Path(__file__).resolve()
PORTS = (13300, 13800, 15432)  # Web, API, PostgreSQL
RECOVERY_ERRORS = (OSError, subprocess.SubprocessError, RuntimeError, HTTPException)
# Compare trees (without rename detection), not commit messages or migration numbers.
REVERSIBLE = (
    ":(glob)**/migrations/**",
    ":(glob)services/*/src/*/db/base.py",
    ":(glob)**/pyproject.toml",
    ".python-version",
    "uv.lock",
    "apps/web/bun.lock",
    "apps/web/package.json",
    "perlimen_lite/database.py",
    "perlimen_lite/migrate.py",
    "ops/lite.sh",
)


class Release(TypedDict):
    sha: str
    ci: str


class Deployment(Release):
    checkout: str


class State(TypedDict):
    current: Deployment | None
    previous: Deployment | None
    blocked: str | None
    held: bool


def require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def valid_sha(sha: str) -> str:
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "Expected a full commit SHA")
    return sha


def isolated_runtime(ports: tuple[int, ...], command: list[str]) -> int:
    """Start no release code until a fresh rootless namespace has explicit forwards."""
    require(os.geteuid() != 0, "Use the dedicated rootless account")
    require(len(ports) == len(set(ports)) == 3, "Expected three distinct ports")
    require(all(1024 <= port <= 65535 for port in ports), "Invalid runtime port")
    runtime = Path(os.environ["RUNTIME_DIRECTORY"])
    podman = which("podman")
    if podman is None:
        raise RuntimeError("Missing local Podman")
    podman_runtime = runtime / "podman"
    podman_runtime.mkdir(mode=0o700)
    config = podman_runtime / "containers.conf"
    config.write_text('[engine]\nlock_type = "file"\n')
    # The checkout launcher scrubs the environment. Restore the mapped host IDs
    # and private Podman settings at the CLI boundary, even for old checkouts.
    cli = podman_runtime / "podman"
    cli.write_text(
        "#!/bin/sh\n"
        f"export _CONTAINERS_ROOTLESS_UID={os.geteuid()} _CONTAINERS_ROOTLESS_GID={os.getegid()}\n"
        f"export XDG_RUNTIME_DIR={quote(str(podman_runtime))} "
        f"CONTAINERS_CONF_OVERRIDE={quote(str(config))}\n"
        f"exec {quote(os.path.abspath(podman))} "
        f"--tmpdir {quote(str(podman_runtime / 'tmp'))} \"$@\"\n"
    )
    cli.chmod(0o700)
    dns_options = []
    resolver = Path("/etc/resolv.conf").resolve()
    if resolver == Path("/run/systemd/resolve/stub-resolv.conf"):
        # The host's loopback resolver is not on the namespace's loopback.
        guest_dns = "192.0.2.53"
        conf = runtime / "resolv.conf"
        conf.write_text(
            resolver.read_text().replace("nameserver 127.0.0.53", f"nameserver {guest_dns}")
        )
        dns_options = ["--dns-forward", guest_dns, "--dns-host", "127.0.0.53"]
        command = [
            "bash",
            "-p",
            "-ec",
            'mount --bind "$1" "$2"; shift 2; exec "$@"',
            "lite-resolver",
            str(conf),
            str(resolver),
            *command,
        ]

    def listeners(port: int) -> list[str]:
        return subprocess.check_output(
            ["ss", "-H", "-ltn", f"sport = :{port}"], text=True, timeout=5
        ).splitlines()

    for port in ports:
        require(not listeners(port), f"Host port {port} is occupied")
    with subprocess.Popen(
        [
            "unshare",
            "--map-root-user",
            "--map-auto",
            "--net",
            "--mount",
            "bash",
            "-p",
            "-c",
            'echo "$$"; read -r go && [[ $go == go ]] || exit 1; exec 1>&2; exec "$@"',
            "lite-netns",
            *command,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=os.environ | {
            "PATH": f"{podman_runtime}:{os.environ['PATH']}",
            "_CONTAINERS_USERNS_CONFIGURED": "done",
        },
        text=True,
    ) as inner:
        assert inner.stdin is not None and inner.stdout is not None
        proxy = None
        pid = None
        stop = False

        def stopping(_signum: int, _frame: object) -> None:
            nonlocal stop
            stop = True

        handler = signal.signal(signal.SIGTERM, stopping)
        try:
            require(select([inner.stdout], [], [], 10)[0], "No private user/network namespace")
            raw_pid = inner.stdout.readline().strip()
            require(raw_pid.isascii() and raw_pid.isdecimal(), "No private namespace PID")
            for ns, kind in (("net", "network"), ("user", "user"), ("mnt", "mount")):
                namespace = Path(f"/proc/{raw_pid}/ns/{ns}").readlink()
                require(namespace != Path(f"/proc/self/ns/{ns}").readlink(), f"No {kind} isolation")
            pid = int(raw_pid)
            forward = "127.0.0.1/" + ",".join(map(str, ports))
            proxy = subprocess.Popen(
                [
                    "pasta",
                    "--quiet",
                    "--foreground",
                    "--config-net",
                    "--host-lo-to-ns-lo",
                    "--no-map-gw",
                    "--no-netns-quit",  # The controller, not a procfs watch, owns the proxy.
                    *dns_options,
                    "-t",
                    forward,
                    "-u",
                    "none",
                    "-T",
                    "none",
                    "-U",
                    "none",
                    raw_pid,
                ]
            )
            deadline = monotonic() + 10
            while True:
                require(
                    not stop and proxy.poll() is None and inner.poll() is None,
                    "Loopback isolation failed to start",
                )
                if all(
                    [row.split()[3] for row in listeners(p)] == [f"127.0.0.1:{p}"] for p in ports
                ):
                    break
                require(monotonic() < deadline, "Loopback forwards did not become available")
                sleep(0.1)
            (runtime / "namespace-pid").write_text(raw_pid)
            inner.stdin.write("go\n")
            inner.stdin.close()
            while inner.poll() is None and not stop:
                require(proxy.poll() is None, "Loopback proxy exited")
                sleep(0.2)
            return 0 if stop else inner.wait()
        finally:
            inner.stdin.close()
            try:
                if inner.poll() is None and pid is not None:
                    with suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGTERM)
                try:
                    inner.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    inner.kill()
            finally:
                if proxy is not None and proxy.poll() is None:
                    proxy.terminate()
                    proxy.wait(timeout=3)
                signal.signal(signal.SIGTERM, handler)


class Target:
    def __init__(self, directory: Path, ports: tuple[int, int, int] = PORTS) -> None:
        self.directory = directory.resolve()
        self.intake = self.directory / "intake.git"
        self.releases = self.directory / "releases"
        self.record = self.directory / "release.json"
        key = hashlib.sha256(os.fsencode(self.directory / "checkout")).hexdigest()[:16]
        self.instance = f"perlimen-lite-{key}"
        self.unit = f"{self.instance}.service"
        self.ports = ports
        home = Path.home()
        self.env = {
            name: os.environ[name]
            for name in ("USER", "LOGNAME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
            if name in os.environ
        } | {
            "HOME": str(home),
            "PATH": f"{home}/.bun/bin:{home}/.local/bin:{os.defpath}",
            "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }

    def run(self, *args: str, cwd: Path | None = None, timeout: int = 30) -> str:
        return subprocess.check_output(
            args, cwd=cwd or self.directory, timeout=timeout, env=self.env, text=True
        ).strip()

    def git(self, *args: str, cwd: Path | None = None, timeout: int = 30) -> str:
        return self.run(
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            *args,
            cwd=cwd or self.intake,
            timeout=timeout,
        )

    def load(self) -> State:
        if not self.record.exists():
            return {"current": None, "previous": None, "blocked": None, "held": False}
        state: State = json.loads(self.record.read_text())
        for release in (state["current"], state["previous"]):
            if release is not None:
                self.checkout(release)
        if state["blocked"] is not None:
            valid_sha(state["blocked"])
        require(type(state["held"]) is bool, "Invalid hold in release record")
        return state

    def save(self, state: State) -> None:
        with NamedTemporaryFile(
            mode="w", dir=self.directory, prefix=".release-", delete=False
        ) as file:
            tmp = Path(file.name)
            try:
                json.dump(state, file)
                file.flush()
                os.fsync(file.fileno())
                tmp.replace(self.record)
                fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            finally:
                tmp.unlink(missing_ok=True)

    def tip(self) -> str:
        if not self.intake.exists():
            self.intake.mkdir(mode=0o700)
            self.git("init", "--bare", "--quiet", "--template=")
        # A local branch ref rejects non-fast-forward fetches. Never force-update it.
        self.git(
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-recurse-submodules",
            REMOTE,
            f"{MAIN}:{INTAKE}",
            timeout=120,
        )
        return valid_sha(self.git("rev-parse", INTAKE))

    def main_matches(self, sha: str) -> bool:
        observed = self.git("ls-remote", "--exit-code", REMOTE, MAIN)
        tip, _, ref = observed.partition("\t")
        require(ref == MAIN, "Unexpected remote main response")
        return valid_sha(tip) == sha

    def verify_ci(self, sha: str) -> str:
        return subprocess.check_output(
            [sys.executable, "-I", str(VERIFIER), sha], env={}, text=True, timeout=30
        ).strip()

    def eligible(self, sha: str) -> Release:
        ci = self.verify_ci(sha)
        require(self.main_matches(sha), "main moved during CI verification; retry next poll")
        return {"sha": sha, "ci": ci}

    def compatible(self, a: str, b: str) -> None:
        try:
            self.git("diff", "--quiet", "--no-ext-diff", "--no-renames", a, b, "--", *REVERSIBLE)
        except subprocess.CalledProcessError as error:
            raise RuntimeError(
                "Migration, launcher or dependency inputs differ; target unchanged"
            ) from error

    def show(self, property_name: str) -> str:
        return self.run("systemctl", "--user", "show", self.unit, "-p", property_name, "--value")

    def stop(self) -> None:
        if self.show("LoadState") != "not-found":
            self.run("systemctl", "--user", "stop", self.unit, timeout=40)

    def checkout(self, release: Deployment) -> Path:
        sha = valid_sha(release["sha"])
        name = release["checkout"]
        require(re.fullmatch(rf"{sha}-[a-z0-9_]+", name), "Invalid release checkout name")
        path = self.releases / name
        require(not path.is_symlink(), "Release checkout cannot be a symlink")
        return path

    def prepare(self, release: Release) -> Deployment:
        self.releases.mkdir(mode=0o700, exist_ok=True)
        sha = valid_sha(release["sha"])
        checkout = Path(mkdtemp(prefix=f"{sha}-", dir=self.releases))
        self.git("worktree", "add", "--quiet", "--detach", str(checkout), sha)
        return {"sha": sha, "ci": release["ci"], "checkout": checkout.name}

    def clean(self, release: Deployment) -> Path:
        checkout = self.checkout(release)
        head = self.git("rev-parse", "HEAD", cwd=checkout)
        require(head == release["sha"], "Checkout SHA mismatch")
        status = self.git("status", "--porcelain", "-uno", cwd=checkout)
        require(not status, "Checkout has tracked edits")
        return checkout

    def live(self, release: Deployment, phase: str) -> None:
        group = self.show("ControlGroup")
        require(
            self.show("ActiveState") == "active" and group.startswith("/"), "Lite is not active"
        )
        members = Path(f"/sys/fs/cgroup{group}/cgroup.procs").read_text().split()
        checkout = self.clean(release)
        runtime = Path(self.env["XDG_RUNTIME_DIR"]) / self.instance
        pid = (runtime / "namespace-pid").read_text().strip()
        require(pid in members, "Lite namespace does not belong to the running unit")
        require(
            Path(f"/proc/{pid}/ns/net").readlink() != Path("/proc/self/ns/net").readlink(),
            "Lite shares the host network namespace",
        )
        pids = []
        for port in self.ports:
            rows = self.run(
                "nsenter",
                "--target",
                pid,
                "--user",
                "--net",
                "ss",
                "-H",
                "-ltnp",
                f"sport = :{port}",
            ).splitlines()
            require(
                len(rows) == 1 and rows[0].split()[3] == f"127.0.0.1:{port}",
                f"Expected one loopback listener on {port}",
            )
            match = re.search(r"\bpid=(\d+),", rows[0])
            if match is None or match[1] not in members:
                raise RuntimeError(f"Listener on {port} does not belong to {self.unit}")
            pids.append(match[1])
        for pid, cwd in zip(pids[:2], (checkout / "apps/web", checkout), strict=True):
            require(
                Path(f"/proc/{pid}/cwd").resolve(strict=True) == cwd, "Listener checkout mismatch"
            )
        for port, route in zip(self.ports[:2], ("/", "/readyz"), strict=True):
            with closing(HTTPConnection("127.0.0.1", port, timeout=10)) as client:
                client.request("GET", route)
                require(client.getresponse().status == 200, f"Health check failed: {port}{route}")
        if phase:
            print(
                f"{phase}: {release['ci']} unit={self.unit} web/api/pg_pids={','.join(pids)}",
                flush=True,
            )

    def activate(self, release: Deployment, phase: str) -> None:
        checkout = self.clean(release)
        self.stop()
        self.run(
            "systemd-run",
            "--user",
            f"--unit={self.unit}",
            "--collect",
            "--service-type=notify",
            "--property=NotifyAccess=all",
            "--property=KillMode=mixed",
            "--property=TimeoutStartSec=420",
            "--property=TimeoutStopSec=25",
            f"--property=StateDirectory={self.instance}",
            "--property=StateDirectoryMode=0700",
            f"--property=RuntimeDirectory={self.instance}",
            "--property=RuntimeDirectoryMode=0700",
            f"--setenv=PATH={self.env['PATH']}",
            "--expand-environment=no",
            sys.executable,
            "-I",
            str(CONTROLLER),
            "isolated-runtime",
            str(checkout),
            *map(str, self.ports),
            timeout=435,
        )
        self.live(release, phase)

    def recover(self, current: Deployment | None) -> None:
        if current is None:
            self.stop()  # Interrupted first rollout: no known-good revision yet.
            return
        try:
            self.live(current, "")
        except RECOVERY_ERRORS:
            try:
                self.activate(current, "recovered")
            except RECOVERY_ERRORS:
                self.stop()
                raise

    def replace(self, state: State, release: Deployment, *, rollback: bool = False) -> None:
        current = state["current"]
        if rollback:
            state["held"] = True  # Persist the hold before stopping the active release.
            self.save(state)
        try:
            self.activate(release, "rollback verified" if rollback else "candidate verified")
        except RECOVERY_ERRORS:
            if not rollback:
                state["blocked"] = release["sha"]
                self.save(state)
            self.recover(current)
            raise
        if not rollback:
            # A stale or unreadable gate leaves the candidate retriable.
            try:
                require(
                    self.verify_ci(release["sha"]) == release["ci"],
                    "CI proof changed during readiness; retry next poll",
                )
                still_tip = self.main_matches(release["sha"])
            except RECOVERY_ERRORS:
                self.recover(current)
                raise
            if not still_tip:
                self.recover(current)
                print("main moved during readiness; retry next poll", flush=True)
                return
        state["current"], state["previous"] = release, current
        if not rollback:
            state["blocked"] = None
        self.save(state)
        print(f"{'rolled back' if rollback else 'promoted'} sha={release['sha']}", flush=True)

    def poll(self, state: State) -> None:
        current = state["current"]
        self.recover(current)  # Even offline or on hold, restart only the known-good SHA.

        if state["held"]:
            return
        sha = self.tip()
        baseline = current["sha"] if current else state["blocked"]
        if sha in (baseline, state["blocked"]):
            return
        if baseline:
            self.git("merge-base", "--is-ancestor", baseline, sha)
            self.compatible(baseline, sha)
        self.replace(state, self.prepare(self.eligible(sha)))

    def rollback(self, state: State) -> None:
        current, previous = state["current"], state["previous"]
        if current is None or previous is None:
            raise RuntimeError("No previous known-good release")
        self.compatible(current["sha"], previous["sha"])
        self.replace(state, previous, rollback=True)


def main() -> int:
    if sys.argv[1:2] == ["isolated-runtime"]:
        checkout, *ports = sys.argv[2:]
        return isolated_runtime(
            tuple(map(int, ports)),
            [
                "bash",
                "-p",
                "-c",
                'set -e; cd -- "$1/apps/web"; shift; '
                "bun install --frozen-lockfile --ignore-scripts; cd ../..; "
                'exec bash -p ops/lite.sh run "$@" "" isolated',
                "lite-launch",
                checkout,
                *ports,
            ],
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state", type=Path, required=True, help="Private dedicated target directory"
    )
    parser.add_argument("action", choices=("poll", "status", "rollback", "resume", "stop"))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        require(
            os.geteuid() != 0 and os.environ.get("XDG_RUNTIME_DIR"),
            "Run as the dedicated unprivileged systemd user",
        )
        target = Target(args.state)
        target.directory.mkdir(mode=0o700, exist_ok=True)
        info = target.directory.stat()
        require(
            info.st_uid == os.getuid() and not info.st_mode & 0o077,
            "Target directory must be owned by this user and private",
        )
        require(not target.intake.is_symlink(), "Intake cannot be a symlink")
        require(not target.releases.is_symlink(), "Release directory cannot be a symlink")
        with (target.directory / "lock").open("a") as lock:
            flock(lock, LOCK_EX | LOCK_NB)
            state = target.load()
            match args.action:
                case "poll":
                    target.poll(state)
                case "rollback":
                    target.rollback(state)
                case "resume":
                    state["held"] = False
                    target.save(state)
                case "stop":
                    target.stop()
                case "status":
                    print(json.dumps(state), flush=True)
                    if state["current"]:
                        target.live(state["current"], "running")
    except (*RECOVERY_ERRORS, ValueError, KeyError, TypeError) as error:
        print(f"Lite pull: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
