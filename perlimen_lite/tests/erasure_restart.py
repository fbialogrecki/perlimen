"""Account erasure across a hard restart of the real Lite API process."""

import os
import secrets
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from time import monotonic, sleep

import httpx
from media.domain.models import AccountErasureTombstone, MediaAsset, MediaDerivative
from perlimen_lite.database import endpoint, engine_for, required
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session
from users.domain.models import AccountErasureJob, ApplicationUser, PasswordResetToken, UserSession


def wait_until[T](probe: Callable[[], T | None], timeout: float = 10) -> T:
    deadline = monotonic() + timeout
    while True:
        value = probe()
        if value is not None:
            return value
        assert monotonic() < deadline, "expected persisted state not reached"
        sleep(0.05)


@contextmanager
def serve_lite(client: httpx.Client, port: int) -> Iterator[subprocess.Popen[bytes]]:
    with subprocess.Popen(
        [sys.executable, "-m", "perlimen_lite.serve", str(port)],
        env={
            **os.environ,
            "THRESHOLD_ACCOUNT_ERASURE_WORKER_ENABLED": "true",
            "THRESHOLD_ACCOUNT_ERASURE_POLL_SECONDS": "0.1",
            "THRESHOLD_ACCOUNT_ERASURE_LEASE_SECONDS": "10",
            "THRESHOLD_AUTH_COOKIE_SECURE": "false",
            "THRESHOLD_SMTP_ENABLED": "false",
        },
    ) as process:

        def ready() -> bool | None:
            assert process.poll() is None, "Lite exited before readiness"
            try:
                return True if client.get("/readyz").status_code == 200 else None
            except httpx.ConnectError:
                return None

        try:
            wait_until(ready)
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def check_erasure_restart(root: Path) -> None:
    from media.api.routes import _acquire_owner_write_lock

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    auth = "/users/v1/auth"
    posts = "/social/v1/posts"
    group = "techno-warsaw"
    follow = "/events/v1/events/pgdoor-event/follow"
    users_engine = engine_for(endpoint(), "users", "runtime")
    media_engine = engine_for(endpoint(), "media", "runtime")
    png = BytesIO()
    Image.new("RGB", (32, 24)).save(png, "PNG")
    password = f"Fixture-A8!{secrets.token_hex(8)}"

    def connect() -> httpx.Client:
        return httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"X-Threshold-Internal-Token": required("THRESHOLD_INTERNAL_TOKEN")},
            trust_env=False,
        )

    def register(client: httpx.Client, username: str) -> dict[str, str]:
        response = client.post(
            f"{auth}/register",
            json={"email": f"{username}@example.test", "username": username, "password": password},
        )
        assert response.status_code == 201
        return {
            "X-Threshold-User-Id": response.json()["user"]["id"],
            "X-Threshold-Username": username,
        }

    def upload(client: httpx.Client, principal: dict[str, str]) -> httpx.Response:
        return client.post(
            "/media/v1/assets/upload",
            headers=principal,
            data={"context": "post_image"},
            files={"file": ("fixture.png", png.getvalue(), "image/png")},
        )

    def job_at(stage: str, *, leased: bool = False) -> AccountErasureJob | None:
        with Session(users_engine) as session:
            job = session.scalar(
                select(AccountErasureJob).where(AccountErasureJob.user_id == owner_id)
            )
            if job is not None and job.current_stage == stage and bool(job.lease_owner) == leased:
                return job
        return None

    try:
        with connect() as owner, connect() as other, connect() as second_session:
            username = "erasure-fixture"
            login = {"email_or_username": username, "password": password}
            with serve_lite(owner, port) as original_process:
                principal = register(owner, username)
                owner_id = principal["X-Threshold-User-Id"]
                control = register(other, "erasure-control")
                assert second_session.post(f"{auth}/login", json=login).status_code == 200
                for client, identity in ((owner, principal), (other, control)):
                    assert client.get(f"{auth}/me").status_code == 200
                    assert (
                        client.post(
                            f"/social/v1/groups/{group}/membership", headers=identity
                        ).status_code
                        == 200
                    )
                    assert client.post(follow, headers=identity).status_code == 200

                asset = upload(owner, principal)
                keep = upload(other, control)
                assert asset.status_code == keep.status_code == 201
                asset_id = asset.json()["id"]
                keep_id = keep.json()["id"]
                blobs = set(root.glob(f"{asset_id}.*"))
                control_blobs = set(root.glob(f"{keep_id}.*"))
                assert {p.suffix for p in blobs} == {".original", ".webp"}
                assert len(blobs) == len(control_blobs) == 3
                post = owner.post(
                    posts,
                    headers=principal,
                    json={
                        "group_slug": group,
                        "body": "Preserved thread",
                        "media_asset_ids": [asset_id],
                    },
                )
                assert post.status_code == 201
                thread = f"{posts}/{post.json()['id']}"
                for client, identity in ((owner, principal), (other, control)):
                    assert (
                        client.post(
                            f"{thread}/comments", headers=identity, json={"body": "Preserved reply"}
                        ).status_code
                        == 201
                    )
                before = other.get(thread, headers=control)
                assert before.status_code == 200 and before.json()["author_user_id"] == owner_id

                root.chmod(0o755)
                assert owner.delete("/users/v1/me").status_code == 202
                assert not owner.cookies
                old_refresh = second_session.cookies["threshold_refresh"]
                assert second_session.get(f"{auth}/me").status_code == 401
                assert (
                    second_session.post(
                        f"{auth}/refresh", headers={"Cookie": f"threshold_refresh={old_refresh}"}
                    ).status_code
                    == 401
                )
                assert owner.post(f"{auth}/login", json=login).status_code == 401
                assert (
                    owner.post(
                        f"{auth}/password/reset/request", json={"email": f"{username}@example.test"}
                    ).status_code
                    == 200
                )

                failed = wait_until(lambda: job_at("media"))
                assert failed.attempt_count > 0 and failed.completed_at is None
                assert all(blob.is_file() for blob in blobs)
                with Session(users_engine) as session:
                    user = session.get(ApplicationUser, owner_id)
                    assert user is not None and user.status == "erasure_pending"
                    assert (
                        session.scalar(select(UserSession).where(UserSession.user_id == owner_id))
                        is None
                    )
                    assert (
                        session.scalar(
                            select(PasswordResetToken).where(PasswordResetToken.user_id == owner_id)
                        )
                        is None
                    )
                with Session(media_engine) as session:
                    assert session.get(AccountErasureTombstone, owner_id) is not None
                    assert session.get(MediaAsset, asset_id) is not None
                    # Park the retry behind the Media owner lock, then kill its lease holder.
                    _acquire_owner_write_lock(owner_user_id=owner_id, session=session)
                    claimed = wait_until(lambda: job_at("media", leased=True))
                    assert claimed.id == failed.id and claimed.completed_at is None
                    original_process.kill()
                    assert original_process.wait(timeout=5) == -9

            root.chmod(0o700)
            with serve_lite(other, port) as restarted:
                assert restarted.pid != original_process.pid
                assert upload(owner, principal).status_code == 409
                assert (
                    owner.post(
                        f"{thread}/comments", headers=principal, json={"body": "Resurrection"}
                    ).status_code
                    == 410
                )
                assert owner.post(follow, headers=principal).status_code == 409
                complete = wait_until(lambda: job_at("completed"), timeout=15)
                assert complete.id == failed.id and complete.completed_at is not None
                assert not any(blob.exists() for blob in blobs)
                assert all(blob.is_file() for blob in control_blobs)
                with Session(media_engine) as session:
                    assert session.get(MediaAsset, asset_id) is None
                    assert not session.scalars(
                        select(MediaDerivative).where(MediaDerivative.asset_id == asset_id)
                    ).all()
                    assert session.get(MediaAsset, keep_id) is not None
                with Session(users_engine) as session:
                    user = session.get(ApplicationUser, owner_id)
                    assert user is not None and user.status == "deleted"
                    assert user.username is None and user.email is None and user.credential is None
                assert other.get(f"{auth}/me").status_code == 200
                after = other.get(thread, headers=control)
                replies = other.get(f"{thread}/comments", headers=control)
                assert after.status_code == replies.status_code == 200
                assert (
                    after.json()["body"] == "Preserved thread"
                    and after.json()["media_asset_ids"] == []
                )
                assert [r["body"] for r in replies.json()] == ["Preserved reply"] * 2
                assert {r["author_user_id"] for r in replies.json()} == {
                    "deleted-user",
                    control["X-Threshold-User-Id"],
                }
                assert after.json()["author_display_name"] == "Deleted User"
                assert all(
                    marker not in after.text + replies.text
                    for marker in (owner_id, username, asset_id)
                )
    finally:
        root.chmod(0o700)
        users_engine.dispose()
        media_engine.dispose()
