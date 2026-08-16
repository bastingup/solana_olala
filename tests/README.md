# Test suite

Full dev suite for Solana-olala: backend unit tests, API/WebSocket
integration tests, and frontend asset/contract checks. Everything runs
offline — chain access is faked, no network calls leave the machine.

```bash
tests/run.sh      # Linux
tests\run.bat     # Windows
```

Both install `pytest` into the project venv (`backend/.venv`) and run the
whole suite. Individual runs:

```bash
backend/.venv/bin/python -m pytest tests/ -v
backend/.venv/bin/python -m pytest tests/test_risk_engine.py -v
```

| File | Covers |
|---|---|
| `fakes.py` | Offline stand-ins: fake RPC provider, fake market data, token factory |
| `test_config.py` | Config defaults, YAML persistence, update validation |
| `test_events.py` | EventBus delivery, slow-client overflow behavior |
| `test_models.py` | TraderStats/Position derived math |
| `test_reconstruction.py` | Swap detection from raw transaction diffs |
| `test_scoring.py` | FIFO round trips, win rate, score formula |
| `test_filters.py` | Every trader-admission rejection path |
| `test_risk_engine.py` | 1% liquidity cap, reserve rules, position ceilings |
| `test_atr.py` | Candle bucketing, ATR warm-up, trailing stop |
| `test_token_safety.py` | Honeypot screen: authorities, concentration, age |
| `test_keystore.py` | Encryption round trip, wrong passphrase, key formats |
| `test_executor_paper.py` | Paper fill pricing, slippage direction, fees |
| `test_portfolio.py` | Balances, open/resize/close, exposure, persistence |
| `test_database.py` | SQLite round trips for every table |
| `test_trading_engine.py` | Signal → risk gate → execution, exits, rejections |
| `test_follower.py` | Cursor arming, new-trade detection, replay order |
| `test_api.py` | REST endpoints incl. live-mode guards |
| `test_stream.py` | WebSocket snapshot contract over a live server |
| `test_frontend_assets.py` | JS syntax, asset references, no-CDN policy |
