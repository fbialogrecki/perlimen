"""One local API; each router keeps its own schema, session and authorization."""

import asyncio
import os
import stat
from collections.abc import AsyncIterator, Callable, Generator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic.script import ScriptDirectory
from events.api.routes import router as events_router
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from media.api.routes import router as media_router
from social.api.routes import router as social_router
from social.owner import get_social_owner
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from users.account_erasure import account_erasure_worker
from users.api.routes import (
    SessionAuthenticationError,
    require_internal_token,
    session_authentication_error,
)
from users.api.routes import (
    router as users_router,
)

from events import main_dependencies as events
from media import main_dependencies as media
from perlimen_common.health import ok
from perlimen_lite.database import DOMAINS, endpoint, engine_for, migration_config, required, role
from perlimen_lite.social import LiteSocialOwner
from social import main_dependencies as social
from users import main_dependencies as users

SERVICE = "perlimen-lite"
SessionDependency = Callable[[], Generator[Session]]
MODULES: dict[str, tuple[APIRouter, SessionDependency]] = {
    "users": (users_router, users.get_db_session),
    "social": (social_router, social.get_db_session),
    "events": (events_router, events.get_db_session),
    "media": (media_router, media.get_session),
}


def database_session(factory: sessionmaker[Session]) -> SessionDependency:
    def acquire() -> Generator[Session]:
        with factory() as session:
            yield session

    return acquire


def private_media_ready(root: Path) -> bool:
    try:
        info = root.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o700
        and info.st_uid == os.getuid()
        and os.access(root, os.R_OK | os.W_OK | os.X_OK)
    )


def unavailable_blob_storage() -> None:
    # Do not reach the legacy S3 endpoint; the local adapter is a separate slice.
    raise HTTPException(status_code=503, detail="local blob storage is not configured")


def create_app() -> FastAPI:
    url = endpoint()
    required("THRESHOLD_INTERNAL_TOKEN")
    media_root = Path(required("PERLIMEN_LITE_MEDIA_ROOT")).absolute()
    engines = {domain: engine_for(url, domain, "runtime") for domain in DOMAINS}
    factories = {
        d: sessionmaker(bind=engine, expire_on_commit=False) for d, engine in engines.items()
    }
    heads = {d: set(ScriptDirectory.from_config(migration_config(d)).get_heads()) for d in DOMAINS}

    # Legacy transports have no place in this composition. Owner adapters are
    # added without delegating to localhost or changing the standalone apps.
    users.settings.nats_enabled = social.settings.nats_enabled = False
    users.settings.social_service_url = users.settings.events_service_url = None
    social.settings.users_service_url = social.settings.events_service_url = None
    users.settings.media_service_url = social.settings.media_service_url = None
    events.settings.users_base_url = events.settings.social_service_url = None
    events.settings.media_service_url = None
    media.settings.upload_temp_dir = str(media_root)
    if users.settings.smtp_enabled and users.settings.smtp_host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Lite SMTP requires an isolated local sender")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        try:
            async with asyncio.TaskGroup() as tasks:
                if users.settings.account_erasure_worker_enabled:
                    tasks.create_task(
                        account_erasure_worker(factories["users"], users.settings, stop)
                    )
                try:
                    yield
                finally:
                    stop.set()
        finally:
            for engine in engines.values():
                engine.dispose()

    app = FastAPI(title="Perlimen Lite", lifespan=lifespan)
    app.add_exception_handler(SessionAuthenticationError, session_authentication_error)
    app.dependency_overrides[media.get_object_storage] = unavailable_blob_storage
    social_owner = LiteSocialOwner(factories["users"], factories["events"])
    app.dependency_overrides[get_social_owner] = lambda: social_owner
    for domain, (router, session) in MODULES.items():
        app.include_router(
            router, prefix=f"/{domain}", dependencies=[Depends(require_internal_token)]
        )
        app.dependency_overrides[session] = database_session(factories[domain])

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return ok(SERVICE)

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        if not private_media_ready(media_root):
            raise HTTPException(status_code=503, detail="private media directory is not ready")
        for domain, engine in engines.items():
            try:
                with engine.connect() as conn:
                    namespace, login = conn.exec_driver_sql(
                        "SELECT current_schema, current_user"
                    ).one()
                    revisions = set(
                        conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalars()
                    )
            except SQLAlchemyError:
                raise HTTPException(status_code=503, detail="database is not ready") from None
            if (namespace, login) != (domain, role(domain, "runtime")) or revisions != heads[
                domain
            ]:
                raise HTTPException(status_code=503, detail="database is not ready")
        return ok(SERVICE)

    return app


app = create_app()
