"""Black-box product contracts against an explicitly disposable local web origin."""

import argparse
import json
import secrets
import ssl
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from io import BytesIO
from threading import Barrier
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import httpx
from PIL import Image

FEED = "/api/social/feed?filter=posts"
UPLOAD = "/api/media/assets"
SESSION = "threshold_session"
REFRESH = "threshold_refresh"
CITY = "Warsaw"


def local_origin(value: str) -> str:
    error = argparse.ArgumentTypeError("supply a numeric loopback origin with an explicit port")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise error from None
    if (
        url.scheme not in {"http", "https"}
        or not port
        or url.netloc not in {f"127.0.0.1:{port}", f"[::1]:{port}"}
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        raise error
    return f"{url.scheme}://{url.netloc}"


class Document(HTMLParser):
    """Keep raw HTML separate from rendered text and metadata for privacy checks."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.html = html
        self.meta: dict[str, str] = {}
        self.headings: list[str] = []
        self.text: list[str] = []
        self._heading: list[str] | None = None
        self._ignored = 0
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "template"}:
            self._ignored += 1
        if self._ignored:
            return
        if tag == "h1":
            self._heading = []
        if tag == "meta":
            values = dict(attrs)
            prop, content = values.get("property"), values.get("content")
            if prop and content:
                self.meta[prop] = content

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "template"} and self._ignored:
            self._ignored -= 1
        if not self._ignored and tag == "h1" and self._heading is not None:
            self.headings.append("".join(self._heading).strip())
            self._heading = None

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self.text.append(data)
            if self._heading is not None:
                self._heading.append(data)

    @property
    def rendered(self) -> str:
        return " ".join(self.text)


class ProductContract(unittest.TestCase):
    origin: str
    tls: ssl.SSLContext
    max_erasure_wait: float
    run_id: str
    password: str
    clients: ExitStack
    accounts: dict[str, httpx.Client]

    @classmethod
    def client(cls) -> httpx.Client:
        return cls.clients.enter_context(
            httpx.Client(
                base_url=cls.origin,
                verify=cls.tls,
                trust_env=False,
                follow_redirects=False,
                timeout=15,
                headers={"Origin": cls.origin, "Accept-Language": "en"},
            )
        )

    @classmethod
    def setUpClass(cls) -> None:
        cls.clients = ExitStack()
        cls.addClassCleanup(cls.clients.close)
        cls.run_id = secrets.token_hex(5)
        cls.password = f"Fixture-A8!{secrets.token_hex(8)}"
        cls.accounts = {}

    def call(
        self,
        client: httpx.Client,
        method: str,
        path: str,
        status: int | tuple[int, ...],
        **kwargs: Any,
    ) -> httpx.Response:
        response = client.request(method, path, **kwargs)
        expected = (status,) if isinstance(status, int) else status
        self.assertIn(response.status_code, expected, f"{method} {path}")
        return response

    def username(self, role: str) -> str:
        return f"lite-{self.run_id}-{role}"

    def account(self, role: str) -> httpx.Client:
        if role in self.accounts:
            return self.accounts[role]
        client = self.client()
        name = self.username(role)
        self.call(
            client,
            "POST",
            "/api/auth/register",
            201,
            json={
                "username": name,
                "email": f"{name}@example.com",
                "password": self.password,
            },
        )
        self.accounts[role] = client
        return client

    def page(self, owner: httpx.Client, role: str) -> dict[str, Any]:
        response = self.call(
            owner,
            "POST",
            "/api/pages",
            201,
            json={
                "slug": self.username(role),
                "display_name": f"Lite Room {self.run_id}",
                "page_type": "club",
                "city": CITY,
            },
        )
        return cast(dict[str, Any], response.json())

    def event(self, owner: httpx.Client, page_id: str, suffix: str) -> dict[str, Any]:
        response = self.call(
            owner,
            "POST",
            "/api/events",
            201,
            json={
                "page_id": page_id,
                "slug": self.username(suffix),
                "title": f"Lite Night {suffix}",
                "starts_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
                "city": CITY,
                "location_mode": "tba",
                "description": "Public synthetic event fixture",
            },
        )
        return cast(dict[str, Any], response.json())

    def document(self, client: httpx.Client, path: str, *, absent: str = "") -> Document:
        response = self.call(client, "GET", path, 200, headers={"User-Agent": "Googlebot"})
        self.assertTrue(response.headers.get("content-type", "").startswith("text/html"))
        if absent:
            self.assertFalse(absent in response.text, "SSR exposed a private fixture value")
        return Document(response.text)

    def post(self, author: httpx.Client, body: str, **fields: Any) -> dict[str, Any]:
        response = self.call(
            author,
            "POST",
            "/api/social/posts",
            201,
            json={
                "body": body,
                **fields,
            },
        )
        return cast(dict[str, Any], response.json())

    def upload(
        self, client: httpx.Client, context: str, *, status: int = 201, mime: str = "image/png"
    ) -> httpx.Response:
        png = BytesIO()
        Image.new("RGB", (32, 24), (13, 31, 51)).save(png, "PNG")
        return self.call(
            client,
            "POST",
            UPLOAD,
            status,
            data={"context": context},
            files={"file": ("fixture.png", png.getvalue(), mime)},
        )

    def avatar(self, client: httpx.Client) -> str:
        asset = self.upload(client, "user_avatar").json()["id"]
        self.call(client, "PATCH", "/api/me/profile", 200, json={"avatar_media_asset_id": asset})
        return f"{UPLOAD}/assets/{asset}/avatar_256.webp"

    def test_public_ssr_and_open_graph(self) -> None:
        owner = self.account("organizer")
        page = self.page(owner, "room")
        event = self.event(owner, page["id"], "public")
        anon = self.client()
        routes = (
            (f"/u/{self.username('organizer')}", self.username("organizer")),
            (f"/pages/{page['slug']}", page["display_name"]),
            (f"/events/{event['slug']}", event["title"]),
        )
        for path, title in routes:
            with self.subTest(path=path):
                doc = self.document(anon, path, absent="@example.com")
                self.assertIn(title, doc.headings)
                self.assertEqual(doc.meta.get("og:title"), title)
                self.assertEqual(
                    doc.meta.get("og:type"), "website" if path.startswith("/events/") else "profile"
                )
                self.assertTrue(doc.meta.get("og:description"))

    def test_protected_feed_and_committed_block(self) -> None:
        viewer = self.account("viewer")
        author = self.account("author")
        unblocked = self.account("organizer")
        anon = self.client()
        viewer_id = self.call(viewer, "GET", "/api/me", 200).json()["user"]["id"]
        self.call(anon, "GET", FEED, 401)
        self.call(anon, "GET", FEED, 401, headers={"X-Threshold-User-Id": viewer_id})

        onboarding = self.call(viewer, "POST", "/api/onboarding", 200, json={"city": CITY})
        group = onboarding.json()["city_group"]["slug"]
        membership = f"/api/social/groups/{group}/membership"
        group_text = f"Group fixture {self.run_id}"
        followed_text = f"Followed fixture {self.run_id}"
        control_text = f"Unblocked group fixture {self.run_id}"
        group_post = self.post(viewer, group_text, group_slug=group)
        followed_post = self.post(author, followed_text)
        self.call(unblocked, "POST", membership, 200)
        control_id = self.post(unblocked, control_text, group_slug=group)["id"]
        landing = self.call(anon, "GET", "/app", (200, 302, 303, 307, 308))
        if landing.status_code == 200:
            self.assertIn("Redirecting", Document(landing.text).rendered)
        else:
            redirect = urlsplit(landing.headers["location"])
            self.assertEqual(redirect.path, "/login")
            self.assertEqual(parse_qs(redirect.query).get("callbackUrl"), ["/app"])
        # Include serialized server-component data in the leak check.
        for marker in (group_text, followed_text, control_text):
            self.assertNotIn(marker, landing.text)

        def ids() -> list[str]:
            return [p["id"] for p in self.call(viewer, "GET", FEED, 200).json()["items"]]

        self.assertIn(group_post["id"], ids())
        self.assertNotIn(followed_post["id"], ids())
        self.call(
            viewer,
            "POST",
            "/api/follow",
            200,
            json={
                "handle": self.username("author"),
                "targetType": "consumer",
            },
        )
        before = ids()
        html = self.document(viewer, "/app").rendered
        self.assertIn(group_text, html)
        with self.subTest(contract="followed author outside the group"):
            self.assertIn(followed_post["id"], before)
            self.assertLess(before.index(followed_post["id"]), before.index(group_post["id"]))
            self.assertIn(followed_text, html)

        # A group post makes the block check independent of follow transport.
        self.call(author, "POST", membership, 200)
        blocked_text = f"Blocked group fixture {self.run_id}"
        blocked_post = self.post(author, blocked_text, group_slug=group)
        before = ids()
        before_app = self.document(viewer, "/app")
        self.assertIn(control_id, before)
        self.assertIn(blocked_post["id"], before)
        self.assertIn(control_text, before_app.rendered)
        self.assertIn(blocked_text, before_app.rendered)

        self.call(viewer, "POST", "/api/blocks", 201, json={"username": self.username("author")})
        after = ids()  # The first read must use the committed policy, without waiting or retries.
        self.assertIn(group_post["id"], after)
        self.assertIn(control_id, after)
        with self.subTest(contract="first social Feed read after block"):
            self.assertNotIn(blocked_post["id"], after)
            self.assertNotIn(followed_post["id"], after)
        after_app = self.document(viewer, "/app")
        self.assertIn(group_text, after_app.rendered)
        self.assertIn(control_text, after_app.rendered)
        for marker in (blocked_text, blocked_post["id"], followed_text, followed_post["id"]):
            with self.subTest(contract="first SSR Feed response after block", marker=marker):
                self.assertFalse(marker in after_app.html, "SSR exposed a blocked post")
        self.call(author, "GET", f"/posts/{followed_post['id']}", 200)

    def assert_session_cookies(self, response: httpx.Response) -> None:
        cookies = SimpleCookie()
        for header in response.headers.get_list("set-cookie"):
            cookies.load(header)
        for name, path in ((SESSION, "/"), (REFRESH, "/api/auth")):
            self.assertIn(name, cookies)
            self.assertTrue(cookies[name]["httponly"], f"{name} must be HttpOnly")
            self.assertEqual(bool(cookies[name]["secure"]), self.origin.startswith("https:"))
            self.assertEqual(cookies[name]["samesite"].lower(), "lax")
            self.assertEqual(cookies[name]["path"], path)

    def test_session_rotation_origin_and_revocation(self) -> None:
        owner = self.account("viewer")
        owner_id = self.call(owner, "GET", "/api/me", 200).json()["user"]["id"]
        payload = {"emailOrUsername": self.username("viewer"), "password": self.password}
        client = self.client()
        asserted_identity = {
            "X-Threshold-User-Id": owner_id,
            "X-Threshold-Username": self.username("viewer"),
            "X-Authentik-Username": self.username("viewer"),
        }
        self.call(client, "GET", "/api/me", 401, headers=asserted_identity)
        self.call(client, "GET", FEED, 401, headers=asserted_identity)
        self.call(
            client,
            "POST",
            "/api/pages",
            401,
            headers=asserted_identity,
            json={
                "slug": "unauthenticated",
                "display_name": "Unauthenticated",
                "page_type": "club",
            },
        )
        self.call(
            client,
            "POST",
            "/api/auth/login",
            403,
            json=payload,
            headers={"Origin": "https://unrelated.example"},
        )
        self.call(client, "GET", "/api/me", 401)
        self.assert_session_cookies(self.call(client, "POST", "/api/auth/login", 200, json=payload))
        self.assertEqual(
            self.call(client, "GET", "/api/me", 200).json()["user"]["username"],
            self.username("viewer"),
        )
        self.assertNotIn(REFRESH, client.build_request("GET", "/api/me").headers.get("cookie", ""))
        self.assertNotIn(
            REFRESH, client.build_request("POST", "/api/authentication").headers.get("cookie", "")
        )
        prior = dict(client.cookies)
        self.assert_session_cookies(self.call(client, "POST", "/api/auth/refresh", 200))
        self.assertTrue(
            all(prior[name] != client.cookies[name] for name in (SESSION, REFRESH)),
            "refresh did not rotate both cookies",
        )
        replay = self.client()
        replay.cookies.set(SESSION, prior[SESSION], path="/")
        self.call(replay, "GET", "/api/me", 401)
        replay.cookies.set(REFRESH, prior[REFRESH], path="/api/auth")
        self.call(replay, "POST", "/api/auth/refresh", 401)
        self.call(client, "GET", "/api/me", 200)
        logout_cookies = dict(client.cookies)
        self.call(
            client, "POST", "/api/auth/logout", 403, headers={"Origin": "https://unrelated.example"}
        )
        self.call(client, "GET", "/api/me", 200)
        self.call(client, "POST", "/api/auth/logout", 204)
        self.assertFalse(client.cookies)
        self.call(client, "GET", "/api/me", 401)
        replay.cookies.set(SESSION, logout_cookies[SESSION], path="/")
        self.call(replay, "GET", "/api/me", 401)
        replay.cookies.set(REFRESH, logout_cookies[REFRESH], path="/api/auth")
        self.call(replay, "POST", "/api/auth/refresh", 401)

    def test_page_roles_and_qr(self) -> None:
        owner = self.account("organizer")
        guest = self.account("viewer")
        door = self.account("door")
        page = self.page(owner, "venue")
        event = self.event(owner, page["id"], "door-one")
        other = self.event(owner, page["id"], "door-two")
        event_api, other_api = (f"/api/events/{item['slug']}" for item in (event, other))
        guest_role = f"/api/pages/{page['slug']}/members/{self.username('viewer')}"
        door_role = f"/api/pages/{page['slug']}/members/{self.username('door')}"
        self.call(guest, "PATCH", event_api, 403, json={"title": "Denied"})
        self.call(owner, "PUT", guest_role, 200, json={"role": "editor"})
        self.call(guest, "PATCH", event_api, 200, json={"title": "Edited by fixture editor"})
        self.call(guest, "PUT", door_role, 403, json={"role": "admin"})
        self.call(owner, "DELETE", guest_role, 204)
        self.call(guest, "PATCH", event_api, 403, json={"title": "Revoked"})
        self.call(self.client(), "GET", f"{event_api}/guestlist", 401)
        self.call(
            owner, "POST", f"{event_api}/guestlist", 201, json={"username": self.username("viewer")}
        )
        self.document(self.client(), f"/events/{event['slug']}", absent=self.username("viewer"))
        assignment = f"{event_api}/door-staff/by-username/{self.username('door')}"
        self.call(owner, "PUT", assignment, (200, 201))
        self.call(door, "GET", f"{event_api}/guestlist", 403)

        qr_path = f"{event_api}/guestlist/me/qr-token"
        old = self.call(guest, "POST", qr_path, 201).json()["token"]
        issued_at = datetime.now(UTC)
        issued = self.call(guest, "POST", qr_path, 201).json()
        token = issued["token"]
        self.assertTrue(old != token and len(token) >= 32, "expected separate opaque QR values")
        expiry = datetime.fromisoformat(issued["expires_at"])
        self.assertGreater(expiry, issued_at)
        self.assertLessEqual(expiry, datetime.now(UTC) + timedelta(seconds=300))
        self.call(owner, "POST", f"{other_api}/check-in", 404, json={"token": token})
        self.call(guest, "POST", f"{event_api}/check-in", 403, json={"token": token})
        self.call(door, "POST", f"{event_api}/check-in", 409, json={"token": old})
        door2 = self.client()
        door2.cookies.update(door.cookies)
        barrier = Barrier(2, timeout=5)

        def redeem(client: httpx.Client) -> httpx.Response:
            barrier.wait()
            return self.call(
                client, "POST", f"{event_api}/check-in", (200, 409), json={"token": token}
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(redeem, (door, door2)))
        self.assertEqual(sorted(result.status_code for result in responses), [200, 409])
        body = next(result.json() for result in responses if result.status_code == 200)
        self.assertEqual(body["status"], "checked_in")
        self.assertEqual(body.get("username", body["display_name"]), self.username("viewer"))
        self.assertFalse(set(body) - {"status", "display_name", "username"})
        self.call(door, "POST", f"{event_api}/check-in", 409, json={"token": token})

    def test_upload_visibility_and_attachment_ownership(self) -> None:
        owner = self.account("author")
        other = self.account("viewer")
        anon = self.client()
        self.upload(anon, "post_image", status=401)
        self.upload(owner, "post_image", status=422, mime="image/jpeg")
        asset = self.upload(owner, "post_image").json()["id"]
        private = f"{UPLOAD}/assets/{asset}"
        denied = (401, 403, 404)
        for variant in ("post_480.webp", "original"):
            with self.subTest(unpublished=variant):
                self.call(anon, "GET", f"{private}/{variant}", denied)
        self.call(
            other,
            "POST",
            "/api/social/posts",
            (403, 422),
            json={
                "body": "Foreign asset fixture",
                "media_asset_ids": [asset],
            },
        )
        post = self.post(owner, "Owned asset fixture", media_asset_ids=[asset])
        self.assertEqual(post["media_asset_ids"], [asset])
        with self.subTest(contract="attachment does not publish a session-gated image"):
            self.call(anon, "GET", f"{private}/post_480.webp", denied)
        avatar = self.avatar(owner)
        published = avatar.rsplit("/", 1)[0]
        for variant in ("original", "post_480.webp"):
            with self.subTest(published=variant):
                self.call(anon, "GET", f"{published}/{variant}", denied)
        doc = self.document(anon, f"/u/{self.username('author')}")
        og_image = doc.meta.get("og:image", "")
        self.assertEqual(og_image, f"{self.origin}{published}/avatar_512.webp")
        for path, size in ((avatar, 256), (urlsplit(og_image).path, 512)):
            with self.subTest(public_image=path):
                result = self.call(anon, "GET", path, 200)
                self.assertEqual(result.headers["content-type"], "image/webp")
                with Image.open(BytesIO(result.content)) as image:
                    self.assertEqual((image.format, image.size), ("WEBP", (size, size)))
                    image.load()

    def test_account_erasure_revokes_then_removes(self) -> None:
        account = self.account("erase")
        observer = self.account("organizer")
        anon = self.client()
        name = self.username("erase")
        profile = f"/u/{name}"
        body = f"Retained thread fixture {self.run_id}"
        onboarding = self.call(account, "POST", "/api/onboarding", 200, json={"city": CITY})
        group = onboarding.json()["city_group"]["slug"]
        post_id = self.post(account, body, group_slug=group)["id"]
        post_path = f"/posts/{post_id}"
        reply = f"Retained reply fixture {self.run_id}"
        self.call(
            observer, "POST", f"/api/social/posts/{post_id}/comments", 201, json={"body": reply}
        )
        avatar = self.avatar(account)
        self.call(anon, "GET", avatar, 200)
        self.assertIn(name, self.document(anon, profile).headings)

        def retained_post() -> dict[str, Any]:
            response = self.call(observer, "GET", f"/api/social/groups/{group}/posts", 200)
            matches = [item for item in response.json()["items"] if item["id"] == post_id]
            self.assertEqual(len(matches), 1, "erasure must preserve the group post")
            return cast(dict[str, Any], matches[0])

        user_id = self.call(account, "GET", "/api/me", 200).json()["user"]["id"]
        original_post = retained_post()
        self.assertEqual(original_post["author_username"], name)
        self.assertEqual(original_post["author_user_id"], user_id)
        original_thread = self.document(observer, post_path)
        self.assertIn(body, original_thread.rendered)
        self.assertIn(reply, original_thread.rendered)
        self.assertTrue(profile in original_thread.html, "fixture author profile link is missing")
        replay = self.client()
        replay.cookies.update(account.cookies)
        self.call(account, "DELETE", "/api/me", 202)
        self.assertFalse(account.cookies)
        self.call(replay, "GET", "/api/me", 401)
        self.call(replay, "POST", "/api/auth/refresh", 401)

        deadline = time.monotonic() + self.max_erasure_wait
        while True:
            profile_result = self.call(
                anon, "GET", profile, (200, 404), headers={"User-Agent": "Googlebot"}
            )
            media_result = self.call(anon, "GET", avatar, (200, 401, 403, 404))
            thread = self.document(observer, post_path)
            self.assertIn(body, thread.rendered, "erasure must preserve the thread")
            self.assertIn(reply, thread.rendered, "erasure must preserve the other author's reply")
            anonymized = "Deleted User" in thread.rendered or "deleted-user" in thread.rendered
            if profile_result.status_code == 404 and media_result.status_code != 200 and anonymized:
                self.assertFalse(name in thread.html, "permalink retained the erased username")
                self.assertFalse(user_id in thread.html, "permalink retained the erased user ID")
                payload = json.dumps(retained_post())
                self.assertFalse(name in payload, "post payload retained the erased username")
                self.assertFalse(user_id in payload, "post payload retained the erased user ID")
                break
            self.assertLess(
                time.monotonic(),
                deadline,
                f"erasure did not converge: profile={profile_result.status_code}, "
                f"avatar={media_result.status_code}, thread anonymized={anonymized}",
            )
            time.sleep(0.5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True, type=local_origin)
    parser.add_argument("--ca-file", help="local CA PEM for an isolated HTTPS instance")
    parser.add_argument(
        "--erasure-wait", type=int, default=30, choices=range(1, 121), metavar="SECONDS"
    )
    parser.add_argument(
        "--disposable",
        action="store_true",
        required=True,
        help="confirm the web, DB, mail and media are disposable fixtures",
    )
    parser.add_argument("cases", nargs="*", help="unittest test names (default: all)")
    args = parser.parse_args()
    ProductContract.origin = args.origin
    ProductContract.tls = ssl.create_default_context(cafile=args.ca_file)
    ProductContract.max_erasure_wait = args.erasure_wait
    unittest.main(argv=["product_contract", "-v", *args.cases])


if __name__ == "__main__":
    main()
