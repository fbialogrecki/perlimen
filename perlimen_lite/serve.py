"""Serve the Lite API on loopback with sanitized diagnostics."""

import logging
import sys

import uvicorn

from perlimen_lite.database import required
from perlimen_lite.diagnostics import RequestDiagnostics, configure_diagnostics


def main() -> None:
    configure_diagnostics()
    try:
        from perlimen_lite.main import app

        uvicorn.run(
            RequestDiagnostics(app, internal_token=required("THRESHOLD_INTERNAL_TOKEN")),
            host="127.0.0.1",
            port=int(sys.argv[1]),
            proxy_headers=False,
            access_log=False,
            log_config=None,
        )
    except Exception:
        logging.getLogger(__name__).error("Lite API startup failed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
