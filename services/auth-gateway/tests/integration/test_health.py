from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import auth_gateway.main as main
import httpx
import jwt
import nats
import pytest
from auth_gateway import users_client
from auth_gateway.main import app
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from perlimen_common.auth import JwtVerifier, Principal


def test_healthz() -> None:
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "auth-gateway"}


def test_me_requires_bearer_token() -> None:
    client = TestClient(app)
    response = client.get("/me")
    assert response.status_code == 401


def test_me_fails_closed_when_auth_validation_is_not_configured() -> None:
    client = TestClient(app)
    response = client.get("/me", headers={"Authorization": "Bearer smoke"})
    assert response.status_code == 503
    assert response.json() == {"detail": "auth validation is not configured"}


@dataclass
class FakeJwtVerifier:
    def verify(self, token: str) -> Principal:
        assert token == "smoke"
        return Principal(subject="ak-subject", email="ada@example.test", username="ada")


@dataclass
class FakeUsersProfileClient:
    async def current_profile(
        self,
        *,
        subject: str,
        email: str | None,
        username: str | None,
    ) -> dict[str, object]:
        assert subject == "ak-subject"
        assert email == "ada@example.test"
        assert username == "ada"
        return {
            "user": {
                "id": "user-1",
                "authentik_subject": subject,
                "email": email,
                "username": username,
            },
            "consumer_profile": {
                "id": "profile-1",
                "display_name": "Ada",
                "bio": None,
            },
            "onboarding_preferences": {
                "id": "prefs-1",
                "city": "Wroclaw",
                "preferred_scenes": "electronic,live",
            },
        }


def test_main_passes_internal_token_without_changing_nats_transport(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("THRESHOLD_INTERNAL_TOKEN", "test-internal-token")
    monkeypatch.setattr(
        main,
        "UsersProfileClient",
        lambda **kwargs: captured.update(kwargs) or FakeUsersProfileClient(),
    )

    main._build_users_profile_client(main.Settings(users_transport="nats"))

    assert captured["transport"] == "nats"
    assert captured["internal_token"] == "test-internal-token"


def test_me_returns_application_profile_from_users(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(main, "jwt_verifier", FakeJwtVerifier())
    monkeypatch.setattr(main, "users_profile_client", FakeUsersProfileClient(), raising=False)
    client = TestClient(app)

    response = client.get("/me", headers={"Authorization": "Bearer smoke"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "authenticated",
        "subject": "ak-subject",
        "email": "ada@example.test",
        "username": "ada",
        "profile": {
            "user": {
                "id": "user-1",
                "authentik_subject": "ak-subject",
                "email": "ada@example.test",
                "username": "ada",
            },
            "consumer_profile": {
                "id": "profile-1",
                "display_name": "Ada",
                "bio": None,
            },
            "onboarding_preferences": {
                "id": "prefs-1",
                "city": "Wroclaw",
                "preferred_scenes": "electronic,live",
            },
        },
    }


def test_http_gateway_requires_admin_audience_not_product_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THRESHOLD_INTERNAL_TOKEN", "test-internal-token")
    config = main.Settings(
        users_transport="http",
        users_base_url="http://127.0.0.1:3800/users",
        authentik_audience="perlimen-admin-test",
    )
    issuer = "https://id.example.test/admin/"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = JwtVerifier(
        issuer=issuer, jwks_url=f"{issuer}jwks/", audience=config.authentik_audience
    )
    jwks = jwt.PyJWKClient(f"{issuer}jwks/")
    rsa_jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    monkeypatch.setattr(jwks, "get_signing_key_from_jwt", lambda _: jwt.PyJWK(rsa_jwk))
    monkeypatch.setattr(verifier, "_jwks_client", jwks)
    monkeypatch.setattr(main, "jwt_verifier", verifier)
    monkeypatch.setattr(main, "users_profile_client", main._build_users_profile_client(config))

    requests: list[httpx.Request] = []

    def users(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) > 1:
            return httpx.Response(200, content=b"unavailable")
        return httpx.Response(200, json={"user": {"id": "internal-profile"}})

    async def unexpected_broker(*args: object, **kwargs: object) -> None:
        pytest.fail("HTTP gateway attempted a NATS connection")

    monkeypatch.setattr(nats, "connect", unexpected_broker)
    http_client = httpx.AsyncClient
    monkeypatch.setattr(
        users_client.httpx,
        "AsyncClient",
        lambda **kwargs: http_client(transport=httpx.MockTransport(users), **kwargs),
    )

    def bearer(audience: str | None) -> dict[str, str]:
        claims = {
            "iss": issuer,
            "sub": "operator",
            "exp": datetime.now(UTC) + timedelta(minutes=1),
        }
        if audience is not None:
            claims["aud"] = audience
        return {"Authorization": f"Bearer {jwt.encode(claims, key, algorithm='RS256')}"}

    with TestClient(app) as client:
        client.cookies.set("threshold_session", "opaque-product-session")
        assert client.get("/me").status_code == 401
        assert client.get("/me", headers=bearer(None)).status_code == 401
        assert client.get("/me", headers=bearer("other-application")).status_code == 401
        assert not requests
        response = client.get("/me", headers=bearer("perlimen-admin-test"))
        assert response.status_code == 200
        assert response.json()["profile"] == {"user": {"id": "internal-profile"}}
        assert "set-cookie" not in response.headers
        assert client.get("/me", headers=bearer("perlimen-admin-test")).status_code == 503
    request = requests[0]
    assert request.method == "POST"
    assert request.url == "http://127.0.0.1:3800/users/internal/v1/current-profile"
    assert request.headers["X-Threshold-Internal-Token"] == "test-internal-token"
    assert "authorization" not in request.headers and "cookie" not in request.headers
