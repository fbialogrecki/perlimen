"""One local API; each router keeps its own schema, session and authorization."""

import asyncio
from collections.abc import AsyncIterator, Callable, Generator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic.script import ScriptDirectory
from events.api.routes import erase_account_data as erase_events_account
from events.api.routes import router as events_router
from events.api.schemas import AccountErasureRequest as EventsErasureRequest
from events.owner import get_event_owner
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from media.api.routes import erase_account_data as erase_media_account
from media.api.routes import router as media_router
from media.api.schemas import AccountErasureRequest as MediaErasureRequest
from social.api.routes import anonymize_author
from social.api.routes import router as social_router
from social.api.schemas import AnonymizeAuthorRequest
from social.owner import get_social_owner
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from users.account_erasure import ErasureStages, account_erasure_worker
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
from perlimen_lite.events import LiteEventOwners
from perlimen_lite.media_access import local_asset_lookup, media_access_router
from perlimen_lite.media_storage import LocalMediaStorage
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


def create_app() -> FastAPI:
    url = endpoint()
    required("THRESHOLD_INTERNAL_TOKEN")
    media_root = Path(required("PERLIMEN_LITE_MEDIA_ROOT")).absolute()
    storage = LocalMediaStorage(media_root)
    engines = {domain: engine_for(url, domain, "runtime") for domain in DOMAINS}
    factories = {
        d: sessionmaker(bind=engine, expire_on_commit=False) for d, engine in engines.items()
    }
    heads = {d: set(ScriptDirectory.from_config(migration_config(d)).get_heads()) for d in DOMAINS}

    # Legacy transports have no place in this composition. Owner adapters are
    # added without delegating to localhost or changing the standalone apps.
    users.settings.nats_enabled = social.settings.nats_enabled = False
    users.settings.social_service_url = users.settings.events_service_url = None
    users.settings.media_service_url = None
    social.settings.users_service_url = social.settings.events_service_url = None

    events.settings.users_base_url = events.settings.social_service_url = None
    lookup = local_asset_lookup(factories["media"])
    for consumer in (users.settings, social.settings, events.settings):
        consumer.use_local_media(lookup)
    media.settings.s3_bucket = "lite-local"
    media.settings.upload_temp_dir = str(media_root)
    if users.settings.smtp_enabled and users.settings.smtp_host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Lite SMTP requires an isolated local sender")

    social_owner = LiteSocialOwner(factories["users"], factories["events"])

    def erase_social(user_id: str) -> None:
        with factories["social"]() as session:
            anonymize_author(AnonymizeAuthorRequest(user_id=user_id), session, social_owner)

    def erase_events(user_id: str, artist_profile_ids: list[str]) -> None:
        with factories["events"]() as session:
            erase_events_account(
                EventsErasureRequest(user_id=user_id, artist_profile_ids=artist_profile_ids),
                session,
            )

    def erase_media(user_id: str) -> None:
        with factories["media"]() as session:
            erase_media_account(MediaErasureRequest(user_id=user_id), session, storage)

    stages = ErasureStages(social=erase_social, events=erase_events, media=erase_media)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        try:
            async with asyncio.TaskGroup() as tasks:
                if users.settings.account_erasure_worker_enabled:
                    tasks.create_task(
                        account_erasure_worker(
                            factories["users"], users.settings, stop, stages=stages
                        )
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
    app.dependency_overrides[get_social_owner] = lambda: social_owner
    event_owner = LiteEventOwners(factories["users"], factories["social"]).port()
    app.dependency_overrides[get_event_owner] = lambda: event_owner

    def local_upload_storage() -> LocalMediaStorage:
        if not storage.ready():
            raise HTTPException(status_code=503, detail="private media directory is not ready")
        return storage

    # Erasure resolves the adapter without I/O so it can commit the fence first.
    app.dependency_overrides[media.get_object_storage] = lambda: storage
    app.dependency_overrides[media.get_upload_storage] = local_upload_storage
    app.include_router(
        media_access_router(factories, storage),
        prefix="/media",
        dependencies=[Depends(require_internal_token)],
    )
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
        if not storage.ready():
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
