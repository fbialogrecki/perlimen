"""Lite-only access policy for the existing media objects."""

from collections.abc import Callable
from typing import Annotated

from events.domain.media_access import public_event_poster
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from media.domain.models import AccountErasureTombstone, MediaAsset, MediaDerivative
from media.main_dependencies import get_session
from social.domain.media_access import visible_post_image
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from users.domain.media_access import media_viewer_blocked, public_avatar

from perlimen_common.media_client import MediaAssetRef, MediaAssetValidationError
from perlimen_lite.media_storage import OBJECT_KEY, LocalMediaStorage

Db = Annotated[Session, Depends(get_session)]
Viewer = Annotated[str | None, Header(alias="X-Threshold-User-Id")]
NO_CACHE = {"Cache-Control": "private, no-store"}


def local_asset_lookup(factory: sessionmaker[Session]) -> Callable[[str], MediaAssetRef]:
    def lookup(asset_id: str) -> MediaAssetRef:
        with factory() as session:
            asset = session.get(MediaAsset, asset_id)
            if (
                asset is None
                or session.get(AccountErasureTombstone, asset.owner_user_id) is not None
            ):
                raise MediaAssetValidationError("media asset not found")
            return MediaAssetRef(asset.id, asset.owner_user_id, asset.context, asset.status)

    return lookup


def missing() -> HTTPException:
    return HTTPException(status_code=404, detail="asset not found", headers=NO_CACHE)


def media_access_router(
    factories: dict[str, sessionmaker[Session]], storage: LocalMediaStorage
) -> APIRouter:
    router = APIRouter()

    def can_read(asset: MediaAsset, viewer: str | None) -> bool:
        if viewer == asset.owner_user_id:
            return True
        if asset.context in {"user_avatar", "page_avatar"}:
            with factories["users"]() as users:
                return public_avatar(
                    users, asset_id=asset.id, owner=asset.owner_user_id, context=asset.context
                )
        if asset.context == "event_poster":
            with factories["events"]() as events:
                return public_event_poster(events, asset_id=asset.id)
        if asset.context == "post_image" and viewer:
            with factories["users"]() as users:
                if media_viewer_blocked(users, viewer=viewer, owner=asset.owner_user_id):
                    return False
            with factories["social"]() as social:
                return visible_post_image(social, asset_id=asset.id, owner=asset.owner_user_id)
        return False

    @router.get("/media/assets/{asset_path:path}")
    def read(asset_path: str, session: Db, viewer: Viewer = None) -> Response:
        match = OBJECT_KEY.fullmatch(asset_path)
        if match is None or match[2] == "original":
            raise missing()
        asset = session.get(MediaAsset, match[1])
        if (
            asset is None
            or asset.status != "ready"
            or session.get(AccountErasureTombstone, asset.owner_user_id) is not None
            or not can_read(asset, viewer)
        ):
            raise missing()
        derivative = session.scalar(
            select(MediaDerivative).where(
                MediaDerivative.asset_id == asset.id,
                MediaDerivative.object_key == asset_path,
            )
        )
        if derivative is None:
            raise missing()
        try:
            body, content_type = storage.get_object(
                bucket=derivative.bucket, key=derivative.object_key
            )
        except KeyError:
            raise missing() from None
        except OSError:
            raise HTTPException(
                status_code=503, detail="media storage unavailable", headers=NO_CACHE
            ) from None
        return Response(body, media_type=content_type, headers=NO_CACHE)

    return router
