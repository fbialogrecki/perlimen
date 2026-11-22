#!/usr/bin/env bash
set -euo pipefail

fail() { printf 'Perlimen Lite: %s\n' "$*" >&2; exit 1; }
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
root=$(pwd -P)
instance=perlimen-lite-$(printf '%s' "$root" | sha256sum | cut -c1-16)
unit=$instance.service

case ${1:-} in
    start)
        (( EUID != 0 )) || fail 'Run as the NUC user, not root.'
        export PATH="$HOME/.bun/bin:$HOME/.local/bin:$PATH"
        for tool in bun uv podman pasta systemd-run systemd-notify curl openssl ss; do
            command -v "$tool" >/dev/null || fail "Missing $tool. See docs/runbooks/perlimen-lite.md."
        done
        if systemctl --user is-active --quiet "$unit"; then
            printf '%s is already running.\n' "$unit"
            exit 0
        fi
        systemd-run --user --unit="$unit" --collect --service-type=notify \
            --property=NotifyAccess=all --property=KillMode=mixed \
            --property=TimeoutStartSec=240 --property=TimeoutStopSec=25 \
            --property="StateDirectory=$instance" --property=StateDirectoryMode=0700 \
            --property="RuntimeDirectory=$instance" --property=RuntimeDirectoryMode=0700 \
            --setenv="PATH=$PATH" --expand-environment=no \
            bash -p "$root/ops/lite.sh" run "${PERLIMEN_LITE_WEB_PORT:-3300}" \
            "${PERLIMEN_LITE_API_PORT:-3800}" "${PERLIMEN_LITE_PG_PORT:-55432}" ||
            fail "Startup failed; see journalctl --user -u $unit -e."
        exit 0
        ;;
    stop)
        if [[ $(systemctl --user show -p LoadState --value "$unit") == not-found ]]; then
            exit 0
        fi
        exec systemctl --user stop "$unit"
        ;;
    run) [[ -n ${NOTIFY_SOCKET:-} && -n ${STATE_DIRECTORY:-} && -n ${RUNTIME_DIRECTORY:-} ]] || fail 'Use lite:start.' ;;
    *) fail 'Use lite:start or lite:stop.' ;;
esac

# Do not inherit service URLs, telemetry, credentials, dotenv loaders or proxy settings.
while IFS= read -r name; do
    case $name in
        HOME|USER|LOGNAME|PATH|LANG|XDG_RUNTIME_DIR|DBUS_SESSION_BUS_ADDRESS|NOTIFY_SOCKET|STATE_DIRECTORY|RUNTIME_DIRECTORY) ;;
        *) unset "$name" ;;
    esac
done < <(compgen -e)
umask 077
export TMPDIR="$RUNTIME_DIRECTORY"
host=127.0.0.1
web_port=$2 api_port=$3 pg_port=$4
for port in "$web_port" "$api_port" "$pg_port"; do
    [[ $port =~ ^[1-9][0-9]{3,4}$ ]] && (( port >= 1024 && port <= 65535 )) || fail 'Invalid port.'
    [[ -z $(ss -H -ltn "sport = :$port") ]] || fail "Port $port is in use. Choose another Lite port."
done
[[ $web_port != "$api_port" && $web_port != "$pg_port" && $api_port != "$pg_port" ]] || fail 'Use distinct ports.'
state=$(realpath -- "$STATE_DIRECTORY")
[[ $state != "$root" && $state != "$root/"* ]] || fail 'State must be outside the checkout.'
for file in apps/web/.env{,.local,.development,.development.local}; do
    [[ ! -e $file && ! -L $file ]] || fail "Remove $file from this Lite worktree (it is not loaded)."
done
[[ -f apps/web/node_modules/next/dist/bin/next ]] || fail 'Run bun install --frozen-lockfile in apps/web first.'
[[ -f perlimen_lite/main.py && -f perlimen_lite/migrate.py ]] || fail 'The modular Lite API is required in this checkout.'

secret() {
    if [[ ! -s $STATE_DIRECTORY/$1 ]]; then
        openssl rand -hex 32 > "$STATE_DIRECTORY/.$1.new"
        mv -- "$STATE_DIRECTORY/.$1.new" "$STATE_DIRECTORY/$1"
    fi
}
secret pg-password
secret internal-token
mkdir -p -- "$STATE_DIRECTORY/media"

pg=(podman --remote=false --root "$STATE_DIRECTORY/containers" --runroot "$RUNTIME_DIRECTORY/containers")
pg_pid= web_pid= api_pid= migration_pid=
cleanup() {
    local pid
    for pid in "$web_pid" "$api_pid" "$migration_pid"; do
        [[ -z $pid ]] || kill -TERM "$pid" 2>/dev/null || true
    done
    if [[ -n $pg_pid ]]; then
        "${pg[@]}" stop --ignore --time=10 postgres >/dev/null
        wait "$pg_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 0' TERM INT

"${pg[@]}" run --rm --name postgres --cgroups=disabled --sdnotify=ignore --network=pasta \
    --publish "$host:$pg_port:5432" --http-proxy=false \
    --volume pgdata:/var/lib/postgresql/data \
    --volume "$STATE_DIRECTORY/pg-password:/run/pg-password:ro,Z" \
    --env POSTGRES_PASSWORD_FILE=/run/pg-password --env POSTGRES_USER=lite_bootstrap \
    --env POSTGRES_DB=perlimen_lite docker.io/library/postgres:17-alpine &
pg_pid=$!

await() {
    local stage=$1 pid=$2 seconds=$3 deadline=$((SECONDS + $3))
    shift 3
    until "$@" >/dev/null 2>&1; do
        kill -0 "$pid" 2>/dev/null && (( SECONDS < deadline )) || fail "$stage did not become ready within ${seconds}s."
        sleep 1
    done
    kill -0 "$pid" 2>/dev/null || fail "$stage exited during startup."
}
await PostgreSQL "$pg_pid" 90 "${pg[@]}" exec postgres pg_isready -h "$host" -U lite_bootstrap -d perlimen_lite

api_env() {
    local variable
    for variable in PERLIMEN_LITE_{USERS,SOCIAL,EVENTS,MEDIA}_RUNTIME_PASSWORD "$@"; do
        secret "$variable"
        export "$variable=$(< "$STATE_DIRECTORY/$variable")"
    done
    export PERLIMEN_LITE_DATABASE_URL="postgresql://$host:$pg_port/perlimen_lite"
    export PERLIMEN_LITE_MEDIA_ROOT="$STATE_DIRECTORY/media"
}

export THRESHOLD_INTERNAL_TOKEN="$(< "$STATE_DIRECTORY/internal-token")"
(
    api_env PERLIMEN_LITE_{USERS,SOCIAL,EVENTS,MEDIA}_MIGRATION_PASSWORD
    export PERLIMEN_LITE_BOOTSTRAP_PASSWORD="$(< "$STATE_DIRECTORY/pg-password")"
    exec uv run --frozen --no-dev --python 3.13 python -m perlimen_lite.migrate
) &
migration_pid=$!
wait "$migration_pid" || fail 'Local migrations failed.'
migration_pid=
(
    api_env THRESHOLD_AUTH_PASSWORD_PEPPER_CURRENT THRESHOLD_AUTH_SESSION_TOKEN_HMAC_KEY
    exec uv run --frozen --no-dev --python 3.13 python -m perlimen_lite.serve "$api_port"
) &
api_pid=$!
http=(curl -q --noproxy '*' --fail --silent --max-time 3)
api_url=http://$host:$api_port
await API "$api_pid" 45 "${http[@]}" "$api_url/readyz"

(
    cd apps/web
    export USERS_SERVICE_URL="$api_url/users" SOCIAL_SERVICE_URL="$api_url/social"
    export EVENTS_SERVICE_URL="$api_url/events" MEDIA_SERVICE_URL="$api_url/media"
    export PERLIMEN_LITE_API_PORT="$api_port"
    export PERLIMEN_LITE_BFF_LOG="$RUNTIME_DIRECTORY/bff.jsonl"
    : > "$PERLIMEN_LITE_BFF_LOG"
    export NODE_ENV=development NEXT_TELEMETRY_DISABLED=1 AUTH_COOKIE_SECURE=true WEB_TRUSTED_LAN_HTTP=false
    # Next development output can include URLs, fetch details and raw errors.
    exec bun ./node_modules/next/dist/bin/next dev --hostname "$host" --port "$web_port" >/dev/null 2>&1
) &
web_pid=$!
await web "$web_pid" 75 "${http[@]}" "http://$host:$web_port/"
kill -0 "$pg_pid" "$api_pid" 2>/dev/null || fail 'A component exited during startup.'
printf 'Web http://%s:%s · API %s · PostgreSQL %s:%s\n' "$host" "$web_port" "$api_url" "$host" "$pg_port"
systemd-notify --ready --status="Web http://$host:$web_port/ · API $api_url/"
wait -n "$web_pid" "$api_pid" "$pg_pid" || true
fail 'A component exited; stopping the stack.'
