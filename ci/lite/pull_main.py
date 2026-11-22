"""Poll protected main and reconcile one dedicated loopback-only Lite target."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import closing
from fcntl import LOCK_EX, LOCK_NB, flock
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TypedDict

REMOTE = "https://github.com/fbialogrecki/perlimen.git"
MAIN = "refs/heads/main"
INTAKE = "refs/heads/lite-intake-main"
VERIFIER = Path(__file__).resolve().with_name("verify_main_ci.py")
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


class State(TypedDict):
    current: Release | None
    previous: Release | None
    blocked: str | None
    held: bool


def require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def valid_sha(sha: str) -> str:
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "Expected a full commit SHA")
    return sha


class Target:
    def __init__(self, directory: Path, ports: tuple[int, int, int] = PORTS) -> None:
        self.directory = directory.resolve()
        self.checkout = self.directory / "checkout"
        self.record = self.directory / "release.json"
        self.unit = (
            f"perlimen-lite-{hashlib.sha256(os.fsencode(self.checkout)).hexdigest()[:16]}.service"
        )
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

    def git(self, *args: str, timeout: int = 30) -> str:
        return self.run(
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            *args,
            cwd=self.checkout,
            timeout=timeout,
        )

    def load(self) -> State:
        if not self.record.exists():
            return {"current": None, "previous": None, "blocked": None, "held": False}
        state: State = json.loads(self.record.read_text())
        for release in (state["current"], state["previous"]):
            if release is not None:
                valid_sha(release["sha"])
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
        if not self.checkout.exists():
            self.checkout.mkdir(mode=0o700)
        if not (self.checkout / ".git").is_dir():
            require(not any(self.checkout.iterdir()), "Refusing to adopt an existing checkout")
            self.git("init", "--quiet", "--template=")
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

    def eligible(self, sha: str) -> Release:
        ci = subprocess.check_output(
            [sys.executable, "-I", str(VERIFIER), sha], env={}, text=True, timeout=30
        ).strip()
        observed = self.git("ls-remote", "--exit-code", REMOTE, MAIN)
        require(observed == f"{sha}\t{MAIN}", "main moved during CI verification; retry next poll")
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

    def live(self, release: Release, phase: str) -> None:
        group = self.show("ControlGroup")
        require(
            self.show("ActiveState") == "active" and group.startswith("/"), "Lite is not active"
        )
        members = Path(f"/sys/fs/cgroup{group}/cgroup.procs").read_text().split()
        sha = release["sha"]
        require(self.git("rev-parse", "HEAD") == sha, "Runtime checkout SHA mismatch")
        require(not self.git("status", "--porcelain", "-uno"), "Runtime checkout has tracked edits")
        pids = []
        for port in self.ports:
            rows = self.run("ss", "-H", "-ltnp", f"sport = :{port}").splitlines()
            require(
                len(rows) == 1 and rows[0].split()[3] == f"127.0.0.1:{port}",
                f"Expected one loopback listener on {port}",
            )
            match = re.search(r"\bpid=(\d+),", rows[0])
            if match is None or match[1] not in members:
                raise RuntimeError(f"Listener on {port} does not belong to {self.unit}")
            pids.append(match[1])
        for pid, cwd in zip(pids[:2], (self.checkout / "apps/web", self.checkout), strict=True):
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

    def activate(self, release: Release, phase: str) -> None:
        self.stop()
        require(not self.git("status", "--porcelain", "-uno"), "Release checkout has tracked edits")
        self.git("switch", "--quiet", "--detach", valid_sha(release["sha"]))
        web = self.checkout / "apps/web"
        build = web / ".next"
        if build.exists():
            shutil.rmtree(build)
        self.run("bun", "install", "--frozen-lockfile", "--ignore-scripts", cwd=web, timeout=180)
        ports = [
            f"PERLIMEN_LITE_{part}_PORT={port}"
            for part, port in zip(("WEB", "API", "PG"), self.ports, strict=True)
        ]
        self.run(
            "env", *ports, "bash", "-p", "ops/lite.sh", "start", cwd=self.checkout, timeout=255
        )
        self.live(release, phase)

    def recover(self, current: Release | None) -> None:
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

    def replace(self, state: State, release: Release, *, rollback: bool = False) -> None:
        current = state["current"]
        if rollback:
            state["held"] = True  # Persist the hold before stopping the active release.
        else:
            state["blocked"] = release["sha"]  # Also records an interrupted promotion.
        self.save(state)
        try:
            self.activate(release, "rollback verified" if rollback else "candidate verified")
        except RECOVERY_ERRORS:
            self.recover(current)
            raise
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
        self.replace(state, self.eligible(sha))

    def rollback(self, state: State) -> None:
        current, previous = state["current"], state["previous"]
        if current is None or previous is None:
            raise RuntimeError("No previous known-good release")
        self.compatible(current["sha"], previous["sha"])
        self.replace(state, previous, rollback=True)


def main() -> int:
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
        require(not target.checkout.is_symlink(), "Target checkout cannot be a symlink")
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
