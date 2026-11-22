from social.domain.models import Post
from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session


def visible_post_image(session: Session, *, asset_id: str, owner: str) -> bool:
    return (
        session.scalar(
            select(Post.id)
            .where(
                Post.author_user_id == owner,
                Post.hidden_at.is_(None),
                cast(Post.media_asset_ids, JSONB).contains([asset_id]),
            )
            .limit(1)
        )
        is not None
    )
