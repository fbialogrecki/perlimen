"""Provision the dedicated Lite database and apply each owner's migrations."""

import logging

import psycopg
from alembic import command
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sqlalchemy.engine import URL

from perlimen_lite.database import (
    BOOTSTRAP,
    CONNECT_ARGS,
    DATABASE,
    DOMAINS,
    endpoint,
    engine_for,
    migration_config,
    password,
    required,
    role,
)


def provision(conn: psycopg.Connection[tuple[object, ...]], secrets: dict[str, str]) -> None:
    # Refuse a lookalike database whose owner or namespaces don't match this profile.
    if (
        not conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = current_database() "
            "AND datdba = (SELECT oid FROM pg_roles WHERE rolname = current_user)"
        ).fetchone()
        or conn.execute(
            "SELECT 1 FROM pg_namespace WHERE left(nspname, 3) <> 'pg_' "
            "AND NOT (nspname = ANY(%s))",
            (["public", "information_schema", *DOMAINS],),
        ).fetchone()
        or conn.execute(
            "SELECT 1 FROM pg_class WHERE relnamespace = 'public'::regnamespace"
        ).fetchone()
    ):
        raise RuntimeError("not a dedicated Lite database")
    conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(DATABASE)))
    conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    for name, value in secrets.items():
        attributes = conn.execute(
            "SELECT rolcanlogin AND NOT (rolsuper OR rolinherit OR rolcreaterole "
            "OR rolcreatedb OR rolreplication OR rolbypassrls) AND NOT EXISTS "
            "(SELECT 1 FROM pg_auth_members WHERE member = pg_roles.oid) "
            "FROM pg_roles WHERE rolname = %s",
            (name,),
        ).fetchone()
        ident = sql.Identifier(name)
        if attributes is None:
            verifier = conn.pgconn.encrypt_password(value.encode(), name.encode(), b"scram-sha-256")
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}").format(
                    ident, sql.Literal(verifier.decode())
                )
            )
        elif not attributes[0]:
            raise RuntimeError(f"unexpected privileges for Lite role {name}")
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(DATABASE), ident)
        )
    for domain in DOMAINS:
        owner = role(domain, "migration")
        existing = conn.execute(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = %s", (domain,)
        ).fetchone()
        namespace = sql.Identifier(domain)
        if existing is None:
            conn.execute(
                sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                    namespace, sql.Identifier(owner)
                )
            )
        elif existing[0] != owner:
            raise RuntimeError(f"unexpected owner of Lite schema {domain}")
        conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(namespace))
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                namespace, sql.Identifier(role(domain, "runtime"))
            )
        )


def upgrade(url: URL, domain: str) -> None:
    engine = engine_for(url, domain, "migration")
    # Both identifiers below are derived from the fixed list of domains.
    runtime = role(domain, "runtime")
    with engine.begin() as conn:
        config = migration_config(domain)
        config.attributes.update(connection=conn, version_table_schema=domain)
        command.upgrade(config, "head")
        conn.exec_driver_sql(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {domain} TO {runtime}"
        )
        conn.exec_driver_sql(
            f"REVOKE INSERT, UPDATE, DELETE ON {domain}.alembic_version FROM {runtime}"
        )


def main() -> None:
    logging.basicConfig(format="%(levelname)s [%(name)s] %(message)s", level=logging.INFO)
    url = endpoint()
    bootstrap = required("PERLIMEN_LITE_BOOTSTRAP_PASSWORD")
    secrets = {role(d, "runtime"): password(d, "runtime") for d in DOMAINS}
    secrets.update({role(d, "migration"): password(d, "migration") for d in DOMAINS})
    if len({bootstrap, *secrets.values()}) != len(secrets) + 1:
        raise ValueError("Lite database roles require distinct passwords")
    conninfo = make_conninfo(
        "",
        host=url.host,
        port=url.port,
        dbname=DATABASE,
        user=BOOTSTRAP,
        options="-c search_path=pg_catalog -c lock_timeout=5000",
        **CONNECT_ARGS,
    )
    with psycopg.connect(conninfo, password=bootstrap, autocommit=True) as conn:
        # Keep one session lock across the separate role/DDL transactions.
        conn.execute("SELECT pg_advisory_lock(497294182683423321)")
        with conn.transaction():
            provision(conn, secrets)
        for domain in DOMAINS:
            logging.info("Migrating Lite schema %s", domain)
            upgrade(url, domain)


if __name__ == "__main__":
    main()
