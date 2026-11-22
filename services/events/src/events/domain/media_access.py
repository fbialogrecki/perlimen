from events.domain.models import Event
from sqlalchemy import select
from sqlalchemy.orm import Session


def public_event_poster(session: Session, *, asset_id: str) -> bool:
    return (
        session.scalar(
            select(Event.id)
            .where(Event.poster_media_asset_id == asset_id, Event.deleted_at.is_(None))
            .limit(1)
        )
        is not None
    )
