# Perlimen Lite backend

Lite is an opt-in local backend, not a change to the deployed stack. Run it in its own
process from the checkout root after `uv sync --frozen`. No existing database or data
volume can be used for initialization.

## PostgreSQL and API

On dedicated PostgreSQL storage, initialize database `perlimen_lite` with bootstrap
login `lite_bootstrap`. For the Postgres image, these are `POSTGRES_DB` and
`POSTGRES_USER`; `POSTGRES_PASSWORD` is the same value as `PERLIMEN_LITE_BOOTSTRAP_PASSWORD`.
Publish Postgres to `127.0.0.1` only and keep its TCP authentication on SCRAM-SHA-256,
not `trust`. Supply configuration outside git:

| Variable | Consumer |
| --- | --- |
| `PERLIMEN_LITE_DATABASE_URL` = `postgresql://127.0.0.1:<port>/perlimen_lite` (no credentials or query parameters) | Migrator and API |
| `PERLIMEN_LITE_{USERS,SOCIAL,EVENTS,MEDIA}_RUNTIME_PASSWORD` (one variable per domain) | Migrator and API |
| `PERLIMEN_LITE_{USERS,SOCIAL,EVENTS,MEDIA}_MIGRATION_PASSWORD` (one variable per domain) | Migrator only |
| `PERLIMEN_LITE_BOOTSTRAP_PASSWORD` | Migrator only |
| `THRESHOLD_INTERNAL_TOKEN` | API, and the trusted BFF when connected |
| `PERLIMEN_LITE_MEDIA_ROOT` | API; existing directory owned by the API user, mode `0700` |

Generate distinct role passwords and reuse them on each start. The migrator does not
rotate passwords. Invoke these commands in their respective environments:

```sh
.venv/bin/python -m perlimen_lite.migrate
.venv/bin/python -m perlimen_lite.serve 8000
```

The migrator provisions schemas and roles under a PostgreSQL advisory lock, then
runs the packaged Alembic history once per domain from that domain's migration
login. DDL and revision tracking commit per schema. Re-running continues from the
recorded revisions. Runtime roles use only their domain's tables and read their
Alembic ledger to check readiness; they cannot change the schema or ledger. IDs
in other domains remain logical references, not cross-schema foreign keys. Do not
pass a caller's SQLAlchemy session to another domain.

The single app has `/healthz` and `/readyz`; the latter requires all four heads and a
private media directory. Domain HTTP routes retain their payloads and authorization
under `/users`, `/social`, `/events`, and `/media`. The Lite listener expects the
internal token even on `/users`; this supplements, not replaces, product sessions
and each router's own authorization. Do not expose the listener to browsers.
Lite sets and clears `threshold_refresh` at `/users/v1/auth`; standalone users
uses `/v1/auth`. The BFF translates both paths to `/api/auth`, including clearing.

The Lite launcher connects the BFF directly to the private listener, not to legacy services.
Social reads follows and both directions of blocks through users-owned calls, using the users
runtime role. It filters Feed candidates in SQL before applying the cursor and page limit, then
batches interaction data for the resulting page. Group and thread reads and new interactions
also honor blocks. Mentions and notification writes use the owner APIs without HTTP or NATS.
The Lite migrator removes the legacy Social block projection under the Social migration role;
Lite starts neither the subscriber nor the repair loop. Standalone keeps its existing transport.

Events resolves Page roles, artists and door staff and delivers notifications through Users;
it publishes city announcements through Social, using separate owner sessions. An exact
create retry by the event's creator still returns the existing slug conflict but can replay
Social delivery for the same event ID. Social's unique event-to-post mapping prevents
duplicate announcements. Delivery is best effort when Social or the official group is unavailable.
Guestlist and QR operations keep Events' authorization and transaction rules in both profiles.

Other cross-domain integrations can still be unavailable: legacy outbound URLs are disabled
in this process. The existing users erasure worker is registered with the users
session factory, but end-to-end erasure is not enabled by this alone.
The isolated smoke test asserts real PostgreSQL behavior, not whole-product readiness.

## Broker and operator SSO

Lite runs no NATS broker, enables no NATS clients, and does not load the standalone service
lifespans. The standalone NATS clients and deployment stay in place:

| NATS subject | Standalone client and handler | Lite path |
| --- | --- | --- |
| `users.current_profile.v1` | auth-gateway request → Users responder | Optional gateway calls `POST /users/internal/v1/current-profile` over HTTP with the internal token |
| `users.follow.list_following.v1` | Social request → Users responder | Social reads the canonical follows through the Users owner API |
| `users.block.changed.v1` | Users publisher → Social subscriber (standalone also repairs via HTTP) | Social reads canonical blocks through the Users owner API; no projection or repair loop |

Events, Media and the web BFF use no NATS client. `nats-box` and NATS monitoring belong to the
legacy cluster, not the Lite launcher.

Product email/password sessions work without Authentik or the gateway. If run separately for
operator SSO, the gateway accepts an Authentik bearer for the dedicated admin audience, not a
product cookie. Restrict the Authentik application to operators; `aud` identifies the intended
application, not a user's membership in a group. The gateway resolves an internal profile and
does not mint a product cookie, grant product roles or replace the owning domain's authorization.
See [auth-gateway configuration](../services/auth-gateway/README.md#optional-lite-configuration).

## Media

Lite reuses the bounded upload and WebP pipeline. Store the media root outside the checkout
and any public web root. The API writes each blob as a `0600` file under the `0700` media
root and does not follow blob symlinks. PostgreSQL holds the owner, context, keys and status;
backup/restore needs both the database and media directory. No S3 service, credentials or
media worker are needed. Standalone media still uses S3.

Upload returns the existing asset and derivative metadata; the bucket is the logical label
`lite-local` and object keys never expose host paths. Through the BFF, owners can preview
derivatives; other signed-in users can read visible post images unless either user blocked the other.
Only avatars linked to public profiles or Pages and posters linked to non-deleted public
events can be read anonymously. Originals are never served in Lite; all reads use `no-store`.
The media-owned erasure stage commits the owner tombstone before attempting file deletion,
then removes originals and derivatives before dropping the inventory. If the directory is
unavailable, the tombstone still revokes access and fences writes; the inventory stays for retry.

## Verify

With a **fresh disposable instance**, supply the migrator and API variables above
(the test supplies the media directory and internal token), then run:

```sh
PERLIMEN_LITE_TEST_FRESH_POSTGRES=1 uv run --frozen python -m pytest -q perlimen_lite/tests
```

The test rejects an already populated schema, runs the migration CLI twice,
checks own-schema access and sibling/DDL denials, then exercises the composed app. It
compares SQL query counts for one versus multiple Feed posts and checks that the canonical
block takes effect immediately with no Social projection table.
It also verifies announcement replay and races two authorized scans on separate PostgreSQL
connections, checking the committed admission and audit row. It creates synthetic accounts;
destroy only that disposable test instance afterwards.
