"""Users-owned inputs to social visibility. No Social table is involved."""

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from users.domain.models import ApplicationUser, Follow, UserBlock


def followed_users(session: Session, viewer_id: str) -> set[str]:
    return set(
        session.scalars(
            select(Follow.target_id)
            .join(ApplicationUser, ApplicationUser.id == Follow.target_id)
            .where(
                Follow.follower_user_id == viewer_id,
                Follow.target_type.in_(("consumer", "artist")),
                ApplicationUser.status == "active",
            )
            .distinct()
        )
    )


def block_ids(session: Session, viewer_id: str) -> tuple[set[str], set[str]]:
    outgoing: set[str] = set()
    incoming: set[str] = set()
    for blocker, blocked in session.execute(
        select(UserBlock.blocker_user_id, UserBlock.blocked_user_id).where(
            or_(UserBlock.blocker_user_id == viewer_id, UserBlock.blocked_user_id == viewer_id)
        )
    ):
        if blocker == viewer_id:
            outgoing.add(blocked)
        else:
            incoming.add(blocker)
    return outgoing, incoming
