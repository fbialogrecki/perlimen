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
- optional Lite gateway: HTTP `POST /users/internal/v1/operator-identity`

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

Use a dedicated Authentik application/provider, not a client or audience shared with any
other application. Require an operator-only application policy that denies non-operators
before token issuance. Check with both an allowed operator and a non-operator in the isolated
IdP before enabling the gateway; keep the gateway off if either result is wrong. The gateway
verifies signature, issuer, expiry and audience; a matching `aud` proves the target application,
not group membership. Access policy is enforced at issuance, not rechecked on each `/me` call.

`GET /me` confirms the verified `subject`, `email` and `username` over the private token-guarded
Lite route and returns those fields with `status: authenticated`. It never forwards the bearer,
sets cookies, writes product data or confers product permissions. The same email and username
remain available for ordinary registration. Standalone deployment configuration and the legacy
NATS profile contract are unchanged.
