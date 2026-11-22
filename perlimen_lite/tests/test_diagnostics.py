import json
import logging
from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from perlimen_lite.diagnostics import DiagnosticFormatter, RequestDiagnostics, logger
from pytest import LogCaptureFixture


def test_request_diagnostics_keep_only_safe_fields(caplog: LogCaptureFixture) -> None:
    app = FastAPI()
    internal_token = "fixture-internal-token"
    private_id = uuid4().hex
    trusted_id = uuid4().hex
    private_inputs = ["fixture-qr", "fixture-cookie", "fixture-query", "fixture-body", private_id]

    @app.post("/events/{slug}/check-in")
    async def check_in(slug: str, request: Request) -> None:
        raise ValueError(f"{slug}: {await request.body()!r}")

    client = TestClient(
        RequestDiagnostics(app, internal_token=internal_token), raise_server_exceptions=False
    )
    with caplog.at_level(logging.INFO, logger=logger.name):
        failure = client.post(
            "/events/fixture-qr/check-in?token=fixture-query",
            headers={"x-request-id": private_id, "cookie": "session=fixture-cookie"},
            json={"code": "fixture-body"},
        )
        missing = client.get(
            "/fixture-qr?token=fixture-query",
            headers={"x-request-id": trusted_id, "x-threshold-internal-token": internal_token},
        )

    assert failure.status_code == 500
    assert failure.headers["x-request-id"] != private_id
    assert missing.status_code == 404
    assert missing.headers["x-request-id"] == trusted_id
    rendered = [
        DiagnosticFormatter().format(record)
        for record in caplog.records
        if record.name == logger.name
    ]
    first, second = map(json.loads, rendered)
    assert (first["route"], first["status"], first["request_id"]) == (
        "/events/{slug}/check-in",
        500,
        failure.headers["x-request-id"],
    )
    assert (second["route"], second["status"], second["request_id"]) == (
        "__unmatched__",
        404,
        trusted_id,
    )
    for entry in (first, second):
        assert entry["complete"]
        assert entry["started_at_unix_us"] <= entry["finished_at_unix_us"]
        assert entry["duration_ms"] >= 0
    assert all(value not in "\n".join(rendered) for value in (*private_inputs, internal_token))


def test_stream_logs_once_after_the_last_chunk(caplog: LogCaptureFixture) -> None:
    app = FastAPI()
    router = APIRouter()

    def request_logs() -> list[logging.LogRecord]:
        return [record for record in caplog.records if record.name == logger.name]

    @router.get("/assets/{asset_id}")
    async def asset(asset_id: str) -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            yield b"first"
            assert not request_logs()
            yield b"last"

        return StreamingResponse(chunks())

    app.include_router(router, prefix="/media")
    with caplog.at_level(logging.INFO, logger=logger.name):
        response = TestClient(RequestDiagnostics(app, internal_token="")).get(
            "/media/assets/fixture-private-key"
        )

    assert response.content == b"firstlast"
    (record,) = request_logs()
    event = json.loads(DiagnosticFormatter().format(record))
    assert (event["route"], event["status"], event["complete"]) == (
        "/media/assets/{asset_id}",
        200,
        True,
    )


def test_exception_message_and_traceback_do_not_reach_runtime_log(
    caplog: LogCaptureFixture,
) -> None:
    component = logging.getLogger("uvicorn.error")
    try:
        raise ValueError("fixture-private-payload")
    except ValueError:
        component.exception("unexpected request for %s", "fixture-private-path")
    (record,) = caplog.records
    rendered = DiagnosticFormatter().format(record)
    assert "fixture-private" not in rendered
    assert json.loads(rendered)["severity"] == "error"
