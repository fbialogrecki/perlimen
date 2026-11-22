import logging
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response

from auth_gateway.settings import Settings
from auth_gateway.users_client import UsersProfileClient, UsersProfileClientError
from perlimen_common.auth import (
    AuthConfigurationError,
    AuthError,
    JwtVerifier,
    require_bearer_token,
)
from perlimen_common.health import ok
from perlimen_common.http_observability import instrument_http_observability
from perlimen_common.logging import configure_logging
from perlimen_common.telemetry import configure_telemetry, instrument_fastapi

settings = Settings()
configure_logging()
configure_telemetry(settings.service_name)
logger = logging.getLogger(__name__)


def _build_jwt_verifier(config: Settings) -> JwtVerifier:
    return JwtVerifier(
        issuer=config.authentik_issuer,
        jwks_url=config.authentik_jwks_url,
        audience=config.authentik_audience,
        require_expiration=config.users_transport == "http",
    )


def _build_users_profile_client(config: Settings) -> UsersProfileClient:
    internal_token = config.perlimen_internal_token
    return UsersProfileClient(
        base_url=config.users_base_url,
        timeout_seconds=config.users_timeout_seconds,
        transport=config.users_transport,
        internal_token=internal_token.get_secret_value() if internal_token is not None else None,
        nats_url=config.nats_url,
        nats_subject=config.users_current_profile_subject,
    )


jwt_verifier = _build_jwt_verifier(settings)
users_profile_client = _build_users_profile_client(settings)

app = FastAPI(title="Perlimen auth-gateway", version="0.1.0")
instrument_fastapi(app)
instrument_http_observability(app, service_name=settings.service_name)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return ok(settings.service_name)


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    return ok(settings.service_name)


@app.get("/me")
async def me(
    response: Response, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    try:
        token = require_bearer_token(authorization)
        principal = jwt_verifier.verify(token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail="unauthenticated") from exc
    except AuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail="auth validation is not configured") from exc

    identity = {
        "subject": principal.subject,
        "email": principal.email,
        "username": principal.username,
    }
    if users_profile_client.transport == "http":
        try:
            await users_profile_client.confirm_operator_identity(identity)
        except UsersProfileClientError as exc:
            raise HTTPException(
                status_code=503, detail="operator identity service is unavailable"
            ) from exc
        response.headers["Cache-Control"] = "no-store"
        return {"status": "authenticated", **identity}

    try:
        profile = await users_profile_client.current_profile(
            subject=principal.subject,
            email=principal.email,
            username=principal.username,
        )
    except UsersProfileClientError as exc:
        raise HTTPException(status_code=503, detail="users profile service is unavailable") from exc

    logger.info("current profile resolved through users service")
    return {
        "status": "authenticated",
        **identity,
        "profile": profile,
    }
