"""Open a locally mailed Lite reset link in Chromium and submit the real form."""

import argparse
import hashlib
import os
import secrets
import socketserver
import subprocess
from email import policy
from email.parser import BytesParser
from pathlib import Path
from queue import Queue
from threading import Thread

from playwright.sync_api import Error, Page, sync_playwright

HOST = "127.0.0.1"
ROOT = Path(__file__).resolve().parents[2]
LITE = ["bash", "ops/lite.sh"]


class Capture(socketserver.StreamRequestHandler):
    # ponytail: one local test client; no relay, authentication or storage.
    messages: Queue[tuple[str, str]] = Queue()

    def handle(self) -> None:
        self.request.settimeout(5)
        self.wfile.write(b"220 local capture\r\n")
        while line := self.rfile.readline(8192):
            command = line.split(maxsplit=1)[0].upper()
            if command == b"QUIT":
                self.wfile.write(b"221 closing\r\n")
                return
            if command == b"DATA":
                self.wfile.write(b"354 send message\r\n")
                data = bytearray()
                while (line := self.rfile.readline(8192)) != b".\r\n":
                    if not line or len(data) + len(line) > 8192:
                        return
                    data.extend(line)
                message = BytesParser(policy=policy.default).parsebytes(bytes(data))
                self.messages.put((str(message["subject"]), message.get_content()))
            self.wfile.write(b"250 ok\r\n")


def api(page: Page, path: str, status: int, **payload: str) -> None:
    actual = page.evaluate(
        """async ([path, body]) => (await fetch(path, body && {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        })).status""",
        [path, payload or None],
    )
    assert actual == status, f"{path}: expected {status}, received {actual}"


def mailed_url(origin: str, subject: str, path: str) -> str:
    actual, body = Capture.messages.get(timeout=5)
    assert actual == subject, "unexpected captured mail subject"
    url = body.strip().splitlines()[-1]
    assert url.startswith(f"{origin}/{path}?token="), "wrong web origin or path in captured mail"
    return url


def reset(origin: str, executable: str | None) -> tuple[bytes, bytes]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chromium", executable_path=executable, chromium_sandbox=True
        )
        signed_in = browser.new_page()
        signed_in.goto(f"{origin}/login")
        username = f"lite-reset-{secrets.token_hex(4)}"
        email = f"{username}@example.test"
        password = f"Smoke-A9!{secrets.token_hex(8)}"
        api(signed_in, "/api/auth/register", 201, username=username, email=email, password=password)
        verify_url = mailed_url(origin, "Verify your email address", "verify-email")
        api(signed_in, "/api/me", 200)
        api(signed_in, "/api/auth/password/reset/request", 200, email=email)
        url = mailed_url(origin, "Reset your password", "reset-password")

        page = browser.new_page()  # Separate, anonymous browser context.
        try:
            assert page.goto(verify_url).status == 200, "mailed verification page did not load"
            assert page.url == verify_url, "the mailed verification link was redirected"
            assert page.goto(url).status == 200, "mailed reset page did not load"
            assert page.url == url, "the mailed reset link was redirected"
            for expected in (200, 400):
                page.locator("#reset-password").fill(password + "-new")
                with page.expect_response(f"{origin}/api/auth/password/reset/confirm") as result:
                    page.locator('form button[type="submit"]').click()
                assert result.value.status == expected, "incorrect reset confirmation status"
                if expected == 200:
                    page.get_by_role("status").wait_for()
                    page.reload()  # Replay the exact URL and prove it is single-use.
        except Error:
            raise SystemExit("Chromium could not open the mailed links or submit reset") from None

        api(signed_in, "/api/me", 401)
        api(page, "/api/auth/login", 401, emailOrUsername=username, password=password)
        api(page, "/api/auth/login", 200, emailOrUsername=username, password=password + "-new")
    return verify_url.split("?token=", 1)[1].encode(), url.split("?token=", 1)[1].encode()


def check_diagnostics(unit: str, credentials: tuple[bytes, bytes]) -> None:
    journal = subprocess.check_output(
        ["journalctl", "--user", "-u", unit, "--no-pager", "-o", "cat"]
    )
    assert b"/readyz" in journal, "expected a readiness request in the Lite journal"
    journal_leak = any(credential in journal for credential in credentials)
    artifacts = [
        path
        for dist in (ROOT / "apps/web/.next", ROOT / "apps/web/.next-profiles")
        for path in dist.rglob("*")
        if path.is_file() and (path.name.startswith("trace") or ".log" in path.name)
    ]
    artifact_leaks = 0
    for path in artifacts:
        payload = path.read_bytes()
        artifact_leaks += any(credential in payload for credential in credentials)
    print(
        f"Post-stop diagnostics: journal_leak={journal_leak}, "
        f"next_files={len(artifacts)}, next_files_with_leak={artifact_leaks}"
    )
    assert not journal_leak and artifact_leaks == 0, "credentials persisted in diagnostics"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable", action="store_true", required=True)
    parser.add_argument("--chromium-executable", help="use an existing local Chromium binary")
    args = parser.parse_args()
    instance = f"perlimen-lite-{hashlib.sha256(os.fsencode(ROOT)).hexdigest()[:16]}"
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / instance
    unit = f"{instance}.service"
    load_state = subprocess.check_output(
        ["systemctl", "--user", "show", "-p", "LoadState", "--value", unit], text=True
    ).strip()
    if state.exists() or load_state != "not-found":
        parser.error("use a fresh disposable checkout with no previous Lite state")
    if any((ROOT / "apps/web" / dist).exists() for dist in (".next", ".next-profiles")):
        parser.error("use a fresh Next output directory")

    env = dict(os.environ)
    for service, default in [("WEB", "59500"), ("API", "59580"), ("PG", "59632")]:
        env.setdefault(f"PERLIMEN_LITE_{service}_PORT", default)
    origin = f"http://{HOST}:{env['PERLIMEN_LITE_WEB_PORT']}"
    with socketserver.TCPServer((HOST, 0), Capture) as smtp:
        env["PERLIMEN_LITE_SMTP_CAPTURE_PORT"] = str(smtp.server_address[1])
        thread = Thread(target=smtp.serve_forever)
        thread.start()
        try:
            subprocess.run([*LITE, "start"], cwd=ROOT, env=env, check=True)
            credentials = reset(origin, args.chromium_executable)
        finally:
            smtp.shutdown()
            thread.join()
            subprocess.run([*LITE, "stop"], cwd=ROOT, check=True)
    check_diagnostics(unit, credentials)
    print("Lite reset passed: exact SMTP link, real form, single use, revocation and new login.")


if __name__ == "__main__":
    main()
