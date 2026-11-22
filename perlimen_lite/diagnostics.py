"""Local Lite request diagnostics; never serialize request or exception data."""

import json
import logging
import re
from hmac import compare_digest
from time import perf_counter_ns, time_ns
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("perlimen_lite.http")
_REQUEST_ID = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}")
_METHODS = {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}
_COMPONENTS = {"alembic", "events", "media", "perlimen_lite", "social", "users", "uvicorn"}


class DiagnosticFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if record.name == logger.name and (event := getattr(record, "request_event", None)):
            return json.dumps(event)
        component = record.name.partition(".")[0]
        # Arguments, formatted messages and traceback text can all contain secrets.
        return json.dumps(
            {
                "event": "runtime_warning",
                "service": "api",
                "component": component if component in _COMPONENTS else "runtime",
                "severity": "error" if record.levelno >= logging.ERROR else "warning",
                "observed_at_unix_us": time_ns() // 1_000,
            }
        )


def configure_diagnostics() -> None:
    """Configure Lite process logs before Uvicorn starts, with log_config=None and no access log."""
    handler = logging.StreamHandler()
    handler.setFormatter(DiagnosticFormatter())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    logger.setLevel(logging.INFO)


class RequestDiagnostics:
    def __init__(self, app: ASGIApp, *, internal_token: str) -> None:
        self.app = app
        self.internal_token = internal_token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started_at = time_ns() // 1_000
        started = perf_counter_ns()
        headers = Headers(scope=scope)
        supplied = headers.get("x-request-id", "")
        request_id = (
            supplied
            if _REQUEST_ID.fullmatch(supplied)
            and self.internal_token
            and compare_digest(
                headers.get("x-threshold-internal-token", "").encode("latin-1"), self.internal_token
            )
            else uuid4().hex
        )
        method = scope["method"]
        status: int | None = None
        complete = False

        def record() -> None:
            # FastAPI's included routers keep the full template in their effective context.
            context = scope.get("fastapi", {}).get("effective_route_context")
            route = getattr(context or scope.get("route"), "path", None)
            event = {
                "event": "http_request",
                "service": "api",
                "request_id": request_id,
                "method": method if method in _METHODS else "OTHER",
                "route": route if isinstance(route, str) else "__unmatched__",
                "status": status,
                "complete": complete,
                "started_at_unix_us": started_at,
                "finished_at_unix_us": time_ns() // 1_000,
                "duration_ms": round((perf_counter_ns() - started) / 1_000_000, 3),
            }
            logger.info("request", extra={"request_event": event})

        async def observe(message: Message) -> None:
            nonlocal status, complete
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["x-request-id"] = request_id
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True
                record()

        try:
            await self.app(scope, receive, observe)
        finally:
            if not complete:
                record()
