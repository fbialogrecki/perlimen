import logging

import httpx

from perlimen_common.media_client import (
    MediaAssetRef as MediaAssetRef,
)
from perlimen_common.media_client import (
    MediaAssetValidationError as MediaAssetValidationError,
)
from perlimen_common.media_client import get_media_asset as get_media_asset
from users.settings import Settings

logger = logging.getLogger(__name__)



def erase_media_assets(settings: Settings, user_id: str) -> None:
    if not settings.media_service_url or not settings.perlimen_internal_token:
        raise RuntimeError("media erasure config is missing")
    try:
        response = httpx.post(
            f"{settings.media_service_url.rstrip('/')}/internal/v1/account-erasure",
            headers={"X-Threshold-Internal-Token": settings.perlimen_internal_token},
            json={"user_id": user_id},
            timeout=settings.media_request_timeout_seconds,
        )
        response.raise_for_status()
    except Exception:
        logger.exception("media account erasure request failed")
        raise



def validate_avatar_asset(
    settings: Settings,
    *,
    asset_id: str,
    owner_user_id: str,
    allowed_contexts: set[str],
) -> None:
    asset = get_media_asset(settings, asset_id)
    if asset.owner_user_id != owner_user_id:
        raise MediaAssetValidationError("media asset owner mismatch")
    if asset.context not in allowed_contexts:
        raise MediaAssetValidationError("media asset context mismatch")
    if asset.status != "ready":
        raise MediaAssetValidationError("media asset is not ready")
