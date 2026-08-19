"""WebSocket contract test: a real server thread, a real client socket."""

import json
import socket
import threading
import time

import pytest
from werkzeug.serving import make_server

from olala.api.server import AppContext, build_app
from olala.persistence.database import Database
from olala.security.keystore import EncryptedKeystore

from fakes import FakeMarketData, FakeProvider


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path, config_store):
    ctx = AppContext(
        config_store=config_store,
        database=Database(path=tmp_path / "ws.db"),
        keystore=EncryptedKeystore(path=tmp_path / "ks.enc"),
        provider=FakeProvider(),
        market_data=FakeMarketData())
    app = build_app(ctx)
    port = free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ctx, port
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_snapshot_then_live_events(live_server):
    from simple_websocket import Client

    ctx, port = live_server
    ws = Client.connect(f"ws://127.0.0.1:{port}/ws")
    try:
        snapshot = json.loads(ws.receive(timeout=5))
        assert snapshot["type"] == "snapshot"
        data = snapshot["data"]
        assert "dev_mode" in data
        assert len(data["wallets"]) == 3
        assert data["keystore"]["locked"] is True

        # An event published on the bus reaches the socket.
        ctx.bus.publish("discovery_scan", {"seed_symbol": "JUP",
                                           "new_candidates": 2})
        event = json.loads(ws.receive(timeout=5))
        assert event["type"] == "discovery_scan"
        assert event["data"]["new_candidates"] == 2
    finally:
        ws.close()


def test_two_clients_both_receive(live_server):
    from simple_websocket import Client

    ctx, port = live_server
    ws1 = Client.connect(f"ws://127.0.0.1:{port}/ws")
    ws2 = Client.connect(f"ws://127.0.0.1:{port}/ws")
    try:
        json.loads(ws1.receive(timeout=5))
        json.loads(ws2.receive(timeout=5))
        time.sleep(0.1)
        ctx.bus.publish("wallet_update", {"id": "w1"})
        for ws in (ws1, ws2):
            event = json.loads(ws.receive(timeout=5))
            assert event["type"] == "wallet_update"
    finally:
        ws1.close()
        ws2.close()
