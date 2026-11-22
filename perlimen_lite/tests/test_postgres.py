"""Opt-in integration test. Requires a pristine, disposable Lite Postgres."""

import os
import secrets
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import psycopg
import pytest
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from media.domain.models import AccountErasureTombstone, MediaAsset, MediaDerivative
from perlimen_lite.database import (
    BOOTSTRAP,
    DATABASE,
    DOMAINS,
    endpoint,
    engine_for,
    migration_config,
    required,
    role,
)
from PIL import Image
from sqlalchemy import Connection, Engine, event, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

TABLES = {
    "users": "application_users",
    "social": "posts",
    "events": "events",
    "media": "media_assets",
}


def assert_denied(conn: Connection, statement: str) -> None:
    with pytest.raises(ProgrammingError) as exc, conn.begin_nested():
        conn.exec_driver_sql(statement)
    assert isinstance(exc.value.orig, psycopg.errors.InsufficientPrivilege)


def assert_media_erasure_retry(client: TestClient, root: Path) -> None:
    png = BytesIO()
    Image.new("RGB", (32, 24)).save(png, "PNG")
    owner = "synthetic-media-owner"

    def upload() -> int:
        return client.post(
            "/media/v1/assets/upload",
            data={"context": "post_image"},
            files={"file": ("fixture.png", png.getvalue(), "image/png")},
            headers={"X-Threshold-User-Id": owner},
        ).status_code

    path = "/media/internal/v1/account-erasure"
    assert upload() == 201
    blobs = set(root.iterdir())
    assert len(blobs) == 3 and all(blob.is_file() for blob in blobs)

    engine = engine_for(endpoint(), "media", "runtime")
    try:
        root.chmod(0o755)
        try:
            assert upload() == 503
            assert client.post(path, json={"user_id": owner}).status_code == 503
        finally:
            root.chmod(0o700)

        with Session(engine) as session:
            assert session.get(AccountErasureTombstone, owner) is not None
            assert len(list(session.scalars(select(MediaAsset)))) == 1
            assert len(list(session.scalars(select(MediaDerivative)))) == 2
        assert set(root.iterdir()) == blobs
        assert client.get("/readyz").status_code == 200
        assert upload() == 409

        for _ in range(2):
            assert client.post(path, json={"user_id": owner}).status_code == 200
        assert not list(root.iterdir())
        with Session(engine) as session:
            assert session.get(AccountErasureTombstone, owner) is not None
            assert session.scalar(select(MediaAsset)) is None
            assert session.scalar(select(MediaDerivative)) is None
    finally:
        engine.dispose()


@pytest.mark.skipif(
    os.getenv("PERLIMEN_LITE_TEST_FRESH_POSTGRES") != "1",
    reason="requires dedicated fresh Postgres",
)
def test_migrate_and_serve_fresh_postgres(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = endpoint()
    with psycopg.connect(
        host=url.host,
        port=url.port,
        dbname=DATABASE,
        user=BOOTSTRAP,
        password=required("PERLIMEN_LITE_BOOTSTRAP_PASSWORD"),
        connect_timeout=5,
    ) as conn:
        assert not conn.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname NOT IN ('information_schema', 'public') "
            "AND left(nspname, 3) <> 'pg_'"
        ).fetchall(), "test requires fresh Postgres"
        assert not conn.execute("SELECT 1 FROM pg_tables WHERE schemaname = 'public'").fetchall(), (
            "test requires an empty public schema"
        )

    # Both CLI calls target the same database; the second must be a no-op for revisions.
    monkeypatch.setenv("PGHOSTADDR", "127.0.0.2")
    for first_run in (True, False):
        run = subprocess.run(
            [sys.executable, "-m", "perlimen_lite.migrate"],
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert run.returncode == 0, run.stderr
        assert ("Running upgrade" in run.stderr) is first_run

    for domain, relation in (
        ("users", "safety_audit_logs"),
        ("social", "safety_audit_logs"),
        ("social", "account_erasure_tombstones"),
        ("events", "account_erasure_tombstones"),
        ("media", "account_erasure_tombstones"),
    ):
        engine = engine_for(url, domain, "runtime")
        with engine.connect() as conn:
            assert (
                conn.exec_driver_sql(f"SELECT count(*) FROM {domain}.{relation}").scalar_one() == 0
            )
        engine.dispose()

    for domain in DOMAINS:
        engine = engine_for(url, domain, "runtime")
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT current_schema, current_user").one() == (
                domain,
                role(domain, "runtime"),
            )
            head = ScriptDirectory.from_config(migration_config(domain)).get_current_head()
            assert (
                conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == head
            )
            if domain == "social":
                assert (
                    conn.exec_driver_sql("SELECT to_regclass('social.user_blocks')").scalar()
                    is None
                )
            own = TABLES[domain]
            assert conn.exec_driver_sql(f"SELECT count(*) FROM {own}").scalar_one() == 0
            conn.exec_driver_sql(f"DELETE FROM {own} WHERE false")
            assert_denied(conn, f"CREATE TABLE {domain}.unwanted_ddl (id integer)")
            assert_denied(conn, f"ALTER TABLE {own} ADD COLUMN unwanted_ddl integer")
            assert_denied(conn, "CREATE TABLE public.unwanted_ddl (id integer)")
            assert_denied(conn, "CREATE TEMP TABLE unwanted_ddl (id integer)")
            assert_denied(conn, "DELETE FROM alembic_version")
            assert_denied(conn, f"SET ROLE {role(domain, 'migration')}")
            for sibling in set(DOMAINS) - {domain}:
                foreign = f"{sibling}.{TABLES[sibling]}"
                assert_denied(conn, f"SELECT 1 FROM {foreign}")
                assert_denied(conn, f"DELETE FROM {foreign} WHERE false")
                assert_denied(conn, f"SELECT 1 FROM {own} CROSS JOIN {foreign}")
        engine.dispose()

    # app must not need bootstrap or migration credentials after provisioning.
    monkeypatch.delenv("PERLIMEN_LITE_BOOTSTRAP_PASSWORD")
    for domain in DOMAINS:
        monkeypatch.delenv(f"PERLIMEN_LITE_{domain.upper()}_MIGRATION_PASSWORD")
    root = tmp_path / "media"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("PERLIMEN_LITE_MEDIA_ROOT", str(root))
    monkeypatch.setenv("THRESHOLD_INTERNAL_TOKEN", "lite-smoke-test-only")
    from perlimen_lite.main import app

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        root.chmod(0o755)
        assert client.get("/readyz").status_code == 503
        root.chmod(0o700)

        paths = client.get("/openapi.json").json()["paths"]
        for domain in ("users", "social"):
            assert "post" in paths[f"/{domain}/v1/reports"]
            assert "get" in paths[f"/{domain}/v1/moderation/reports"]
        for domain in ("events", "media"):
            assert "post" in paths[f"/{domain}/internal/v1/account-erasure"]
        assert client.post("/v1/reports").status_code == 404
        assert (
            client.get("/social/v1/groups", headers={"X-Threshold-User-Id": "forged"}).status_code
            == 401
        )
        client.headers["X-Threshold-Internal-Token"] = "lite-smoke-test-only"
        groups = client.get("/social/v1/groups")
        assert groups.status_code == 200
        assert "techno-warsaw" in {group["slug"] for group in groups.json()}
        assert client.get("/events/v1/events").status_code == 200
        asset = client.get("/media/internal/v1/assets/test-missing-asset")
        assert asset.status_code == 404 and asset.json()["detail"] == "asset not found"
        assert_media_erasure_retry(client, root)
        auth = client.get("/users/v1/auth/me", headers={"X-Threshold-User-Id": "forged"})
        assert auth.status_code == 401 and len(auth.headers.get_list("set-cookie")) == 2
        auth_path = "/users/v1/auth"
        password = "Test-Only-x9L48Vr"
        registered = client.post(
            f"{auth_path}/register",
            json={
                "email": "lite@example.test",
                "username": "litestage",
                "password": password,
            },
        )
        assert registered.status_code == 201
        assert client.cookies.get("threshold_refresh", path=auth_path)
        assert client.get(f"{auth_path}/me").json()["user"]["username"] == "litestage"

        client.cookies.clear()
        credentials = {"email_or_username": "litestage", "password": password}
        login = client.post(f"{auth_path}/login", json=credentials)
        assert login.status_code == 200
        first_refresh = client.cookies.get("threshold_refresh")
        assert first_refresh
        assert client.post(f"{auth_path}/refresh").status_code == 200
        rotated = client.cookies.get("threshold_refresh", path=auth_path)
        assert rotated and rotated != first_refresh

        outside_auth = client.get("/social/v1/groups")
        assert "threshold_refresh=" not in outside_auth.request.headers.get("cookie", "")
        assert client.post(f"{auth_path}/logout").status_code == 204
        assert not client.cookies
        assert client.get(f"{auth_path}/me").status_code == 401
        assert client.post(f"{auth_path}/refresh").status_code == 401

        assert client.post(f"{auth_path}/login", json=credentials).status_code == 200
        client.cookies.delete("threshold_session", path="/")
        assert client.cookies.get("threshold_refresh", path=auth_path)
        assert client.get(f"{auth_path}/me").status_code == 401
        assert not client.cookies
        check_social_policy(client)


def check_social_policy(client: TestClient) -> None:
    password = f"Fixture-A8!{secrets.token_hex(8)}"

    def account(name: str) -> dict[str, str]:
        client.cookies.clear()
        response = client.post(
            "/users/v1/auth/register",
            json={"email": f"{name}@example.test", "username": name, "password": password},
        )
        assert response.status_code == 201
        return {"X-Threshold-User-Id": response.json()["user"]["id"], "X-Threshold-Username": name}

    viewer = account("queryviewer")
    author = account("queryauthor")
    control = account("querycontrol")
    group = "/social/v1/groups/techno-warsaw"
    posts = "/social/v1/posts"
    feed = "/social/v1/feed"
    for principal in (viewer, author, control):
        assert client.post(f"{group}/membership", headers=principal).status_code == 200
    visible = client.post(
        posts, headers=control, json={"body": "control", "group_slug": "techno-warsaw"}
    )
    assert visible.status_code == 201
    control_id = visible.json()["id"]
    discussion = f"{posts}/{control_id}/comments"
    comment = client.post(discussion, headers=author, json={"body": "Hello @querycontrol"})
    assert comment.status_code == 201
    assert comment.json()["mentions"][0]["target_id"] == control["X-Threshold-User-Id"]
    inbox = client.get("/users/v1/notifications").json()
    assert {"comment.created", "mention.created"} <= {item["type"] for item in inbox}
    assert (
        client.put(
            f"{posts}/{control_id}/reaction", headers=author, json={"kind": "up"}
        ).status_code
        == 200
    )
    assert (
        client.put(f"{posts}/{control_id}/emoji", headers=author, json={"emoji": "🎵"}).status_code
        == 200
    )
    for index in range(8):
        post = client.post(
            posts, headers=author, json={"body": f"post {index}", "group_slug": "techno-warsaw"}
        )
        assert post.status_code == 201
    author_post = post.json()["id"]

    def counted_feed(limit: int) -> tuple[list[str], dict[str, int]]:
        reads: dict[str, int] = {}

        def tally(conn: Connection, _cursor: object, statement: str, *_: object) -> None:
            login = conn.engine.url.username
            if login and statement.lstrip().upper().startswith("SELECT"):
                reads[login] = reads.get(login, 0) + 1

        event.listen(Engine, "before_cursor_execute", tally)
        try:
            response = client.get(feed, headers=viewer, params={"limit": limit})
            assert response.status_code == 200
            return [item["id"] for item in response.json()["items"]], reads
        finally:
            event.remove(Engine, "before_cursor_execute", tally)

    one, small = counted_feed(1)
    many, large = counted_feed(20)
    assert one == [author_post] and len(many) == 9 and many[-1] == control_id
    assert small == large == {"lite_users_runtime": 2, "lite_social_runtime": 8}

    # Control is the current product session. Its block must take effect in both directions.
    assert client.post("/users/v1/me/blocks", json={"username": "queryauthor"}).status_code == 201
    assert [item["id"] for item in client.get(feed, headers=control).json()["items"]] == [
        control_id
    ]
    assert control_id not in {
        item["id"] for item in client.get(feed, headers=author).json()["items"]
    }
    assert client.get(f"{group}/posts", headers=control).json()["items"][0]["id"] == control_id
    assert client.get(f"{posts}/{author_post}", headers=control).status_code == 404
    assert client.get(discussion, headers=control).json() == []
    assert (
        client.post(discussion, headers=author, json={"body": "blocked comment"}).status_code == 403
    )
    assert (
        client.post(posts, headers=author, json={"body": "Hello @QUERYCONTROL"}).status_code == 403
    )
    assert (
        client.patch(
            f"{posts}/{author_post}", headers=author, json={"body": "Hello @querycontrol"}
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"{posts}/{control_id}/reaction", headers=author, json={"kind": "down"}
        ).status_code
        == 404
    )
    assert client.delete("/users/v1/me/blocks/queryauthor").status_code == 204
    assert client.get(feed, headers=control).json()["items"][0]["id"] == author_post
