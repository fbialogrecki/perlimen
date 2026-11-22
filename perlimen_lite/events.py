"""Connect Events to Users and Social without sharing sessions or using HTTP."""

import logging
from dataclasses import dataclass
from typing import Unpack

from events.domain.models import Event
from events.owner import EventOwner, Notification
from fastapi import HTTPException, Response
from pydantic import ValidationError
from social.api.routes import create_event_announcement
from social.api.schemas import EventAnnouncementRequest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from users.api.routes import (
    active_artist_refs,
    active_page_role,
    active_user_refs,
    profile_mention_target,
    receive_notification,
)
from users.api.schemas import NotificationCreateRequest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiteEventOwners:
    users: sessionmaker[Session]
    social: sessionmaker[Session]

    def port(self) -> EventOwner:
        return EventOwner(
            check_page_role=self.check_page_role,
            get_artist_ref=lambda artist_id: self.get_artist_refs([artist_id]).get(artist_id),
            get_artist_refs=self.get_artist_refs,
            get_active_user_refs=self.get_active_user_refs,
            get_user_by_username=self.get_user_by_username,
            notify_user=self.notify_user,
            announce_event=self.announce_event,
            retry_announcement_on_conflict=True,
        )

    def check_page_role(self, page_id: str, user_id: str) -> str | None:
        with self.users() as session:
            return active_page_role(session, page_id, user_id)

    def get_artist_refs(self, ids: list[str]) -> dict[str, dict[str, str]]:
        if not ids:
            return {}
        with self.users() as session:
            return {
                ref.artist_profile_id: ref.model_dump()
                for ref in active_artist_refs(session, list(dict.fromkeys(ids))[:100])
            }

    def get_active_user_refs(self, ids: list[str]) -> dict[str, dict[str, str]]:
        if not ids:
            return {}
        with self.users() as session:
            return {
                ref.id: {
                    "user_id": ref.id,
                    "username": ref.username,
                    "display_name": ref.display_name,
                }
                for ref in active_user_refs(session, ids)
            }

    def get_user_by_username(self, username: str) -> dict[str, str] | None:
        with self.users() as session:
            try:
                target = profile_mention_target(session, username)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                return None
            return {
                "user_id": target.target_id,
                "username": target.handle,
                "display_name": target.display_name,
            }

    def notify_user(self, **notice: Unpack[Notification]) -> bool:
        payload = {**notice, "type": notice["event_type"]}
        del payload["event_type"]
        try:
            with self.users() as session:
                receive_notification(
                    session, NotificationCreateRequest.model_validate(payload), Response()
                )
        except (HTTPException, SQLAlchemyError, ValidationError):
            logger.warning("Lite event notification unavailable")
            return False
        return True

    def announce_event(self, event: Event) -> bool:
        try:
            with self.social() as session:
                create_event_announcement(
                    EventAnnouncementRequest(
                        event_id=event.id,
                        event_slug=event.slug,
                        event_title=event.title,
                        city=event.city,
                        page_id=event.page_id,
                        actor_user_id=event.created_by_user_id,
                    ),
                    session,
                    Response(),
                )
        except (HTTPException, SQLAlchemyError, ValidationError):
            logger.warning("Lite event announcement unavailable")
            return False
        return True
