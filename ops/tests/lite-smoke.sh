#!/usr/bin/env bash
# Run only in a disposable Lite checkout containing both the runtime and modular API.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
[[ ${1:-} == --disposable ]] || { printf 'Use --disposable in a fresh Lite checkout.\n' >&2; exit 2; }
instance=perlimen-lite-$(printf '%s' "$(pwd -P)" | sha256sum | cut -c1-16)
unit=$instance.service
state=${XDG_STATE_HOME:-$HOME/.local/state}/$instance
runtime=${XDG_RUNTIME_DIR:?}/$instance
[[ ! -e $state && $(systemctl --user show -p LoadState --value "$unit") == not-found ]]
trap 'go-task lite:stop' EXIT

export PERLIMEN_LITE_WEB_PORT=${PERLIMEN_LITE_WEB_PORT:-58300}
export PERLIMEN_LITE_API_PORT=${PERLIMEN_LITE_API_PORT:-58380}
export PERLIMEN_LITE_PG_PORT=${PERLIMEN_LITE_PG_PORT:-58432}
ports=("$PERLIMEN_LITE_WEB_PORT" "$PERLIMEN_LITE_API_PORT" "$PERLIMEN_LITE_PG_PORT")
pg=(podman --remote=false --root "$state/containers" --runroot "$runtime/containers")
psql=("${pg[@]}" exec postgres psql -XqAt -v ON_ERROR_STOP=1 -U lite_bootstrap -d perlimen_lite)

ready() {
    curl -qfsS --noproxy '*' --max-time 10 "http://127.0.0.1:$PERLIMEN_LITE_WEB_PORT/" -o /dev/null
    curl -qfsS --noproxy '*' --max-time 5 "http://127.0.0.1:$PERLIMEN_LITE_API_PORT/readyz" -o /dev/null
    for port in "${ports[@]}"; do
        [[ $(ss -H -ltn "sport = :$port" | awk '{print $4}') == "127.0.0.1:$port" ]]
    done
    [[ $("${pg[@]}" ps --format '{{.Names}}') == postgres ]]
}

stopped() {
    local group=$1
    go-task lite:stop
    [[ ! -e /sys/fs/cgroup$group && ! -e $runtime ]]
    for port in "${ports[@]}"; do
        [[ -z $(ss -H -ltn "sport = :$port") ]]
    done
}

main_pid() { systemctl --user show "$unit" --value -p MainPID; }
stack_group() { systemctl --user show "$unit" --value -p ControlGroup; }

go-task lite:start
ready
pid=$(main_pid)
group=$(stack_group)
[[ $pid != 0 && -n $group && -r /sys/fs/cgroup$group/cgroup.procs ]]
[[ $(stat -c '%a' "$state") == 700 && $(stat -c '%a' "$state/pg-password") == 600 ]]
"${psql[@]}" -c "COMMENT ON DATABASE perlimen_lite IS 'lite lifecycle smoke';"
go-task lite:start
[[ $(main_pid) == "$pid" ]]
stopped "$group"

go-task lite:start
ready
[[ $("${psql[@]}" -c "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = current_database();") == 'lite lifecycle smoke' ]]
"${psql[@]}" -c 'COMMENT ON DATABASE perlimen_lite IS NULL;'
stopped "$(stack_group)"
printf 'Lite lifecycle smoke passed (loopback, readiness, restart persistence, and clean stop).\n'
