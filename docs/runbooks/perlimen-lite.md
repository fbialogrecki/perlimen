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
not enable remote email, telemetry or object storage. `/readyz` plus the landing
page verifies startup, not authenticated browser flows or media uploads.

On a **fresh disposable checkout** containing both changes, run
`bash ops/tests/lite-smoke.sh --disposable` to verify binding, startup, repeated
start, a PostgreSQL marker surviving stop/start, and listener/cgroup cleanup.
This leaves the checkout's isolated test volume stopped, not deleted.
