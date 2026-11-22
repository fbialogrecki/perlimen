"""Wire Social to the owning APIs using their own runtime connections."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from events.api.routes import event_mention_target
from fastapi import HTTPException, Response
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from users.api.routes import page_mention_target, profile_mention_target, receive_notification
from users.api.schemas import NotificationCreateRequest
from users.social_policy import block_ids, followed_users

logger = logging.getLogger(__name__)


@contextmanager
def owner_session(factory: sessionmaker[Session]) -> Iterator[Session]:
    try:
        with factory() as session:
            yield session
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="social policy unavailable") from None


@dataclass(frozen=True)
class LiteSocialOwner:
    users: sessionmaker[Session]
    events: sessionmaker[Session]

    def followed_users(self, viewer_id: str) -> set[str]:
        with owner_session(self.users) as session:
            return followed_users(session, viewer_id)

    def blocks(self, viewer_id: str) -> tuple[set[str], set[str]]:
        with owner_session(self.users) as session:
            return block_ids(session, viewer_id)

    def mention(self, kind: str, handle: str) -> dict[str, str | None] | None:
        if kind == "event":
            with owner_session(self.events) as session:
                try:
                    return event_mention_target(session, handle).model_dump()
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
                    return None
        with owner_session(self.users) as session:
            for resolve in (profile_mention_target, page_mention_target):
                try:
                    return resolve(session, handle).model_dump()
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
        return None

    def notify(self, payload: dict[str, object]) -> None:
        try:
            notice = NotificationCreateRequest.model_validate(payload)
            with owner_session(self.users) as session:
                receive_notification(session, notice, Response())
        except ValidationError:
            logger.warning("invalid Lite social notification")
        except HTTPException as exc:
            # The social write is already committed; inbox delivery is non-fatal.
            if exc.status_code >= 500:
                logger.warning("Lite social notification unavailable")
