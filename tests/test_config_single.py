"""The single-file config: layering, runtime overrides, and validation.

Two properties matter most here. First, the hand-written file is never
rewritten — one `PUT /api/config` used to `yaml.safe_dump` the whole
thing and erase every comment in it. Second, a routing policy that names
a source which does not exist must fail at STARTUP, not at the moment
the system needs to fall through.
"""

import pytest
import yaml

from olala.config import AppConfig, ConfigStore


HAND_WRITTEN = """\
# A comment the operator wrote and expects to keep.
dev_mode: true

risk:
  per_trade_fraction: 0.11   # trailing comment

tracking:
  min_interval_sec: 7.0
"""


def store_at(tmp_path, text=HAND_WRITTEN, name="config.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return ConfigStore(path=path), path


def test_single_file_is_layered_over_defaults(tmp_path):
    store, _ = store_at(tmp_path)
    config = store.config
    assert config.dev_mode is True
    assert config.risk.per_trade_fraction == 0.11
    assert config.tracking.min_interval_sec == 7.0
    # Untouched values still come from the strict built-in defaults.
    assert config.filters_onchain.min_win_rate == 0.60


def test_runtime_update_never_rewrites_the_hand_written_file(tmp_path):
    store, path = store_at(tmp_path)
    before = path.read_text()

    store.update({"risk": {"per_trade_fraction": 0.02}})

    assert path.read_text() == before          # comments intact
    assert "A comment the operator wrote" in path.read_text()
    runtime = yaml.safe_load(store.runtime_path.read_text())
    assert runtime == {"risk": {"per_trade_fraction": 0.02}}
    assert store.config.risk.per_trade_fraction == 0.02


def test_runtime_overrides_survive_restart_and_layer_on_top(tmp_path):
    store, path = store_at(tmp_path)
    store.update({"tracking": {"tick_sec": 2.0}})

    reopened = ConfigStore(path=path)
    assert reopened.config.tracking.tick_sec == 2.0
    # The hand-written value still wins where no override exists.
    assert reopened.config.tracking.min_interval_sec == 7.0


def test_deleting_the_runtime_file_reverts_to_the_written_config(tmp_path):
    store, path = store_at(tmp_path)
    store.update({"tracking": {"min_interval_sec": 30.0}})
    store.runtime_path.unlink()

    assert ConfigStore(path=path).config.tracking.min_interval_sec == 7.0


def test_only_whitelisted_sections_are_updatable(tmp_path):
    store, _ = store_at(tmp_path)
    with pytest.raises(ValueError, match="not updatable"):
        store.update({"chain": {"helius_api_key": "leaked"}})
    with pytest.raises(ValueError, match="unknown option"):
        store.update({"risk": {"nonexistent": 1}})


def test_unknown_sections_and_keys_do_not_prevent_startup(tmp_path):
    """An old config must never keep the system from booting."""
    store, _ = store_at(tmp_path, "dev_mode: false\n"
                                  "hft: true\n"
                                  "follow:\n  poll_interval_sec: 10\n"
                                  "risk:\n  from_the_future: 1\n")
    assert store.config.dev_mode is False


# -- HFT mode is gone ------------------------------------------------------

def test_no_profile_machinery_remains(tmp_path):
    store, _ = store_at(tmp_path)
    assert not hasattr(store, "profile_name")
    assert not hasattr(store, "profile_path")
    assert not hasattr(store.config, "hft")
    assert "hft" not in store.config.to_dict()


# -- sources & routing -----------------------------------------------------

def test_helius_is_enabled_only_by_its_key(tmp_path):
    store, _ = store_at(tmp_path, "chain: {}\n")
    assert store.config.sources["helius"].enabled is False

    keyed, _ = store_at(tmp_path, "chain:\n  helius_api_key: abc123\n",
                        name="keyed.yaml")
    helius = keyed.config.sources["helius"]
    assert helius.enabled is True
    assert helius.api_key == "abc123"


def test_measured_source_limits_are_the_defaults(tmp_path):
    """These numbers came from probes against the live endpoints."""
    store, _ = store_at(tmp_path, "dev_mode: false\n")
    sources = store.config.sources
    assert sources["publicnode"].supports_batch is True
    assert sources["publicnode"].max_wallet_calls_per_sec == 10.0
    # Helius refused a 50-element batch in 34 ms; 10 was served.
    assert sources["helius"].max_batch == 10
    # mainnet-beta returned per-element 429s and out-of-order responses.
    assert sources["mainnet_beta"].supports_batch is False


def test_helius_is_not_a_tracking_peer(tmp_path):
    """A credit-metered source cannot carry a continuous heartbeat."""
    store, _ = store_at(tmp_path, "dev_mode: false\n")
    assert "helius" not in store.config.routing.tracking


def test_routing_to_an_unknown_source_fails_at_startup(tmp_path):
    with pytest.raises(ValueError, match="unknown source"):
        store_at(tmp_path, "routing:\n  tracking: [typo_node]\n")


def test_empty_routing_policy_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="at least one source"):
        store_at(tmp_path, "routing:\n  tracking: []\n")


def test_tracking_with_no_enabled_source_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="no enabled source"):
        store_at(tmp_path,
                 "sources:\n"
                 "  publicnode:\n    enabled: false\n"
                 "  mainnet_beta:\n    enabled: false\n")


def test_non_positive_intervals_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="must be positive"):
        store_at(tmp_path, "tracking:\n  tick_sec: 0\n")


def test_win_rate_unit_trap_is_still_caught(tmp_path):
    """0.7 must never be read as 0.7% — this admitted 20%-win traders."""
    with pytest.raises(ValueError, match="FRACTION"):
        store_at(tmp_path, "filters_onchain:\n  min_win_rate: 70\n")


def test_config_dict_roundtrips_nested_sources(tmp_path):
    store, _ = store_at(tmp_path)
    as_dict = store.config.to_dict()
    assert isinstance(as_dict["sources"], dict)
    assert as_dict["sources"]["publicnode"]["max_batch"] == 100
    assert as_dict["routing"]["tracking"] == ["publicnode", "mainnet_beta"]


def test_mutable_sections_cover_the_new_tracking_section():
    assert "tracking" in AppConfig._MUTABLE_SECTIONS
    assert "paper_fills" in AppConfig._MUTABLE_SECTIONS


# -- credentials must never reach a browser --------------------------------

def test_public_config_redacts_every_credential_shaped_key():
    """`sources.*.api_key` is filled from `chain.helius_api_key` at load,
    so a redactor that only knew about `chain` would have published the
    key through the source block to every connected client."""
    import json

    from olala.api.server import _redact_secrets

    payload = _redact_secrets({
        "chain": {"helius_api_key": "SECRET-A", "request_timeout_sec": 15},
        "sources": {"helius": {"api_key": "SECRET-B", "max_batch": 10}},
        "nested": [{"solana_tracker_api_key": "SECRET-C"}],
        "future_section": {"auth_token": "SECRET-D", "keep": 1},
    })

    blob = json.dumps(payload)
    for secret in ("SECRET-A", "SECRET-B", "SECRET-C", "SECRET-D"):
        assert secret not in blob
    assert payload["chain"]["request_timeout_sec"] == 15
    assert payload["sources"]["helius"]["max_batch"] == 10
    assert payload["future_section"]["keep"] == 1
