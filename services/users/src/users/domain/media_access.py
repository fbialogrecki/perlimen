from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from users.domain.models import ApplicationUser, ConsumerProfile, Page, UserBlock


def public_avatar(session: Session, *, asset_id: str, owner: str, context: str) -> bool:
    if context == "page_avatar":
        return (
            session.scalar(
                select(Page.id)
                .where(
                    Page.avatar_media_asset_id == asset_id, Page.avatar_media_owner_user_id == owner
                )
                .limit(1)
            )
            is not None
        )
    return (
        session.scalar(
            select(ConsumerProfile.id)
            .join(ApplicationUser)
            .where(
                ApplicationUser.id == owner,
                ApplicationUser.status == "active",
                ConsumerProfile.avatar_media_asset_id == asset_id,
            )
        )
        is not None
    )


def media_viewer_blocked(session: Session, *, viewer: str, owner: str) -> bool:
    return (
        session.scalar(
            select(UserBlock.id)
            .where(
                or_(
                    (UserBlock.blocker_user_id == owner) & (UserBlock.blocked_user_id == viewer),
                    (UserBlock.blocker_user_id == viewer) & (UserBlock.blocked_user_id == owner),
                )
            )
            .limit(1)
        )
        is not None
    )
