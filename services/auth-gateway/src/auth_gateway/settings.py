from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from perlimen_common.config import ServiceSettings


class Settings(ServiceSettings):
    model_config = SettingsConfigDict(hide_input_in_errors=True)

    service_name: str = "auth-gateway"
    authentik_issuer: str | None = None
    authentik_jwks_url: str | None = None
    authentik_audience: str | None = None
    users_base_url: str = "http://127.0.0.1:8001"
    users_timeout_seconds: float = 2.0
    users_transport: Literal["http", "nats"] = "http"
    perlimen_internal_token: SecretStr | None = Field(
        default=None, validation_alias="THRESHOLD_INTERNAL_TOKEN"
    )
    nats_url: str = "nats://127.0.0.1:4222"
    users_current_profile_subject: str = "users.current_profile.v1"

    @model_validator(mode="after")
    def require_http_configuration(self) -> Self:
        if self.users_transport == "http":
            if (
                not self.perlimen_internal_token
                or not self.perlimen_internal_token.get_secret_value()
            ):
                raise ValueError("internal token is required for HTTP users transport")
            if not self.authentik_audience or not self.authentik_audience.strip():
                raise ValueError("admin audience is required for HTTP users transport")
        return self
