# Perlimen Project Specification

| Field | Value |
| --- | --- |
| **Status** | Accepted — describes shipped behavior |
| **Version** | 1.2.0 |
| **Owner** | Engineering |
| **Last updated** | 2026-11-22 |
| **Verified against** | `apps/web/src/`, `apps/web/package.json`, `services/*/src/`, `libs/py/perlimen_common/`, `perlimen_lite/`, `infra/argocd/`, `infra/kustomize/`, `.github/workflows/ci.yml`, `.woodpecker/`, `Taskfile.yml`, `pyproject.toml` |

## Purpose of this file

This is the **project context to load before working in this repository**: what Perlimen is,
how it is built, which service owns which data, and what the deployed system does.

It records **current shipped state only**. Plans, target designs, open questions, acceptance
checklists and TODOs are deliberately absent and are not tracked in this repository; working
rules for agents live in `AGENTS.md`, operational procedures in `docs/`. Two consequences follow:

- Everything written here can be relied on as a description of what exists today.
- If a behavior is not described here, it is not built. Absence is information.

Within that scope it is the source of truth for product behavior, API contracts, the data
model, privacy, security and service ownership boundaries.

## How to read this file

**Do not read this file end to end.**

1. Read the header and map with `Read(offset: 1, limit: 76)`.
2. Read only the entries your task touches, using `offset`/`limit` from the **Lines** column.
3. For a known symbol (an endpoint, table, env var or setting), grep instead of reading.

Keep this map correct: line ranges shift whenever content is added or removed.

| § | Section | Lines |
| --- | --- | --- |
| **1** | **Specification governance** — changing anything in this file | 80-90 |
| **2** | **Vision and scope** — you need product framing or the MVP boundary | 91-109 |
| **3** | **Functional requirements** — changing product behavior | 110-292 |
| 3.1 | Access model and web routes | 112-128 |
| 3.2 | Accounts and authentication | 129-149 |
| 3.3 | Identity, profiles, Pages and follows | 150-167 |
| 3.4 | Groups, posts and comments | 168-178 |
| 3.5 | Feed | 179-192 |
| 3.6 | Events | 193-209 |
| 3.7 | Guestlists and check-in | 210-226 |
| 3.8 | Media | 227-242 |
| 3.9 | Notifications | 243-251 |
| 3.10 | Search and mentions | 252-260 |
| 3.11 | Safety: blocks, reports, moderation | 261-273 |
| 3.12 | Account erasure | 274-292 |
| **4** | **Non-functional requirements** — touching limits, i18n, observability, health | 293-310 |
| **5** | **System context** — you need the repo layout or the ownership split | 311-349 |
| 5.1 | Repository layout | 313-333 |
| 5.2 | Service ownership | 334-349 |
| **6** | **Architecture** — touching runtime, service transports, CI/CD or GitOps | 350-402 |
| 6.1 | Runtime | 352-376 |
| 6.2 | Service-to-service transports | 377-389 |
| 6.3 | Build, publication and deployment | 390-402 |
| **7** | **Security** — touching auth, trust boundaries, secrets or headers | 403-420 |
| **8** | **Privacy and data retention** — touching personal data, deletion or backups | 421-434 |
| **9** | **Data model** — changing tables or migrations | 435-471 |
| 9.1 | users | 440-450 |
| 9.2 | social | 451-456 |
| 9.3 | events | 457-464 |
| 9.4 | media | 465-471 |
| **10** | **API contract** — adding or changing an endpoint | 472-551 |
| 10.1 | Conventions | 474-479 |
| 10.2 | users | 480-501 |
| 10.3 | social | 502-515 |
| 10.4 | events | 516-531 |
| 10.5 | media and auth-gateway | 532-538 |
| 10.6 | Web BFF | 539-551 |
| **11** | **Test plan** — you need test locations, gates or CI jobs | 552-567 |
| **12** | **References** — you need operational detail kept out of this file | 568-577 |

---

## 1. Specification governance

- This file describes shipped behavior only. Do not add plans, target designs, open
  questions, acceptance checklists or TODOs.
- Update the affected section in the same change that alters product behavior, API contracts,
  the data model, privacy/security behavior or service ownership boundaries.
- Refresh the section map's line ranges after every edit (`grep -n '^##' PROJECT_SPECIFICATION.md`).
- If code and this file disagree, the code is the fact; correct this file.

---

## 2. Vision and scope

Perlimen is an open-source social and event-discovery platform for local music scenes. It
connects people, artists and Pages (clubs, collectives, projects, festivals) through
chronological feeds, official groups, public event pages, organizer-managed guestlists and
QR check-in, without algorithmic ranking or third-party tracking.

The shipped MVP covers product auth, profiles and Pages, follows, official groups, posts and
comments with votes and emoji reactions, events with updates, follows and boosts, private
guestlists with artist quotas and door check-in, image media, notifications, mentions,
search, blocks, reports and account erasure. It has no ticketing, payments, direct messages,
user-created groups, mobile app or algorithmic feed. The `secret_location` event mode exists
in the data model but event create/update rejects it.

The product name is Perlimen; deployed identifiers deliberately keep the legacy `threshold`
name (namespace, env var prefix, headers, cookies, buckets). See `docs/rebranding.md`.

---

## 3. Functional requirements

### 3.1 Access model and web routes

- All browser traffic goes to the Next.js app in `apps/web`. Services are only reached by the
  server-side BFF (`apps/web/src/app/api/`) and server components.
- Anonymous routes: `/` (landing), `/login`, `/register`, `/reset-password`, `/verify-email`,
  `/privacy`, and the public detail pages `/events/[slug]`, `/u/[username]` and
  `/pages/[slug]`. Detail pages render server-side for anonymous visitors and offer login for
  interactions.
- Session-gated routes: everything under `/app/*` (gated once in `apps/web/src/app/(app)/layout.tsx`),
  `/events` (redirects to `/app/events` or login), `/groups`, `/groups/[slug]`,
  `/posts/[id]` and `/prototypes/feed-modes`. Anonymous visitors are redirected to
  `/login?callbackUrl=…`; callback URLs pass through `safeInternalHref`.
- A signed-in user without a canonical onboarding city is redirected to `/onboarding` before
  reaching `/app/*`.
- Authenticated app pages: Feed (`/app`), compose, events list/new/edit, guestlist,
  notifications, Pages management, saved, search, settings, and the feed-modes prototype.

### 3.2 Accounts and authentication

- Product auth is custom email/password auth in `users`, exposed through BFF routes under
  `/api/auth/*` (register, login, logout, refresh, email verify request/confirm, password
  reset request/confirm) and `/api/me`. Authentik is not used for product login.
- Passwords: Argon2id (time cost 3, memory 64 MiB, parallelism 2, 32-byte hash) over an
  HMAC-SHA256 pre-hash with a versioned server-side pepper (`services/users/src/users/auth/hashing.py`).
- Sessions: opaque random tokens stored only as HMAC hashes in `user_sessions`.
  `threshold_session` (path `/`) lives 15 minutes; `threshold_refresh` lives 30 days and
  rotates on refresh. Its path is `/v1/auth` in standalone users, `/users/v1/auth` in Lite,
  and `/api/auth` at the BFF. Both cookies are HttpOnly, SameSite=Lax, and Secure when configured.
- Email verification tokens expire after 24 hours, password reset tokens after 30 minutes.
  Tokens are never returned to the browser. Email is sent over SMTP when enabled; plaintext
  SMTP is refused outside `local`/`test` environments. Standalone email links use HTTPS. Lite
  leaves SMTP off by default; opt-in loopback capture uses the actual local web origin in links.
- Auth endpoints are rate limited per client IP and per subject (default 120 attempts per
  60 s); auth events are written to `auth_audit_logs` with keyed hashes of subject, IP and
  user agent.
- Onboarding (`PUT /v1/me/onboarding`) stores the user's city and preferences; the web app
  accepts the canonical cities in `apps/web/src/lib/cities.ts`.

### 3.3 Identity, profiles, Pages and follows

- A person has exactly one public name: the registration username, shown without `@` and in
  the case its owner typed. Usernames are 3-30 characters from `A-Za-z0-9_.-` plus Polish
  diacritics. Uniqueness folds case and diacritics (`Żaba` and `Zaba` collide); emails fold
  case only. Reserved names: `admin`, `root`, `support`, `threshold`, `perlimen`. All of this
  lives in `services/users/src/users/domain/identity.py`.
- Each user has a consumer profile; `POST /v1/me/artist` adds an artist profile.
  `PATCH /v1/me/profile` edits profile fields and avatar.
- Pages are entities with their own display name and slug, a type (`club`, `collective`,
  `project`, `festival`), city, about text, links and avatar. Roles are `owner`, `admin`,
  `editor`, enforced in `users`: any role edits the Page and lists members, owners add,
  change and remove members.
- Residencies: only a current Page owner or admin can invite an artist; the role is checked
  under the account write lock. Artists accept or reject (`/v1/me/residencies/{id}/accept|reject`).
- Follows target artist profiles, consumer profiles or Pages (`/v1/me/follows`). Event
  follows live in `events`.

### 3.4 Groups, posts and comments

- Groups are official only: there is no endpoint to create a group. Users join and leave
  (`/v1/groups/{slug}/membership`).
- Posts are text, optionally in a group, with either images or an event link (a database
  check forbids both). Authors edit and delete their own posts. `events` publishes event announcement posts into city
  groups through `/internal/v1/event-announcements`; system posts cannot be deleted.
- Comments allow replies up to two levels. Posts and comments take up/down votes
  (`/reaction`); posts also take emoji reactions (`/emoji`, validated emoji up to 32 chars).
- Social writes are rate limited per user (default 60 per 60 s).

### 3.5 Feed

- The stream is named **Feed** and is chronological. There is no ranking.
- `social` `GET /v1/feed` returns visible posts by followed users and from the viewer's
  groups, excluding hidden posts and blocked authors, newest first with `created_at|id`
  cursors (`filter=all|posts`). Lite reads follows and blocks from the `users` owner API,
  filters before pagination and fetches interactions in batches, not per post.
- The web app assembles the Feed in `apps/web/src/lib/feed/assembly.ts` from posts, event
  feed candidates, event updates and guestlist access notifications, with filters `all`,
  `posts`, `events` and `access`.
- `/app/prototypes/feed-modes` is a session-gated prototype comparing classic, focus (one
  card at a time) and compact presentations over fixture items. In focus mode, wheel and
  swipe do not move to the next card while a post is expanded.

### 3.6 Events

- Events belong to a Page. Page owners, admins and editors create, edit, soft-delete and post
  updates to events; `events` checks the role with `users` per request.
- An event has slug, title, description, start time, city, location mode, venue, address,
  genres, line-up and optional poster (`event_poster` media asset owned by the creator).
- Location modes: `public_location` and `tba` are accepted; `secret_location` is rejected on
  create and update ("not implemented yet"). `users` has secret-location payload and key
  envelope tables, but no endpoint reads or writes them.
- `GET /v1/events` lists events by city, Page, artist, line-up artist or text, sorted by
  start or creation time, with URL-safe base64 timestamp cursors. Event detail pages are
  public with SSR and Open Graph metadata.
- Lite replays the Social announcement on an exact create retry from the original creator.
  The endpoint still returns the existing slug conflict; Social keeps at most one post per event.
- Signed-in users follow and boost events. A boost is social support, not an RSVP or paid
  promotion.

### 3.7 Guestlists and check-in

- Guestlists are private. Event managers add and remove guests; each line-up artist adds
  guests up to a per-event quota set by managers (`/guestlist/dj`, `/guestlist/quotas/{id}`).
  There is no public request, invite-code or discovery flow. Private BFF reads and QR responses
  are not cacheable.
- Guests see their own access (`/v1/events/{slug}/access`). Adding and removing guests
  notifies the guest (`guestlist.added`, `guestlist.removed`).
- Check-in: the guest mints a QR token (`/guestlist/me/qr-token`), an opaque 32-byte random
  value stored as a hash, valid 300 seconds, single use; minting revokes the guest's earlier
  unused tokens. Only managers and assigned door staff can check token status or redeem it
  (`/check-in`). Expired, used or revoked tokens and already-checked-in guests are rejected.
  A token is bound to one event and guestlist entry and carries no personal data or IDs. The check-in response shows only
  the status and the guest's name. Check-in proves account control, not legal identity.
- Door staff are assigned by username. Access changes are written to
  `event_access_audit_logs`.

### 3.8 Media

- Uploads go browser → BFF (`/api/media/assets`) → `media` `POST /v1/assets/upload` as a
  streamed multipart body with a `context` and a file.
- Contexts: `user_avatar`, `page_avatar`, `post_image`, `event_poster`. Accepted types: JPEG,
  PNG, WebP; at most 10 MB, 8192 px per side and 16 megapixels. Validation checks declared
  type, decoded format and limits.
- `media` stores the original at `assets/{id}/original` and WebP derivatives at
  `assets/{id}/{variant}.webp`: `avatar_512`/`avatar_256` (square crop) for avatars,
  `post_1280`/`post_480` for post images and posters. Only `media` creates object keys.
- Images are served same-origin through `/api/media/assets/[...path]`. In Lite, reads are
  uncached and never serve originals: owners can preview, visible post attachments require
  an unblocked session, and linked public avatars and event posters remain anonymous-readable.
- Lite writes private local blobs without S3. Upload success retains the asset and derivative
  metadata with logical keys, never host paths; internal erasure deletes originals and derivatives.

### 3.9 Notifications

- `users` stores notifications (`notification_events`) with dedupe keys, unread counts and
  read/read-all actions. Other services create them through `POST /internal/v1/notifications`.
- Shipped sources include follows, comments, mentions, event updates, Page membership
  changes, residency invitations/decisions and guestlist changes.
- Preferences toggle four categories: mentions, engagement, event updates, Page updates.
  Critical types (Page membership, residency and guestlist changes) bypass preferences.

### 3.10 Search and mentions

- Search (`/app/search`) combines profiles and Pages from `users` `GET /v1/search`, groups
  from `social` `GET /v1/search/groups` and events from `events` `GET /v1/events?q=`, using
  case-insensitive substring matching. There is no external search engine.
- `@` is how a mention is typed; resolved mentions render as the bare name and link to the
  target. Mention targets resolve through `users` (profiles, Pages) and `events` internal
  endpoints; `/api/search/mentions` serves autocomplete.

### 3.11 Safety: blocks, reports, moderation

- `users` is the only writer of blocks (`/v1/me/blocks`). Lite has no Social enforcement copy
  or reconciliation; the next Feed, group or thread read uses the committed registry in both
  directions. New comments, votes and emoji respect block visibility. Mention policy checks
  the resolved account ID. Standalone `social` keeps its NATS-fed projection and reconciles it
  every 300 seconds with `users` `GET /internal/v1/blocks`.
- Reports target `profile`, `page`, `post` or `comment`. Profile and Page reports go to
  `users`; the reported user and the Page's owners/admins see open reports. Post and comment
  reports go to `social`; the author of the reported content can set status
  (`open`, `reviewing`, `resolved`, `dismissed`) and apply `hide` or `delete`.
- Safety actions are recorded in `safety_audit_logs` in both services.

### 3.12 Account erasure

- `DELETE /v1/me` (202) sets the account to `erasure_pending`, deletes sessions and pending
  tokens, and enqueues an `account_erasure_jobs` row in one transaction.
- The settings flow uses `DELETE /api/me`; 202 acknowledges the durable request, not completed
  erasure. Lite calls each owning module in-process and resumes persisted jobs after restart,
  including reclaiming an interrupted job after its lease expires. Media removes the local
  original and derivatives before deleting their inventory.
- A background worker in `users` runs stages in order: `social` (anonymize author),
  `events`, `media`, then local data. Failed stages retry with exponential backoff capped at
  one hour. `social`, `events` and `media` write erasure tombstones that fence later writes
  by that owner.
- Users serializes relationship writes on the affected accounts and rejects inactive accounts.
  Erasure removes user follows, not unrelated Page follows with a matching handle. It deletes
  sessions, tokens, preferences, notifications, blocks, memberships, residencies and key envelopes;
  clears credentials, identity, artist and onboarding data; anonymizes the consumer profile and retained audits/reports.

---

## 4. Non-functional requirements

- Every service exposes `GET /healthz` and `GET /readyz`.
- Rate limits are in-process: users auth (120 / 60 s per IP and subject), social and events
  writes (60 / 60 s per user), and a bounded fixed-window limiter in the web BFF
  (`apps/web/src/lib/http/local-rate-limit.ts`). They reset on restart.
- Internal HTTP calls use short timeouts (1.5 s defaults in service settings).
- The web UI is translated with `next-intl` (`apps/web/messages/en.json`, `pl.json`); the
  locale cookie is `threshold_locale`.
- The UI is dark-only; motion respects `prefers-reduced-motion`. Only the landing hero uses
  three.js, lazy-loaded with a static fallback.
- Observability: k3s services export OTLP to their collector; Loki, Tempo, Mimir and Grafana stay
  in-cluster. Lite disables telemetry, exporters and raw Next dev logs/traces. It emits request IDs,
  route templates and durations for the API plus BFF timing for `/app`, QR check-in and upload.
- No fake data: product surfaces read live service data or show empty states.

---

## 5. System context

### 5.1 Repository layout

```text
apps/web/                 Next.js 16 App Router app and BFF (Bun, React 19, Tailwind 4)
services/users/           Product auth, profiles, Pages, follows, blocks, notifications, erasure
services/social/          Groups, posts, comments, votes, emoji, Feed, social reports
services/events/          Events, updates, follow, boost, guestlists, door staff, check-in
services/media/           Image validation, derivatives, S3 storage
services/auth-gateway/    Internal/admin SSO edge validating Authentik tokens
libs/py/perlimen_common/  Shared Python helpers (settings, auth, telemetry, logging)
infra/                    Argo CD apps, Kustomize bases and `local` overlay, Helm values
ops/                      Operator scripts: secret seeding, unseal, rotation, backups
ci/, .woodpecker/         CI helpers and Woodpecker pipelines
.github/workflows/ci.yml  GitHub Actions validation (`ci-ok`)
docs/                     Architecture, release/deploy, rebranding, runbooks
```

Python services share one uv workspace (`pyproject.toml`, Python 3.13). Each service package
has `main.py`, `settings.py`, `api/`, `domain/`, `db/`, `migrations/` and a service-level
`tests/`. Settings read env vars with the `THRESHOLD_` prefix.

### 5.2 Service ownership

| Owner | Data and decisions |
| --- | --- |
| `users` | Accounts, credentials, sessions, verification/reset tokens, onboarding, consumer/artist profiles, Pages, memberships, residencies, follows, the canonical block registry, notifications, profile/Page reports, auth audit, account erasure orchestration |
| `social` | Groups, memberships, posts, comments, mentions, votes, emoji reactions, event announcements, Feed, post/comment reports, standalone-only block enforcement copy |
| `events` | Events, updates, follows, boosts, guestlists, artist quotas, door staff, check-in tokens, access audit |
| `media` | Media assets, derivatives, object keys and S3 writes |
| `auth-gateway` | Authentik JWT validation for internal/admin surfaces; no product data |
| `apps/web` | Rendering, BFF routing, cookies, Feed assembly; no database |

No service joins another service's database. Domain authorization lives in the owning
service.

---

## 6. Architecture

### 6.1 Runtime

- One single-node k3s cluster, namespace `threshold`. Argo CD app-of-apps: root Application
  `threshold-root` tracks `infra/argocd` on `main`; the ApplicationSet `threshold-services`
  deploys `web`, `auth-gateway`, `users`, `social`, `media` and `events`.
- One Kustomize overlay, `infra/kustomize/overlays/local`. Each service has a
  `NetworkPolicy` in `infra/kustomize/base/<service>/networkpolicy.yaml`.
- PostgreSQL via CloudNativePG, one cluster per service (`users-postgres`, `social-postgres`,
  `events-postgres`, `media-postgres`). Migrations run as Argo CD sync hook Jobs
  (`python -m <svc>.migrate`).
- Object storage: SeaweedFS S3 (bucket `threshold-media`). Messaging: NATS Core.
- Secrets: OpenBao, synced by External Secrets Operator.
- The cluster runs on one NUC host. Woodpecker, Harbor, OpenBao and Authentik also run in
  it; OpenBao, Harbor and Authentik are installed outside Argo CD.
- Public access: `cloudflared` (Argo CD app `threshold-cloudflared`) forwards to the web
  Service only. A separate webhook-only proxy (`threshold-woodpecker-webhook`) exposes the
  Woodpecker hook endpoint.

The opt-in local backend (`perlimen_lite.main:app`) mounts the four product routers under
`/users`, `/social`, `/events` and `/media` behind the internal token. Users still authenticates
product sessions. One dedicated PostgreSQL database holds four isolated schemas, each with
a separate Alembic ledger, migration login and non-owner runtime login. Root readiness checks
the four heads and the private media directory. This backend does not replace the k3s deployment.
See [Perlimen Lite backend](docs/perlimen-lite-backend.md) for the local configuration contract.

### 6.2 Service-to-service transports

| Transport | Flows |
| --- | --- |
| In-process owner API (Lite) | Social follows, blocks, mentions and notifications; Events Page roles, artist and user references, guest and update notifications, and city announcements; media asset validation; account erasure. Each call uses the owner's runtime login |
| NATS request/reply (standalone) | `users.current_profile.v1` (auth-gateway → users), `users.follow.list_following.v1` (social → users) |
| NATS pub/sub (standalone) | `users.block.changed.v1` (users → social) |
| HTTP `/internal/v1/*` with `X-Threshold-Internal-Token` | Standalone cross-service calls and integrations not moved in-process: Page roles and artist lookups (events → users), notifications and mention lookups, event announcements (events → social), account erasure (users → social/events/media), media asset validation |

The BFF sends `social`, `events` and `media` the internal token and session-derived identity
(`X-Threshold-User-Id` / `X-Threshold-Username`). Product auth uses session cookies; in Lite
the BFF also sends the internal token to the pinned loopback `/users` endpoint without redirects.

### 6.3 Build, publication and deployment

- GitHub Actions `ci.yml` runs on pull requests and pushes to `main`; job `ci-ok` aggregates
  `changes`, `security`, `infra`, `containers`, `python` and `web`.
- `deploy.yml` publishes on application/build-input pushes to `main`, not docs/`infra/` pushes.
  After exact-SHA `ci-ok`, Woodpecker tags six Harbor images with that SHA and opens a digest PR.
  Argo CD deploys on auto-merge. Reverting the digest PR does not reverse migrations.
- The opt-in Lite timer trials the CI-green main tip in a separate checkout and a private network
  namespace with only Web, API and PostgreSQL forwarded on host loopback. After readiness it rechecks
  the same CI proof, then main; failed trials retain the healthy checkout and synthetic store.

---

## 7. Security

- Browsers never hold internal tokens, S3 credentials, bucket names or raw object keys.
- `social`, `events` and `media` require `X-Threshold-Internal-Token` on every route
  (constant-time compare; 503 when unconfigured) and trust user headers only after it.
  Standalone `users` requires it on `/internal/v1/*`; Lite also requires it on `/users/v1/*`.
- Lite BFF mutations require the exact configured public `Origin`; standalone checks its host.
- The web proxy (`apps/web/src/proxy.ts`) sets CSP (`default-src 'self'`, no frames,
  `object-src 'none'`), `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`,
  `Permissions-Policy`, and HSTS in production unless trusted LAN HTTP is configured.
- Check-in tokens are short-lived, opaque, single-use and stored hashed (§3.7).
- Media uploads are streamed with byte, header and part limits before decoding (§3.8).
- Authentik serves operator/admin SSO only; `auth-gateway` validates its JWTs via JWKS.
- CI: Gitleaks and Trivy run in the `security` job; `go-task repo:secret-scan` runs locally.
  Untrusted pull-request jobs carry no publication credentials.

---

## 8. Privacy and data retention

- No third-party trackers or analytics are shipped; the CSP allows same-origin connections
  only.
- Public identity is the username; there are no public real-name fields for people.
- Auth audit logs store keyed hashes of subject, IP and user agent, not raw values.
- Account erasure (§3.12) deletes or anonymizes personal data across all four data services
  while keeping public thread integrity (authors become unlinked "Deleted User" labels).
- Backups: each CNPG cluster archives WAL and takes daily base backups through the Barman
  Cloud plugin with 14-day retention. Host-level backups are described in
  `docs/runbooks/backup-restore.md`.

---

## 9. Data model

SQLAlchemy models live in `services/<svc>/src/<pkg>/domain/models.py`; Alembic migrations in
`services/<svc>/src/<pkg>/migrations/` ship in the wheel. IDs are 36-character string UUIDs.

### 9.1 users

`application_users` (status `active|locked|erasure_pending|deleted`, identity source
`product|authentik_internal`), `user_credentials`, `user_sessions`,
`email_verification_tokens`, `password_reset_tokens`, `auth_audit_logs`,
`consumer_profiles`, `artist_profiles`, `onboarding_preferences`, `pages`,
`page_memberships` (role enum `page_membership_role`), `page_residencies`
(`pending|accepted|rejected`), `follows`, `user_blocks`, `content_reports`,
`safety_audit_logs`, `notification_preferences`, `notification_events`,
`account_erasure_jobs`, `secret_location_payloads`, `secret_location_key_envelopes`.

### 9.2 social

`groups`, `group_memberships`, `posts`, `event_announcements`, `post_mentions`, `comments`,
`comment_mentions`, `reactions`, `comment_reactions`, `post_emoji_reactions`, `user_blocks`
(standalone enforcement copy; absent in Lite), `safety_reports`, `safety_audit_logs`, `account_erasure_tombstones`.

### 9.3 events

`events` (soft delete via `deleted_at`; `location_mode` string), `event_follows`,
`event_updates`, `event_boosts`, `event_guestlist_entries` (`active|removed`),
`event_guest_quotas`, `event_door_staff`, `event_check_in_tokens`
(`issued|revoked|used`, unique `token_hash`), `event_access_audit_logs`,
`account_erasure_tombstones`.

### 9.4 media

`media_assets` (owner, context, bucket, keys), `media_derivatives`,
`account_erasure_tombstones`.

---

## 10. API contract

### 10.1 Conventions

JSON over HTTP. Routes under `/v1/*` serve the BFF on behalf of a user; `/internal/v1/*` are
service-to-service. Errors use FastAPI `{"detail": ...}` with 401/403/404/409/422/429 as
appropriate.

### 10.2 users

- Auth: `POST /v1/auth/register|login|refresh|logout`, `GET /v1/auth/me`,
  `POST /v1/auth/email/verify/request|confirm`, `POST /v1/auth/password/reset/request|confirm`.
- Me: `PUT /v1/me/onboarding`, `PATCH /v1/me/profile`, `POST /v1/me/artist`,
  `DELETE /v1/me`, `GET|POST /v1/me/follows`, `DELETE /v1/me/follows/{target_type}/{target_handle}`,
  `POST /v1/me/blocks`, `DELETE /v1/me/blocks/{username}`, `GET /v1/me/pages`,
  `POST /v1/me/residencies/{residency_id}/accept|reject`.
- Public reads: `GET /v1/profiles/{username}`, `GET /v1/pages/{slug}`, `GET /v1/search`.
- Pages: `POST /v1/pages`, `PATCH /v1/pages/{slug}`, `GET /v1/pages/{slug}/members`,
  `PUT|DELETE /v1/pages/{slug}/members/{username}`,
  `POST /v1/pages/{slug}/residencies/{username}/invite` (alias
  `/residency-invitations/{username}`).
- Notifications: `GET /v1/notifications`, `GET /v1/notifications/unread-count`,
  `POST /v1/notifications/{id}/read`, `POST /v1/notifications/read-all`,
  `GET|PUT /v1/notifications/preferences`.
- Safety: `POST /v1/reports`, `GET /v1/moderation/reports`, `GET /v1/safety/audit-log`.
- Internal: `current-profile`, `users/{id}/onboarding-preferences`, `pages/organizer-refs`,
  `users/active-refs`, `pages/{page_id}/members/{user_id}`, `blocks`,
  `users/{blocker}/blocks/{blocked}`, `mention-targets/profiles|pages/{handle}`,
  `artist-profiles/{id}`, `artist-profiles/batch`, `notifications`.

### 10.3 social

- Groups: `GET /v1/groups`, `GET /v1/groups/{slug}`, `POST|DELETE /v1/groups/{slug}/membership`,
  `GET /v1/me/groups`, `GET /v1/groups/{slug}/posts`, `GET /v1/search/groups`.
- Posts: `POST /v1/posts`, `POST /v1/event-posts`, `GET|PATCH|DELETE /v1/posts/{post_id}`,
  `GET|POST /v1/posts/{post_id}/comments`, `PUT|DELETE /v1/posts/{post_id}/reaction`,
  `PUT|DELETE /v1/posts/{post_id}/emoji`.
- Comments: `PATCH|DELETE /v1/comments/{comment_id}`, `PUT|DELETE /v1/comments/{comment_id}/reaction`.
- Feed: `GET /v1/feed?filter=all|posts&before=&limit=`.
- Safety: `POST /v1/reports`, `GET /v1/moderation/reports`,
  `PATCH /v1/moderation/reports/{report_id}`, `GET /v1/safety/audit-log`.
- Internal: `GET /internal/v1/capabilities`, `POST /internal/v1/event-announcements` (and
  `/batch`), `POST /v1/internal/anonymize-author`.

### 10.4 events

- Events: `POST /v1/events`, `GET /v1/events`, `GET|PATCH|DELETE /v1/events/{slug}`,
  `GET /v1/event-updates`, `GET|POST /v1/events/{slug}/updates`,
  `GET /v1/events/{slug}/viewer-context`, `POST|DELETE /v1/events/{slug}/follow`,
  `POST|DELETE /v1/events/{slug}/boost`.
- Access: `GET|POST /v1/events/{slug}/guestlist`, `POST /v1/events/{slug}/guestlist/dj`,
  `DELETE /v1/events/{slug}/guestlist/{guest_user_id}`,
  `PUT /v1/events/{slug}/guestlist/quotas/{artist_profile_id}` (alias `/guest-quotas/…`),
  `GET /v1/events/{slug}/door-staff`, `PUT /v1/events/{slug}/door-staff/by-username/{door_username}`,
  `DELETE /v1/events/{slug}/door-staff/{assignment_id}`, `GET /v1/events/{slug}/access`.
- Check-in: `POST /v1/events/{slug}/guestlist/me/qr-token` (alias `/check-in-tokens`),
  `POST /v1/events/{slug}/check-in` (alias `/check-ins/validate`).
- Internal: `POST /internal/v1/events/feed-candidates`, `POST /internal/v1/events/batch`,
  `POST /internal/v1/account-erasure`, `GET /internal/v1/mention-targets/events/{slug}`.

### 10.5 media and auth-gateway

- media: `POST /v1/assets`, `POST /v1/assets/upload`, `GET /v1/config/storage`,
  `GET /internal/v1/assets/{asset_id}`, `POST /internal/v1/account-erasure`,
  `GET /media/assets/{asset_path}` (paths must start with `assets/`; no `.`/`..` segments).
- auth-gateway: `GET /me` (Authentik bearer token → current profile via NATS), plus health.

### 10.6 Web BFF

Route handlers in `apps/web/src/app/api/`: `auth/*`, `me` (`DELETE` for erasure, `profile`, `artist`),
`onboarding`, `follow`, `blocks`, `reports`, `notifications/*`, `pages/*`,
`search/mentions`, `media/assets` (upload and `[...path]` proxy), `events/*` (event CRUD,
updates, follow, boost, viewer context, guestlist, quotas, QR token, door staff, check-in) and
`social/*` (feed, groups, posts, comments, reactions, emoji, reports, moderation, group
search). Server env: `USERS_SERVICE_URL`, `SOCIAL_SERVICE_URL`, `EVENTS_SERVICE_URL`,
`MEDIA_SERVICE_URL`, `THRESHOLD_INTERNAL_TOKEN`, `AUTH_COOKIE_SECURE`,
`WEB_TRUSTED_LAN_HTTP`, optional `WEB_TRUSTED_PROXY_DEPTH` (see `apps/web/.env.example`).

---

## 11. Test plan

- Python: `services/<svc>/tests/{unit,integration}`, `libs/py/tests`, `ops/tests`,
  `ci/woodpecker/tests` (pytest paths in `pyproject.toml`). Gates: `go-task py:lint`,
  `py:typecheck` (mypy strict), `py:test`, `service:test SERVICE=<name>`.
- Web: colocated `*.test.ts(x)` files run with `bun test`; gates `go-task web:lint`,
  `web:typecheck`, `web:test`, `web:build`, `web:audit`.
- Infra: `go-task infra:check` renders every kustomization and rejects inline Secret data.
- `go-task check` runs lint, typecheck, test and build.
- CI (`.github/workflows/ci.yml`): `security` (Gitleaks, Trivy, CI script self-tests),
  `infra`, `containers` (Docker builds), `python` (uv lock check, ruff, mypy, pytest), `web`
  (audit, bun test, tsc, eslint, next build), aggregated by `ci-ok`. Woodpecker per-service
  pipelines validate image builds.

---

## 12. References

- `AGENTS.md` — agent working rules, product constraints, commands; `apps/web/AGENTS.md` for web.
- `README.adoc` — project overview, prerequisites, quick start.
- `docs/architecture.md` — runtime platform, trust boundaries, design system notes.
- `docs/release-and-deploy.md` — automatic main deployment, rollback, deploy pitfalls, token rotation.
- `docs/rebranding.md` — retained legacy `threshold` identifiers.
- `docs/runbooks/` — backup/restore, Cloudflare tunnel, NetBird fwmark, observability
  alerting, OpenBao unseal, Woodpecker webhooks.
- `SECURITY.md` — vulnerability reporting.
