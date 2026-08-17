"""Solana-olala backend entrypoint."""

from __future__ import annotations

import logging
import signal
import sys

from olala.api.server import AppContext, build_app


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    ctx = AppContext()
    app = build_app(ctx)
    ctx.start()

    def shutdown(signum, frame):  # noqa: ANN001
        ctx.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    server = ctx.store.config.server
    logging.getLogger(__name__).info(
        "Solana-olala listening on http://%s:%d%s",
        server.host, server.port,
        " (dev mode)" if ctx.store.config.dev_mode else "")
    app.run(host=server.host, port=server.port, threaded=True,
            use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
