from olala.risk.atr import CANDLE_SECONDS, AtrTracker


def test_no_stop_until_warm():
    tracker = AtrTracker(period=14)
    tracker.add_sample("m1", 0.0, 1.0)
    assert tracker.atr("m1") is None
    assert tracker.stop_price("m1", peak_price=1.0, multiplier=3.5) == 0.0


def test_atr_computes_after_enough_candles():
    tracker = AtrTracker(period=14)
    price = 1.0
    for i in range(20):
        price += 0.01 if i % 2 == 0 else -0.005
        tracker.add_sample("m1", i * CANDLE_SECONDS, price)
    atr = tracker.atr("m1")
    assert atr is not None
    assert atr > 0
    stop = tracker.stop_price("m1", peak_price=price, multiplier=3.5)
    assert 0 < stop < price


def test_samples_bucket_into_candles():
    tracker = AtrTracker(period=2)
    # Two samples inside one minute -> one candle with high/low spread.
    tracker.add_sample("m1", 10.0, 1.0)
    tracker.add_sample("m1", 20.0, 2.0)
    candles = tracker._candles["m1"]
    assert len(candles) == 1
    assert candles[0].high == 2.0 and candles[0].low == 1.0


def test_zero_or_negative_prices_ignored():
    tracker = AtrTracker(period=2)
    tracker.add_sample("m1", 0.0, 0.0)
    tracker.add_sample("m1", 0.0, -1.0)
    assert "m1" not in tracker._candles


def test_forget_clears_state():
    tracker = AtrTracker(period=2)
    for i in range(5):
        tracker.add_sample("m1", i * CANDLE_SECONDS, 1.0 + i * 0.1)
    tracker.forget("m1")
    assert tracker.atr("m1") is None
