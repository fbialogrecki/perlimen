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
Prometheus. The API writes JSON request IDs, route templates, status and timings
to the unit journal. While the service is up, read the private BFF timing file
at `$XDG_RUNTIME_DIR/<checkout-instance>/bff.jsonl` (removed on stop). It times
`/app`, check-in and upload from Proxy ingress to App Router response close;
matching API rows share the generated request ID. Lite discards raw Next.js
development output and disables its local span writer and MCP log file. This
does not remove any traces or logs left by a previous non-Lite or unpatched run.
Diagnostic records omit request values and are not the product audit:
audit rows stay in their originating PostgreSQL schemas under existing access rules.

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
