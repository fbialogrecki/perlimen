"""Opt-in integration test. Requires a pristine, disposable Lite Postgres."""

import json
import os
import secrets
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from pathlib import Path
from threading import Barrier

import httpx
import jwt
import nats
import psycopg
import pytest
from alembic.script import ScriptDirectory
from auth_gateway.settings import Settings as GatewaySettings
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
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
from perlimen_lite.tests.erasure_restart import check_erasure_restart, serve_lite
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
    ) as bootstrap_conn:
        assert not bootstrap_conn.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname NOT IN ('information_schema', 'public') "
            "AND left(nspname, 3) <> 'pg_'"
        ).fetchall(), "test requires fresh Postgres"
        assert not bootstrap_conn.execute(
            "SELECT 1 FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall(), "test requires an empty public schema"

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
    from social.main_dependencies import settings as social_settings
    from users.main_dependencies import settings as users_settings

    monkeypatch.setattr(social_settings, "nats_enabled", True)
    monkeypatch.setattr(users_settings, "nats_enabled", True)
    broker_attempts: list[None] = []

    async def unexpected_broker(*args: object, **kwargs: object) -> None:
        broker_attempts.append(None)
        raise AssertionError("Lite attempted a NATS connection")

    monkeypatch.setattr(nats, "connect", unexpected_broker)
    from perlimen_lite.main import app

    check_operator_identity(monkeypatch)
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
        check_events(client)
    assert not broker_attempts

    check_erasure_restart(root)


def check_operator_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    identity = {
        "subject": "isolated-operator-subject",
        "email": "operator@example.test",
        "username": "lite-operator",
    }
    headers = {"X-Threshold-Internal-Token": required("THRESHOLD_INTERNAL_TOKEN")}
    endpoint_path = "/users/internal/v1/operator-identity"
    profile_path = f"/users/v1/profiles/{identity['username']}"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwks = json.dumps({"keys": [{**jwk, "kid": "lite-test"}]}).encode()

    class SyntheticJwks(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            assert self.path == "/jwks"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(jwks)

    with HTTPServer(("127.0.0.1", 0), SyntheticJwks) as issuer, monkeypatch.context() as patch:
        issuer.timeout = 10
        jwks_origin = f"http://127.0.0.1:{issuer.server_port}"
        audience = "lite-admin-test"
        patch.setenv("THRESHOLD_AUTHENTIK_AUDIENCE", audience)
        patch.setenv("no_proxy", "127.0.0.1")
        from auth_gateway import main as gateway

        config = GatewaySettings(
            users_transport="http",
            users_base_url=f"{origin}/users",
            authentik_issuer=jwks_origin,
            authentik_jwks_url=f"{jwks_origin}/jwks",
            authentik_audience=audience,
        )
        token = jwt.encode(
            {
                "iss": jwks_origin,
                "aud": audience,
                "sub": identity["subject"],
                "exp": datetime.now(UTC) + timedelta(minutes=2),
                "email": identity["email"],
                "preferred_username": identity["username"],
                "roles": ["moderator"],
            },
            key,
            algorithm="RS256",
            headers={"kid": "lite-test"},
        )
        bearer = {"Authorization": f"Bearer {token}"}
        patch.setattr(gateway, "jwt_verifier", gateway._build_jwt_verifier(config))
        patch.setattr(gateway, "users_profile_client", gateway._build_users_profile_client(config))
        with (
            httpx.Client(base_url=origin, timeout=5, trust_env=False) as lite,
            serve_lite(lite, port),
            TestClient(gateway.app) as operator,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            pool.submit(issuer.handle_request)
            assert lite.post(endpoint_path, json=identity).status_code == 401
            assert lite.post(endpoint_path, json=identity, headers=bearer).status_code == 401
            extra_claim = lite.post(
                endpoint_path, json={**identity, "roles": ["moderator"]}, headers=headers
            )
            assert extra_claim.status_code == 422
            legacy = lite.post(
                "/users/internal/v1/current-profile",
                json={"authentik_subject": identity["subject"], "username": identity["username"]},
                headers=headers,
            )
            assert legacy.status_code == 404
            for _ in range(2):
                result = operator.get("/me", headers=bearer)
                assert result.status_code == 200
                assert result.json() == {"status": "authenticated", **identity}
                assert "set-cookie" not in result.headers
            assert lite.get(profile_path, headers=headers).status_code == 404
            for path in ("/users/v1/auth/me", "/users/v1/moderation/reports"):
                assert lite.get(path, headers=headers | bearer).status_code == 401

            engine = engine_for(endpoint(), "users", "runtime")
            try:
                with engine.connect() as conn:
                    for table in (
                        "application_users",
                        "consumer_profiles",
                        "onboarding_preferences",
                        "user_credentials",
                        "user_sessions",
                        "page_memberships",
                    ):
                        rows = conn.exec_driver_sql(f"SELECT 1 FROM {table} LIMIT 1")
                        assert rows.first() is None
                registration = lite.post(
                    "/users/v1/auth/register",
                    headers=headers,
                    json={
                        "email": identity["email"],
                        "username": identity["username"],
                        "password": "Test-Only-x9L48Vr",
                    },
                )
                assert registration.status_code == 201, registration.text
                assert operator.get("/me", headers=bearer).json() == {
                    "status": "authenticated",
                    **identity,
                }
                assert lite.get(profile_path, headers=headers).status_code == 200
                with engine.connect() as conn:
                    assert conn.exec_driver_sql(
                        "SELECT username, email, authentik_subject FROM application_users"
                    ).one() == (identity["username"], identity["email"], None)
            finally:
                engine.dispose()


def check_events(client: TestClient) -> None:
    from social.domain.models import Group
    from sqlalchemy.orm import Session

    def account(username: str) -> dict[str, str]:
        client.cookies.clear()
        registered = client.post(
            "/users/v1/auth/register",
            json={
                "username": username,
                "password": f"Fixture-A8!{secrets.token_hex(8)}",
                "email": f"{username}@example.test",
            },
        )
        assert registered.status_code == 201
        return {"X-Threshold-User-Id": registered.json()["user"]["id"]}

    guest = account("pgdoor-guest")
    door = account("pgdoor-staff")
    owner = account("pgdoor-owner")
    city = "Disposable Crosspost City"
    page = client.post(
        "/users/v1/pages",
        json={
            "slug": "pgdoor-page",
            "display_name": "Fixture Room",
            "page_type": "club",
            "city": city,
        },
    )
    assert page.status_code == 201
    payload = {
        "page_id": page.json()["id"],
        "slug": "pgdoor-event",
        "city": city,
        "title": "Fixture night",
        "starts_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "location_mode": "tba",
    }
    events = "/events/v1/events"
    assert client.post(events, json=payload, headers=guest).status_code == 403
    created = client.post(events, json=payload, headers=owner)
    assert created.status_code == 201
    event_id = created.json()["id"]
    social_engine = engine_for(endpoint(), "social", "runtime")
    try:
        with Session(social_engine) as session:
            session.add(Group(slug="pgdoor-city", name="Fixture group", city=city, official=True))
            session.commit()
        assert (
            client.post(
                events, json={**payload, "title": "Not a replay"}, headers=owner
            ).status_code
            == 409
        )
        with social_engine.connect() as conn:
            assert (
                conn.exec_driver_sql(
                    "SELECT count(*) FROM event_announcements WHERE event_id = %s", (event_id,)
                ).scalar_one()
                == 0
            )
        for _ in range(2):
            assert client.post(events, json=payload, headers=owner).status_code == 409
        with social_engine.connect() as conn:
            assert (
                conn.exec_driver_sql(
                    "SELECT count(*) FROM event_announcements WHERE event_id = %s", (event_id,)
                ).scalar_one()
                == 1
            )
            assert (
                conn.exec_driver_sql(
                    "SELECT count(*) FROM posts WHERE event_id = %s AND author_type = 'system'",
                    (event_id,),
                ).scalar_one()
                == 1
            )
    finally:
        social_engine.dispose()

    path = f"{events}/{payload['slug']}"
    guest_id = guest["X-Threshold-User-Id"]
    invited = client.post(
        f"{path}/guestlist",
        headers=owner,
        json={
            "guest_user_id": guest_id,
            "username": "pgdoor-guest",
            "display_name": "PG Fixture Guest",
        },
    )
    assert invited.status_code == 201
    assert client.get(f"{path}/guestlist", headers=door).status_code == 403
    assert guest_id not in client.get(path).text
    assert (
        client.put(f"{path}/door-staff/by-username/pgdoor-staff", headers=owner).status_code == 200
    )
    issued = client.post(f"{path}/guestlist/me/qr-token", headers=guest)
    assert issued.status_code == 201
    token = issued.json()["token"]

    simultaneous = Barrier(2, timeout=5)
    connections: set[int] = set()

    def synchronize_scans(conn: Connection, _cursor: object, statement: str, *_: object) -> None:
        if statement.startswith("SELECT") and "event_check_in_tokens.token_hash =" in statement:
            driver = conn.connection.driver_connection
            assert isinstance(driver, psycopg.Connection)
            connections.add(driver.info.backend_pid)
            simultaneous.wait()

    def scan(headers: dict[str, str]) -> int:
        return client.post(f"{path}/check-in", headers=headers, json={"token": token}).status_code

    event.listen(Engine, "after_cursor_execute", synchronize_scans)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(scan, [owner, door])) == [200, 409]
    finally:
        event.remove(Engine, "after_cursor_execute", synchronize_scans)
    assert len(connections) == 2
    assert client.get(f"{path}/access", headers=guest).json()["checked_in_at"] is not None
    events_engine = engine_for(endpoint(), "events", "runtime")
    try:
        with events_engine.connect() as conn:
            assert (
                conn.exec_driver_sql(
                    "SELECT count(*) FROM event_access_audit_logs "
                    "WHERE event_id = %s AND action = 'guestlist.checked_in'",
                    (event_id,),
                ).scalar_one()
                == 1
            )
            stored_hash, status = conn.exec_driver_sql(
                "SELECT token_hash, status FROM event_check_in_tokens WHERE event_id = %s",
                (event_id,),
            ).one()
            assert status == "used" and stored_hash != token
    finally:
        events_engine.dispose()


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
