from collections.abc import Generator, Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def lock_user_write_domains(session: Session, user_ids: Iterable[str]) -> None:
    if session.get_bind().dialect.name == "postgresql":
        for user_id in sorted(set(user_ids)):
            session.execute(
                text("select pg_advisory_xact_lock(hashtextextended(:user_id, 1431520594))"),
                {"user_id": user_id},
            )


def build_engine(database_url: str) -> Engine:
    if database_url == "sqlite+pysqlite:///:memory:":
        return create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    return create_engine(database_url, pool_pre_ping=True)


def build_session_factory(database_url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=build_engine(database_url), expire_on_commit=False)


def session_scope(session_factory: sessionmaker[Session]) -> Generator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
