# auth-gateway

Thin internal/admin edge service for Perlimen Authentik-backed SSO flows.

This service is not product-auth for public Perlimen users. Public register/login/session flows belong to product-auth in `users` and use custom UI + HttpOnly cookies.

Current scope:

- `/healthz`
- `/readyz`
- `/me` requiring a Bearer token issued by Authentik
- Authentik JWKS/JWT validation
- user context propagation
- standalone: NATS request/reply to `users.current_profile.v1`
- optional Lite gateway: HTTP `POST /users/internal/v1/current-profile`

Intended boundary:

- internal/admin/backend service access: Authentik OIDC/JWT through `auth-gateway`
- product user access: `users` product-auth session, no Authentik dependency in public login/register

## Optional Lite configuration

The Lite launcher does not start this service. If testing operator SSO, run the existing
`auth_gateway.main:app` process separately on loopback, with only synthetic accounts and
an isolated issuer. Do not expose it through the product BFF.

| Setting | Value |
| --- | --- |
| `THRESHOLD_USERS_TRANSPORT` | `http` |
| `THRESHOLD_USERS_BASE_URL` | `http://127.0.0.1:<lite-api-port>/users` |
| `THRESHOLD_INTERNAL_TOKEN` | The same private token as the isolated Lite API, supplied server-side |
| `THRESHOLD_AUTHENTIK_ISSUER` and `THRESHOLD_AUTHENTIK_JWKS_URL` | Issuer and signing keys of the operator-only application |
| `THRESHOLD_AUTHENTIK_AUDIENCE` | The dedicated admin application's expected `aud` value (required in HTTP mode) |

Restrict access in the identity provider to the intended operators before using that
audience. The existing profile endpoint maps by Authentik subject; use distinct synthetic
email addresses and usernames rather than trying to link product accounts. The response
does not establish a product session or authorize product moderation. Standalone deployment
configuration and the legacy NATS transport are unchanged.
