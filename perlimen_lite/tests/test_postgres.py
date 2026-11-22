"""Opt-in integration test. Requires a pristine, disposable Lite Postgres."""

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
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
from sqlalchemy import Connection
from sqlalchemy.exc import ProgrammingError

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
        auth = client.get("/users/v1/auth/me", headers={"X-Threshold-User-Id": "forged"})
        assert auth.status_code == 401 and len(auth.headers.get_list("set-cookie")) == 2
        registered = client.post(
            "/users/v1/auth/register",
            json={
                "email": "lite@example.test",
                "username": "litestage",
                "password": "Test-Only-x9L48Vr",
            },
        )
        assert registered.status_code == 201
        assert client.get("/users/v1/auth/me").json()["user"]["username"] == "litestage"
