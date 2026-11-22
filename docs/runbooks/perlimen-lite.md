# Perlimen Lite local development on the NUC

Run this on the NUC as your regular user, from a dedicated checkout that contains
both the Lite lifecycle commands and the modular API (`perlimen_lite/`). This is
for synthetic data and loopback HTTP only. Do not attach a tunnel, proxy or LAN
listener to it. It does not replace or manage the k3s deployment.

Prerequisites: `go-task`, Bash, a running systemd user manager, rootless Podman
(with `pasta`), Python 3.13/uv, Bun, curl, OpenSSL and `ss`. Install the locked web
dependencies in the checkout once, and again when the lockfile changes:

```bash
(cd apps/web && bun install --frozen-lockfile)
go-task lite:start
go-task lite:stop
```

Start waits for PostgreSQL, runs the modular API's migrations, then checks the
real API `/readyz` and the Next.js landing page. uv uses the frozen lockfile.
The first run may download the PostgreSQL image and Python dependencies. Start
against a running checkout is a no-op. Stop removes the running container and
stops the host processes, but keeps the database, media directory and credentials.
Only PostgreSQL is containerized. There is no production Next.js build in this
command.

| Component | Default listener | Port override at start |
|---|---|---|
| Next.js / Bun | `http://127.0.0.1:3300` | `PERLIMEN_LITE_WEB_PORT` |
| FastAPI | `http://127.0.0.1:3800` | `PERLIMEN_LITE_API_PORT` |
| PostgreSQL 17 | `127.0.0.1:55432` | `PERLIMEN_LITE_PG_PORT` |

The user service name is `perlimen-lite-<checkout-id>.service`, printed at start.
Inspect it with `systemctl --user status <unit>` and `journalctl --user -u <unit>`.
A component exit stops the rest of the stack; the service does not automatically
restart. Stop before changing the ports. Moving the checkout changes its ID;
stop it **before** moving it. The stack runs with the user manager, not as a boot
service.

State is private to the user under `$XDG_STATE_HOME/perlimen-lite-<checkout-id>`
(default `~/.local/state/…`), outside Git. It includes the dedicated rootless
Podman store and PostgreSQL volume, local media directory and generated dev-only
secrets. Keep the entire directory together: deleting a credential is not a
rotation. No `.env` files are needed; the launcher refuses web dotenv files it
would otherwise load and does not inherit legacy service configuration. It does
not enable remote email, telemetry or object storage. The launcher sets
`PERLIMEN_LITE_API_PORT` for the BFF so product-auth requests send the internal
token only to `http://127.0.0.1:<port>/users` and never follow redirects. The
standalone users service does not use this mode. The launcher sets `NEXTAUTH_URL`
to the loopback web origin for the exact Origin check and absolute Open Graph
image URLs. This variable does not enable Authentik or any other login provider.
SMTP is off by default. For synthetic accounts on a disposable checkout only,
set `PERLIMEN_LITE_SMTP_CAPTURE_PORT` at start to the port of a non-relaying SMTP
sink bound to `127.0.0.1`. Lite uses no SMTP credentials and builds both reset
and verification links from the Next.js origin, including the selected web port.
Stop before changing this setting. Never forward captured mail or use real accounts.

The default Lite start runs only Next.js, the Lite API and PostgreSQL: no Tempo,
OTLP export, Grafana datasource, Mimir, Loki, Grafana, Alloy, Collector or
Prometheus, and no NATS broker, Authentik or auth-gateway. The
[optional gateway](../../services/auth-gateway/README.md#optional-lite-configuration) is separate
from product login and Feed. The API writes JSON request IDs, route templates, status and
timings to the unit journal. While the service is up, read the private BFF timing file
at `$XDG_RUNTIME_DIR/<checkout-instance>/bff.jsonl` (removed on stop). It times
`/app`, check-in and upload from Proxy ingress to App Router response close;
matching API rows share the generated request ID. Lite discards raw Next.js
development output and disables its local span writer and MCP log file. This
does not remove any traces or logs left by a previous non-Lite or unpatched run.
Diagnostic records omit request values and are not the product audit:
audit rows stay in their originating PostgreSQL schemas under existing access rules.

Leave the optional gateway off unless testing operator SSO. Use only a private loopback
listener, an isolated issuer and synthetic identities. Require a dedicated Authentik admin
audience and an operator-only IdP policy that denies issuance to non-operators; test both
outcomes before enabling the gateway. Confirm `/me` returns only the verified identity and
sets no cookie. Before registering a product account, the operator's username must return
404 from the public product profile lookup; ordinary registration with the same username and
email must then succeed. Turning the gateway off does not affect product login or Feed.

On a **fresh disposable checkout** containing both changes, run
`bash ops/tests/lite-smoke.sh --disposable` (requires `python3`). It checks
loopback binding, Next.js and API readiness, and synthetic registration and
login through Next.js. It checks the journal, BFF timings and Next diagnostic
files for synthetic reset and verification link values while running and after
stop. Rerun this smoke test when upgrading the pinned Next.js version. A second
start preserves the running PID, and login returns the same account after
stop/start. The test checks listener/cgroup cleanup and leaves the isolated volume
stopped, not deleted. It does not test browser cookie handling, refresh rotation
or media uploads.

For the reset regression, use a separate fresh disposable checkout with locked web
dependencies and a Python environment with Playwright and its Chromium installed:

```bash
python -m playwright install chromium
python ops/tests/lite-reset-smoke.py --disposable
```

The test supplies an in-memory loopback SMTP sink, starts Lite on a nondefault
web port, and opens the exact mailed verification and reset links in Chromium.
It submits the real reset form and verifies single use, session revocation and
login with the new password. After shutdown it checks the unit journal and any
Next dev trace/log artifacts for both credentials, reporting only counts and
booleans. The disposable database remains stopped, not deleted.

## Disposable rollout and rollback rehearsal

From a trusted checkout containing `ci/lite/verify_main_ci.py`, with up-to-date
`origin/main` and the prerequisites above, run:

```bash
python3 -I ops/tests/lite-rollout-drill.py --disposable <baseline-sha> <candidate-sha>
```

Use two full, compatible main-push SHAs, oldest first. The command replays the
candidate event once, without a release or a persistent watcher. It verifies
exact-SHA `ci-ok` before running either revision, rejects changes to migration
and dependency inputs, and rolls back to the known-good revision after the
candidate check, including on candidate startup or health failure. There is
brief downtime between revisions; database migrations are not downgraded.

The drill uses a fresh checkout-specific PostgreSQL store, generated dev-only
credentials and three loopback ports. It checks the live listeners' process
ownership and checkout SHA, API readiness, and the same synthetic account
through the web BFF at every stage. It stops the transient unit and removes the
checkout; the private synthetic state directory printed at startup stays stopped.
This is not a permanent deployment mechanism or a replacement for legacy CI/CD.

## Automatic Lite deployment (dedicated NUC account)

The optional user timer polls the public Perlimen repository every five minutes.
It considers only the observed `main` tip, then requires the existing GitHub
Actions workflow's successful **main-push** `ci-ok` for that exact SHA and run
attempt, which includes the security scans. After reading the exact attempt's
jobs, the verifier reads the latest exact main-push run again and requires the
same run and attempt to still be green. After readiness the controller requires
the same successful proof as before the trial, then rechecks the `main` tip.
A green PR, changed proof or tip, or unreadable response cannot authorize
promotion. Intermediate pushes can be superseded. No new workflow, webhook,
runner, or deployment credential is needed; Woodpecker, Harbor and Argo CD are
unchanged.

### Coordinator setup

- Use a dedicated, unprivileged account with private home, its own rootless
  Podman/subuid/subgid configuration, and the dependencies listed above. Do not
  grant sudo, k3s or legacy access, or install SSH/GitHub keys, registry secrets,
  a credential helper or a shared service socket. The user manager needs to stay
  up across logouts and reboots; the host operator can enable lingering for
  this account. The host needs outbound access for public GitHub and the locked
  dependency/image downloads, but no public or LAN listener or tunnel.
- Check Python **as the dedicated account**, with its real private `HOME`, before
  enabling intake. Use the timer's search path, not a login shell's virtualenv
  or `UV_*` settings:

  ```bash
  cd "$HOME" || exit 1
  lite_uv() { env -i HOME="$HOME" PATH="$HOME/.bun/bin:$HOME/.local/bin:/bin:/usr/bin" uv "$@"; }
  lite_uv python find --no-python-downloads 3.13
  ```

  If missing, run `lite_uv python install 3.13` and repeat the lookup.
  Having `uv` on the system or Python in the maintainer's home is not enough:
  PostgreSQL can become ready before migrations discover the missing interpreter.
  If a trial was already rejected, leave its blocked SHA and data intact; the
  timer will consider the next eligible main tip.
- Reserve **13300 (Web), 13800 (API), 15432 (PostgreSQL)** on loopback. Verify all
  three are free before first start. Use only synthetic data. Do not reuse any
  existing checkout, credentials, database or media directories.
- The automatic target requires rootless Linux user, mount and network
  namespaces, util-linux `unshare` with `--map-root-user` and `--map-auto`,
  `nsenter`, and `pasta` with `--host-lo-to-ns-lo` and `--no-netns-quit` support.
  The controller creates fresh namespaces without a Podman namespace keeper
  and runs rootless Podman directly, with target-private temporary files and
  file locks. Before the checkout's install or launch commands run, it verifies
  the namespaces and loopback forwards. It forwards only the three TCP ports
  to host IPv4 loopback. Startup fails without this boundary; the network
  namespace is not a sandbox for untrusted code.
- Stage the following four files from a reviewed, successfully checked **merged
  main** revision. Run these commands as the dedicated account from the staged
  tree. Install trusted control files outside the managed checkout and update
  them only from another reviewed merged revision while intake is stopped.

```bash
install -d -m 0700 "$HOME/.local/lib/perlimen-lite-pull" "$HOME/.config/systemd/user"
install -m 0600 ci/lite/{pull_main,verify_main_ci}.py "$HOME/.local/lib/perlimen-lite-pull/"
install -m 0600 ops/systemd/perlimen-lite-pull.{service,timer} "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now perlimen-lite-pull.timer
systemctl --user list-timers perlimen-lite-pull.timer
journalctl --user -u perlimen-lite-pull.service -e
```

On first eligible poll the controller creates a checkout and fresh Lite store.
Each candidate gets a separate checkout; the recorded current and previous
checkouts remain available for recovery. The service keeps the same private
PostgreSQL/media store across switches. No two polls or rollbacks run concurrently.
`release.json` records current/previous checkout, SHA and CI evidence. After
checking the namespace, listener PIDs, running checkout, Web `/` and API `/readyz`,
the controller revalidates the CI proof and finally reads remote `main` before
promoting. Changed or unavailable proof or tip restores the last healthy release
(or stops the first trial) without promotion, blocking the candidate SHA, or
setting a hold. Synthetic BFF login is a separate operator acceptance check.

### Verify, roll back and stop

Use the same dedicated account and systemd user environment (commands in Bash):

```bash
ctl="$HOME/.local/lib/perlimen-lite-pull/pull_main.py"
state="$(systemd-path user-state-private)/perlimen-lite-pull"
python3 -I "$ctl" --state "$state" status
python3 -I "$ctl" --state "$state" rollback
python3 -I "$ctl" --state "$state" status
```

`rollback` takes the previous recorded healthy SHA, rechecks compatibility and
holds automatic promotion **before** the switch. `status` must report that SHA
in both the record and the live check; verify the same synthetic account still
works. On first deployment there is no previous SHA. If a candidate fails health
or is interrupted, the prior committed release remains authoritative; the next
poll restores it if necessary without consulting GitHub. A confirmed activation
failure blocks that SHA; an interrupted trial or a failed remote recheck can be retried.
Recovery still needs the installed toolchain, dependency cache and
local PostgreSQL image (or access to download missing artifacts).
A rollback hold survives restarts; it still lets the timer restore the held
release. After triage, lift the hold with:

```bash
python3 -I "$ctl" --state "$state" resume
```

The next poll may redeploy the same `main` tip if it is still eligible and was
rolled back manually rather than blocked by a failed activation. To take this
target offline, disable the timer, stop any active poll, then stop the Lite
unit through the controller:

```bash
systemctl --user disable --now perlimen-lite-pull.timer
systemctl --user stop perlimen-lite-pull.service
python3 -I "$ctl" --state "$state" stop
```

The stop keeps the release record, checkouts, and target-specific Lite data and
credentials. The controller never resets or deletes an inactive checkout. After
stopping the target, inspect inactive worktrees under `releases/` before pruning
them; retain the current and previous worktrees from `release.json`. There is
brief downtime during a switch; this is a single loopback target, not a
production Next build, sandbox for untrusted code, or high-availability service.
Migration/schema inputs, launcher and lock/dependency inputs must match the
known release in both directions. A change to those inputs needs separate
qualification on a fresh isolated target; never force a switch, downgrade the
DB, or delete the existing store. Application data is not restored to a point in
time by a code rollback.

## Paired backup and restore rehearsal

Use two dedicated loopback-only Lite PostgreSQL 17 instances, separate volumes,
separate private media roots and synthetic accounts only. This procedure does
not use or replace CNPG, PVCs or legacy backup jobs. Restore only an archive
pair you created and trust. Record the source checkout SHA for the rehearsal
and use that revision for restore verification.

1. In the source Lite API, register a control account and one to erase. Upload
   an image for each and record the control asset's logical keys, sizes and
   SHA-256 digests for the original and both derivatives. Request account
   erasure and wait for completion, not just HTTP `202`. Stop every source
   writer (including the API's erasure worker) before starting the dump, and
   keep them stopped until both captures finish. Leave **only** the dedicated
   source PostgreSQL running: its snapshot does not include the media files.
2. Verify `/mnt/backup` is mounted on a different physical disk from the source
   database and media. In Bash, set `SRC_MEDIA` to the private media root and
   `SRC_PG_PORT` to the verified source loopback port. Use PostgreSQL 17
   clients and configure bootstrap authentication with a private libpq
   password file, not command-line secrets. Clients can also run inside the
   matching container; redirect stdout to the host backup directory.

   ```bash
   set -euo pipefail
   umask 077
   export PGHOSTADDR=127.0.0.1
   findmnt --mountpoint /mnt/backup
   src=(--host=127.0.0.1 --port="${SRC_PG_PORT:?}" --username=lite_bootstrap --no-password)
   backup=$(mktemp -d /mnt/backup/perlimen-lite-XXXXXXXX)
   pg_dump "${src[@]}" --format=custom --dbname=perlimen_lite > "$backup/lite.dump"
   tar -C "${SRC_MEDIA:?}" -cf "$backup/media.tar" .
   (cd "$backup" && sha256sum lite.dump media.tar > SHA256SUMS)
   sync -f "$backup"
   ```

3. On the new target instance initialize only the administrative database
   `postgres` with `lite_bootstrap`. Confirm that `perlimen_lite` does not
   exist. Before restore, create the eight named roles
   `lite_{users,social,events,media}_{migration,runtime}` as `LOGIN NOINHERIT`
   with distinct SCRAM passwords and no elevated attributes or memberships.
   The database dump does not contain these role definitions or server-side
   auth keys. Supply DB credentials and auth keys separately; use the same
   synthetic password pepper and session HMAC key to check existing accounts
   and sessions. Follow the [Lite backend environment contract](../perlimen-lite-backend.md).
   Do not run migrations on the target before restoring.
4. Set `DST_PG_PORT` to the verified **target** loopback port and `backup` to
   the paired backup directory. Set `DST_MEDIA` to a new directory path outside
   the checkout and any web root. Run as the destination API's OS user.
   Keep owners and privileges on restore; do not add `--clean`, `--no-owner`
   or `--no-acl`. If verification or either restore step fails, leave the
   target offline and retry with new empty targets. Preserve the source and
   all backup files.

   ```bash
   set -euo pipefail
   umask 077
   export PGHOSTADDR=127.0.0.1
   dst=(--host=127.0.0.1 --port="${DST_PG_PORT:?}" --username=lite_bootstrap --no-password)
   test "$(psql "${dst[@]}" -XAt -v ON_ERROR_STOP=1 --dbname=postgres \
       -c "SELECT EXISTS (SELECT FROM pg_database WHERE datname = 'perlimen_lite')")" = f
   (cd "${backup:?}" && sha256sum --strict --check SHA256SUMS)
   mkdir -m 700 "${DST_MEDIA:?}"
   pg_restore "${dst[@]}" --create --exit-on-error --dbname=postgres < "$backup/lite.dump"
   tar --extract --same-permissions --no-same-owner \
       --file "$backup/media.tar" -C "$DST_MEDIA"
   ```

5. Start the restored API on loopback with **only** its runtime credentials.
   Require `/readyz` to pass and check that runtime roles cannot read sibling
   schemas or change DDL or migration ledgers. Match Media rows and their
   logical keys and sizes to the `0600` files in the `0700` directory, and
   compare source and restored digests. Verify control login and owner reads
   of both derivatives; the original must be verified on disk, never served
   over HTTP. Check that the erased account cannot sign in and its prior
   session, blob files and media inventory have not returned. Stop both
   disposable instances without deleting volumes or copies.

A matched restore proves the state of that backup, not erasures requested
later. Never reconnect a restore made before an erasure to users: reconcile
those requests and verify deletion offline first. This synthetic drill does
not establish a schedule, retention policy or production recovery procedure.
