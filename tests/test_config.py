from olala.config import AppConfig, ConfigStore


def test_defaults_are_strict_preset(tmp_path):
    config = ConfigStore(path=tmp_path / "empty.yaml").config
    assert config.dev_mode is False
    assert config.filters_onchain.min_history_days == 90
    assert config.filters_onchain.min_trades == 200
    assert config.filters_onchain.min_win_rate == 0.60
    assert config.risk.max_liquidity_fraction == 0.01
    assert config.paper.wallet_count == 3


def test_update_persists_across_reload(tmp_path):
    path = tmp_path / "config.yaml"
    store = ConfigStore(path=path)
    store.update({"filters_onchain": {"min_win_rate": 0.65}})
    reloaded = ConfigStore(path=path)
    assert reloaded.config.filters_onchain.min_win_rate == 0.65


def test_update_rejects_unknown_section(config_store):
    for bad in ({"server": {"port": 1}}, {"nonsense": {}},
                {"filters_onchain": {"unknown_option": 1}}):
        try:
            config_store.update(bad)
        except ValueError:
            continue
        raise AssertionError(f"update accepted invalid patch: {bad}")


def test_update_rejects_legacy_mode_key(config_store):
    # The universe mode is gone; a legacy patch must fail loudly instead
    # of silently reintroducing it.
    try:
        config_store.update({"mode": "live"})
    except ValueError:
        return
    raise AssertionError("legacy mode key accepted")


def test_load_ignores_legacy_mode_key(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("mode: live\ndev_mode: false\n")
    store = ConfigStore(path=path)
    assert not hasattr(store.config, "mode")


def test_update_coerces_value_types(config_store):
    updated = config_store.update({"filters_onchain": {"min_trades": "150"}})
    assert updated.filters_onchain.min_trades == 150
    assert isinstance(updated.filters_onchain.min_trades, int)


def test_config_snapshot_is_isolated(config_store):
    snapshot = config_store.config
    snapshot.filters_onchain.min_win_rate = 0.0
    assert config_store.config.filters_onchain.min_win_rate == 0.60


def test_mutable_sections_whitelist():
    assert set(AppConfig._MUTABLE_SECTIONS) == {
        "filters_onchain", "filters_solanatracker", "risk",
        "discovery", "follow"}
