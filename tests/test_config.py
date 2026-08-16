from olala.config import AppConfig, ConfigStore


def test_defaults_are_strict_preset(config_store):
    config = config_store.config
    assert config.mode == "paper"
    assert config.filters.min_history_days == 90
    assert config.filters.min_trades == 200
    assert config.filters.min_win_rate == 0.60
    assert config.risk.max_liquidity_fraction == 0.01
    assert config.paper.wallet_count == 3


def test_update_persists_across_reload(tmp_path):
    path = tmp_path / "config.yaml"
    store = ConfigStore(path=path)
    store.update({"filters": {"min_win_rate": 0.65}, "mode": "paper"})
    reloaded = ConfigStore(path=path)
    assert reloaded.config.filters.min_win_rate == 0.65


def test_update_rejects_unknown_section(config_store):
    for bad in ({"server": {"port": 1}}, {"nonsense": {}},
                {"filters": {"unknown_option": 1}}):
        try:
            config_store.update(bad)
        except ValueError:
            continue
        raise AssertionError(f"update accepted invalid patch: {bad}")


def test_update_rejects_invalid_mode(config_store):
    try:
        config_store.update({"mode": "yolo"})
    except ValueError:
        return
    raise AssertionError("invalid mode accepted")


def test_update_coerces_value_types(config_store):
    updated = config_store.update({"filters": {"min_trades": "150"}})
    assert updated.filters.min_trades == 150
    assert isinstance(updated.filters.min_trades, int)


def test_config_snapshot_is_isolated(config_store):
    snapshot = config_store.config
    snapshot.filters.min_win_rate = 0.0
    assert config_store.config.filters.min_win_rate == 0.60


def test_mutable_sections_whitelist():
    assert set(AppConfig._MUTABLE_SECTIONS) == {
        "filters", "risk", "discovery", "follow"}
