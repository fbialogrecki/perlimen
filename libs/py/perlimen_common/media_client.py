from collections.abc import Callable
from dataclasses import dataclass

import httpx
from pydantic import PrivateAttr

from perlimen_common.config import ServiceSettings


class MediaAssetValidationError(Exception):
    pass


@dataclass(frozen=True)
class MediaAssetRef:
    id: str
    owner_user_id: str
    context: str
    status: str


class MediaConsumerSettings(ServiceSettings):
    media_service_url: str | None
    media_request_timeout_seconds: float
    perlimen_internal_token: str | None
    _media_asset_lookup: Callable[[str], MediaAssetRef] | None = PrivateAttr(default=None)

    def use_local_media(self, lookup: Callable[[str], MediaAssetRef]) -> None:
        self._media_asset_lookup = lookup
        self.media_service_url = None


def get_media_asset(settings: MediaConsumerSettings, asset_id: str) -> MediaAssetRef:
    if settings._media_asset_lookup is not None:
        return settings._media_asset_lookup(asset_id)
    if not settings.media_service_url or not settings.perlimen_internal_token:
        raise MediaAssetValidationError("media validation is not configured")
    try:
        response = httpx.get(
            f"{settings.media_service_url.rstrip('/')}/internal/v1/assets/{asset_id}",
            headers={"X-Threshold-Internal-Token": settings.perlimen_internal_token},
            timeout=settings.media_request_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise MediaAssetValidationError("media validation failed") from exc
    if response.status_code == 404:
        raise MediaAssetValidationError("media asset not found")
    if response.status_code >= 400:
        raise MediaAssetValidationError("media validation failed")
    body = response.json()
    return MediaAssetRef(
        id=body["id"],
        owner_user_id=body["owner_user_id"],
        context=body["context"],
        status=body["status"],
    )


def validate_media_asset(
    settings: MediaConsumerSettings, *, asset_id: str, owner_user_id: str, context: str
) -> None:
    asset = get_media_asset(settings, asset_id)
    if asset.owner_user_id != owner_user_id:
        raise MediaAssetValidationError("media asset owner mismatch")
    if asset.context != context:
        raise MediaAssetValidationError("media asset context mismatch")
    if asset.status != "ready":
        raise MediaAssetValidationError("media asset is not ready")
