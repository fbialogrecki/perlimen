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

ready() {
    curl -qfsS --noproxy '*' --max-time 10 "http://127.0.0.1:$PERLIMEN_LITE_WEB_PORT/" -o /dev/null
    curl -qfsS --noproxy '*' --max-time 5 "http://127.0.0.1:$PERLIMEN_LITE_API_PORT/readyz" -o /dev/null
    for port in "${ports[@]}"; do
        [[ $(ss -H -ltn "sport = :$port" | awk '{print $4}') == "127.0.0.1:$port" ]]
    done
    [[ $("${pg[@]}" ps --format '{{.Names}}') == postgres ]]
}

auth() {
    python3 -I - "$1" <<'PY'
import json
import os
import sys
from http.client import HTTPConnection
from http.cookies import SimpleCookie

port = int(os.environ["PERLIMEN_LITE_WEB_PORT"])
username = "lite-smoke-user"
password = "Dev-Smoke-Only-123!"
step = sys.argv[1]
payload = {"password": password}
if step == "register":
    payload.update(username=username, email=f"{username}@example.test")
else:
    payload["emailOrUsername"] = username

web = HTTPConnection("127.0.0.1", port, timeout=20)
web.request("POST", f"/api/auth/{step}", json.dumps(payload), {
    "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}",
})
response = web.getresponse()
expected = 201 if step == "register" else 200
if response.status != expected:
    sys.exit(f"BFF {step}: expected HTTP {expected}, got {response.status}")
cookies = SimpleCookie()
for name, value in response.getheaders():
    if name.lower() == "set-cookie":
        cookies.load(value)
for name in ("threshold_session", "threshold_refresh"):
    cookie = cookies.get(name)
    if not cookie or not (cookie.value and cookie["httponly"] and cookie["secure"]):
        sys.exit(f"BFF {step}: missing secure HttpOnly {name} cookie")
user = json.load(response)["user"]
if user["username"] != username or not user["id"]:
    sys.exit(f"BFF {step}: missing synthetic user")
print(user["id"])
PY
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
registered=$(auth register)
logged_in=$(auth login)
[[ $logged_in == "$registered" ]]
go-task lite:start
[[ $(main_pid) == "$pid" ]]
stopped "$group"

go-task lite:start
ready
logged_in=$(auth login)
[[ $logged_in == "$registered" ]]
stopped "$(stack_group)"
printf 'Lite lifecycle smoke passed (loopback, readiness, BFF register/login, account persistence, and clean stop).\n'
