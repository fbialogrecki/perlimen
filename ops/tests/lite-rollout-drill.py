"""Replay one eligible main push and roll back on a disposable Lite target."""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
from contextlib import closing
from functools import partial
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
HOST = "127.0.0.1"


def require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run(*args: str, cwd: Path = ROOT, timeout: int = 30, env: dict[str, str]) -> str:
    return subprocess.check_output(args, cwd=cwd, env=env, text=True, timeout=timeout).strip()


def auth(port: int, step: str, password: str) -> tuple[UUID, str]:
    username = "lite-rollout-user"
    payload = {"password": password}
    if step == "register":
        payload.update(username=username, email=f"{username}@example.test")
    else:
        payload["emailOrUsername"] = username
    headers = {"Content-Type": "application/json", "Origin": f"http://{HOST}:{port}"}
    with closing(HTTPConnection(HOST, port, timeout=45)) as client:
        client.request("POST", f"/api/auth/{step}", json.dumps(payload), headers)
        response = client.getresponse()
        expected = 201 if step == "register" else 200
        require(
            response.status == expected, f"BFF {step} expected {expected}, got {response.status}"
        )
        cookies = SimpleCookie()
        for name, value in response.getheaders():
            if name.lower() == "set-cookie":
                cookies.load(value)
        user = json.load(response)["user"]
        require(user["username"] == username, "Unexpected BFF account")
        return UUID(user["id"]), cookies["threshold_refresh"]["path"]


def gate(sha: str) -> None:
    verifier = ROOT / "ci/lite/verify_main_ci.py"
    print(run(sys.executable, "-I", str(verifier), sha, env={}), flush=True)


def drill(target: Path, baseline: str, candidate: str, env: dict[str, str]) -> None:
    cmd = partial(run, env=env, cwd=target)
    instance = "perlimen-lite-" + hashlib.sha256(os.fsencode(target)).hexdigest()[:16]
    unit = f"{instance}.service"
    runtime = Path(env["XDG_RUNTIME_DIR"]) / instance
    state = Path(cmd("systemd-path", "user-state-private")) / instance
    require(not state.exists() and not runtime.exists(), "Target state must be fresh")

    # ponytail: ports are released before startup; the launcher rejects collisions.
    with socket.socket() as web, socket.socket() as api, socket.socket() as pg:
        for sock in (web, api, pg):
            sock.bind((HOST, 0))
        ports = tuple(sock.getsockname()[1] for sock in (web, api, pg))
    launch_env = env | {
        f"PERLIMEN_LITE_{part}_PORT": str(port)
        for part, port in zip(("WEB", "API", "PG"), ports, strict=True)
    }
    print(f"target={target} unit={unit} ports={ports} state={state}", flush=True)

    def show(prop: str) -> str:
        return cmd("systemctl", "--user", "show", unit, "--value", "-p", prop)

    def listeners(port: int) -> list[str]:
        return cmd("ss", "-H", "-ltnp", f"sport = :{port}").splitlines()

    require(show("LoadState") == "not-found", "Target unit already exists")

    def stop() -> None:
        group = show("ControlGroup")
        if show("LoadState") != "not-found":
            cmd("systemctl", "--user", "stop", unit, timeout=40)
        require(not group or not Path(f"/sys/fs/cgroup{group}").exists(), "Cgroup still present")
        require(not runtime.exists(), "Target runtime directory still present")
        require(not any(map(listeners, ports)), "Target ports still in use")
        print(f"stopped {unit}: no cgroup, runtime directory or listeners", flush=True)

    def start(sha: str, *, install: bool = False) -> None:
        # Never switch code while serving, and never reuse the previous Next output.
        stop()
        cmd("git", "switch", "--detach", sha)
        dist = target / "apps/web/.next"
        if dist.exists():
            shutil.rmtree(dist)
        if install:
            cmd(
                "bun",
                "install",
                "--frozen-lockfile",
                "--ignore-scripts",
                cwd=target / "apps/web",
                timeout=180,
            )
        cmd("bash", "-p", "ops/lite.sh", "start", env=launch_env, timeout=255)

    def verify(sha: str, phase: str) -> None:
        group = show("ControlGroup")
        require(show("ActiveState") == "active" and group.startswith("/"), "Target unit not ready")
        members = Path(f"/sys/fs/cgroup{group}/cgroup.procs").read_text().split()
        pids = []
        for port in ports:
            rows = listeners(port)
            require(
                len(rows) == 1 and rows[0].split()[3] == f"{HOST}:{port}",
                f"Expected exactly one loopback listener on {port}",
            )
            match = re.search(r"\bpid=(\d+),", rows[0])
            if match is None or match[1] not in members:
                raise RuntimeError(f"Listener on {port} is not in the target unit")
            pids.append(match[1])
        for pid, cwd in zip(pids[:2], (target / "apps/web", target), strict=True):
            live = Path(f"/proc/{pid}/cwd")
            require(live.resolve(strict=True) == cwd, "Listener checkout mismatch")
            require(cmd("git", "rev-parse", "HEAD", cwd=live) == sha, "Live process SHA mismatch")
        require(not cmd("git", "status", "--porcelain", "-uno"), "Running checkout has edits")
        for port, path in ((ports[0], "/"), (ports[1], "/readyz")):
            with closing(HTTPConnection(HOST, port, timeout=10)) as client:
                client.request("GET", path)
                require(client.getresponse().status == 200, f"Not ready: {port}{path}")
        require((state / "media").is_dir(), "Expected checkout-specific state directory")
        print(
            f"{phase}: sha={sha} web/api/pg_listener_pids={','.join(pids)} "
            "web=200 api_ready=200 loopback=only",
            flush=True,
        )

    gate(baseline)
    try:
        start(baseline, install=True)
        verify(baseline, "baseline")
        password = f"Aa1!{secrets.token_urlsafe(24)}"
        fixture, _ = auth(ports[0], "register", password)
        print("baseline: synthetic registration=201", flush=True)

        def login(phase: str) -> None:
            account, path = auth(ports[0], "login", password)
            require(account == fixture, "Synthetic fixture did not survive transition")
            print(f"{phase}: BFF=200 account={account} refresh_cookie_path={path}", flush=True)

        login("baseline")
        gate(candidate)
        try:
            start(candidate)
            verify(candidate, "promotion")
            login("promotion")
        finally:
            start(baseline)
            verify(baseline, "rollback")
            login("rollback")
    finally:
        stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable", action="store_true", required=True)
    parser.add_argument("baseline", help="full SHA of a known-good main push")
    parser.add_argument("candidate", help="full SHA from the main push to replay")
    args = parser.parse_args()
    a, b = args.baseline, args.candidate
    if os.geteuid() == 0 or a == b or not all(re.fullmatch(r"[0-9a-f]{40}", s) for s in (a, b)):
        parser.error("Run unprivileged with two distinct full lowercase commit SHAs")
    allowed = (
        "HOME",
        "USER",
        "LOGNAME",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_STATE_HOME",
    )
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    env.update(
        PATH=f"{Path.home()}/.bun/bin:{Path.home()}/.local/bin:{os.defpath}",
        LANG="C.UTF-8",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    if "XDG_RUNTIME_DIR" not in env:
        parser.error("A systemd user session is required")
    cmd = partial(run, env=env)
    try:
        cmd("git", "merge-base", "--is-ancestor", a, b)
        cmd("git", "merge-base", "--is-ancestor", b, "refs/remotes/origin/main")
    except subprocess.CalledProcessError:
        parser.error("Require baseline < candidate <= origin/main in Git ancestry")
    # No schema or lockfile transition, not even an additive change.
    locked = (
        ":(glob)**/migrations/**",
        "perlimen_lite/database.py",
        "perlimen_lite/migrate.py",
        "apps/web/bun.lock",
        "uv.lock",
        ":(glob)**/pyproject.toml",
        "apps/web/package.json",
    )
    try:
        cmd("git", "diff", "--no-ext-diff", "--quiet", a, b, "--", *locked)
    except subprocess.CalledProcessError:
        parser.error("Migration or dependency inputs differ; refusing reversible promotion")
    plans = ROOT / "docs/plans"
    plans.mkdir(exist_ok=True)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    with TemporaryDirectory(prefix="lite-rollout-", dir=plans) as tmp:
        target = Path(tmp) / "target"
        cmd("git", "clone", "--shared", "--no-checkout", "--template=", str(ROOT), str(target))
        drill(target, a, b, env)


if __name__ == "__main__":
    main()
