# Perlimen Lite product contracts

Run this suite against a **disposable, loopback-only web/BFF instance** with a
fresh database, private test media storage, the official Warsaw group fixture
and the erasure worker running. Disable outgoing mail or use a local capture
sink. Do not point it at a tunnel, a port-forward or an instance backed by real
data. The test client neither starts services nor reads cluster or runtime
credentials. It creates up to five synthetic accounts, posts, events and media
through product routes and deletes one account. Discard the test instance and
its data afterwards; restart the web process before re-running (registration is
rate limited).

From the repository root, with the Python workspace dependencies installed:

```sh
uv run --frozen python tests/lite/product_contract.py \
  --origin http://127.0.0.1:3000 --disposable
```

Supply the actual loopback port. HTTP requires the runtime's explicit trusted
local-HTTP cookie configuration. To verify `Secure` cookies too, run against a
local HTTPS listener using `--origin https://127.0.0.1:<port> --ca-file <ca.pem>`;
the client never disables certificate verification. Add a unittest name such as
`ProductContract.test_session_rotation_origin_and_revocation` to run one case.
`--erasure-wait` changes the default 30-second convergence window if needed.
Set the web process's `NEXTAUTH_URL` to the same origin so the profile's Open
Graph image URL points to the disposable instance.

The suite checks raw anonymous SSR HTML and Open Graph tags; protected `/app`
and the posts API; login, Origin, cookie scope, rotation and revocation; followed
and group Feed items and the first API and full HTML reads after a block, with
an unblocked author as a control; event roles, one city announcement on publish
retry, private guestlists, assigned staff and single-use event-bound QR; upload
validation, ownership, private attachments and the
anonymous Open Graph WebP; and immediate session revocation followed by
asynchronous erasure of identity markers from the retained post payload and
thread HTML. It doesn't require browser hydration and doesn't prove DB lock
behavior, restart recovery, physical file deletion or runtime performance.
Use the owning modules' tests and the Lite integration gates for those checks.

The full suite is a gate for the composed Lite runtime, not the legacy transport:
the existing stack with NATS disabled cannot supply followed authors or the
next-read canonical block guarantee. The legacy media route does not enforce
Lite's anonymous asset visibility rule. Those checks are assertions, not skips.
