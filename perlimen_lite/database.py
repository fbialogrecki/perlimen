"""Connection contract for the dedicated Lite PostgreSQL instance."""

import os
from typing import Literal

from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool

DOMAINS = ("users", "social", "events", "media")
DATABASE = "perlimen_lite"
BOOTSTRAP = "lite_bootstrap"
LOOPBACK = "127.0.0.1"
# Pin the network address too: libpq's PGHOSTADDR can override the URL host.
CONNECT_ARGS: dict[str, str | int] = {
    "hostaddr": LOOPBACK,
    "connect_timeout": 5,
    "require_auth": "scram-sha-256",
    "gssencmode": "disable",
    "sslmode": "disable",
}
type Purpose = Literal["runtime", "migration"]


def required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def endpoint() -> URL:
    name = "PERLIMEN_LITE_DATABASE_URL"
    error = f"{name} must be postgresql://127.0.0.1:<port>/{DATABASE} without credentials"
    try:
        url = make_url(required(name))
    except (ArgumentError, ValueError):
        raise ValueError(error) from None
    if (
        url.drivername != "postgresql"
        or url.host != LOOPBACK
        or url.port is None
        or not 1 <= url.port <= 65535
        or url.database != DATABASE
        or url.username is not None
        or url.password is not None
        or url.query
    ):
        raise ValueError(error)
    return url.set(drivername="postgresql+psycopg")


def role(domain: str, purpose: Purpose) -> str:
    return f"lite_{domain}_{purpose}"


def password(domain: str, purpose: Purpose) -> str:
    return required(f"PERLIMEN_LITE_{domain.upper()}_{purpose.upper()}_PASSWORD")


def engine_for(url: URL, domain: str, purpose: Purpose) -> Engine:
    if domain not in DOMAINS:
        raise ValueError("unknown Lite database schema")
    login = url.set(username=role(domain, purpose), password=password(domain, purpose))
    options = f"-c search_path={domain} -c lock_timeout=5000"
    if purpose == "migration":
        return create_engine(
            login, connect_args={**CONNECT_ARGS, "options": options}, poolclass=NullPool
        )
    options += " -c statement_timeout=30000 -c idle_in_transaction_session_timeout=30000"
    return create_engine(
        login,
        connect_args={**CONNECT_ARGS, "options": options},
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        pool_timeout=5,
    )


def migration_config(domain: str) -> Config:
    config = Config()
    config.set_main_option("script_location", f"{domain}:migrations")
    return config
