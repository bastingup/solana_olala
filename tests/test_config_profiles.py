"""Master config + trading-profile split: selection, override order,
round-tripping, and the legacy single-file path."""

import pytest
import yaml

from olala.config import ConfigStore

MASTER = """\
dev_mode: false
hft: {hft}
risk:
  per_trade_fraction: 0.09
chain:
  helius_api_key: master-key
"""

HFT = """\
filters:
  min_history_days: 7
  max_trades_per_day: 600.0
discovery:
  skill_window_days: 7
  leaderboard_sort: realized
follow:
  poll_interval_sec: 90
"""

SLOW = """\
filters:
  min_history_days: 30
  max_trades_per_day: 40.0
discovery:
  skill_window_days: 30
  leaderboard_sort: win_percentage
follow:
  poll_interval_sec: 45
"""


def world(tmp_path, hft: bool):
    (tmp_path / "config.yaml").write_text(MASTER.format(hft=str(hft).lower()))
    (tmp_path / "config.hft.yaml").write_text(HFT)
    (tmp_path / "config.slow.yaml").write_text(SLOW)
    return ConfigStore(path=tmp_path / "config.yaml")


def test_hft_flag_selects_the_hft_profile(tmp_path):
    store = world(tmp_path, hft=True)
    config = store.config
    assert store.profile_name == "hft"
    assert config.filters.min_history_days == 7
    assert config.filters.max_trades_per_day == 600.0
    assert config.discovery.skill_window_days == 7
    assert config.discovery.leaderboard_sort == "realized"
    assert config.follow.poll_interval_sec == 90


def test_flag_off_selects_the_slow_profile(tmp_path):
    store = world(tmp_path, hft=False)
    config = store.config
    assert store.profile_name == "slow"
    assert config.filters.min_history_days == 30
    assert config.filters.max_trades_per_day == 40.0
    assert config.discovery.skill_window_days == 30
    assert config.discovery.leaderboard_sort == "win_percentage"
    assert config.follow.poll_interval_sec == 45


def test_master_owns_modes_and_risk_in_both_profiles(tmp_path):
    for hft in (True, False):
        config = world(tmp_path, hft=hft).config
        # Risk exposure and credentials must not vary with strategy.
        assert config.risk.per_trade_fraction == 0.09
        assert config.chain.helius_api_key == "master-key"
        assert config.dev_mode is False


def test_profile_overrides_legacy_master_sections(tmp_path):
    # A master carrying profile sections (the old single-file layout)
    # still loads, and the profile file wins where they collide.
    (tmp_path / "config.yaml").write_text(
        "hft: true\nfilters:\n  min_history_days: 99\n  min_trades: 77\n")
    (tmp_path / "config.hft.yaml").write_text(
        "filters:\n  min_history_days: 7\n")
    config = ConfigStore(path=tmp_path / "config.yaml").config
    assert config.filters.min_history_days == 7    # profile wins
    assert config.filters.min_trades == 77         # legacy value survives


def test_legacy_single_file_still_works(tmp_path):
    # No profile files at all: the master alone must still configure it.
    (tmp_path / "config.yaml").write_text(
        "dev_mode: true\nfilters:\n  min_trades: 42\n")
    config = ConfigStore(path=tmp_path / "config.yaml").config
    assert config.dev_mode is True
    assert config.filters.min_trades == 42


def test_missing_profile_file_falls_back_to_defaults(tmp_path):
    (tmp_path / "config.yaml").write_text("hft: true\n")
    store = ConfigStore(path=tmp_path / "config.yaml")
    assert store.profile_name == "hft"
    assert store.config.filters.min_trades == 200   # shipped default


def test_runtime_update_writes_each_section_to_its_own_file(tmp_path):
    store = world(tmp_path, hft=True)
    store.update({"filters": {"min_trades": 33},
                  "risk": {"per_trade_fraction": 0.02}})

    master = yaml.safe_load((tmp_path / "config.yaml").read_text())
    profile = yaml.safe_load((tmp_path / "config.hft.yaml").read_text())

    # Risk stayed in the master; filters went to the active profile.
    assert master["risk"]["per_trade_fraction"] == 0.02
    assert master["hft"] is True
    assert "filters" not in master
    assert profile["filters"]["min_trades"] == 33
    # The other profile is untouched by an hft-profile update.
    slow = yaml.safe_load((tmp_path / "config.slow.yaml").read_text())
    assert slow["filters"]["min_history_days"] == 30

    # And it all survives a reload.
    reloaded = ConfigStore(path=tmp_path / "config.yaml").config
    assert reloaded.filters.min_trades == 33
    assert reloaded.risk.per_trade_fraction == 0.02


def test_shipped_profiles_load_and_differ(tmp_path):
    """The real config.hft.yaml / config.slow.yaml must both be valid."""
    from pathlib import Path
    backend = Path(__file__).resolve().parent.parent / "backend"
    master = tmp_path / "config.yaml"
    for name, expect_fast in (("hft", True), ("slow", False)):
        master.write_text(f"hft: {str(expect_fast).lower()}\n")
        store = ConfigStore(path=master, profile_dir=backend)
        config = store.config
        assert store.profile_name == name
        if expect_fast:
            assert config.filters.max_trades_per_day > 100
            assert config.discovery.skill_window_days <= 7
        else:
            assert config.filters.max_trades_per_day <= 100
            assert config.filters.min_median_hold_minutes >= 30
