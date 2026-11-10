from pathlib import Path

import pytest

from sherwood_monitor.config import Config, DEFAULT_CONFIG_YAML, load_config


def test_load_missing_file_creates_default(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    cfg = load_config(cfg_path)
    assert cfg_path.exists()
    assert cfg_path.read_text() == DEFAULT_CONFIG_YAML
    assert cfg.funds == []
    assert cfg.auto_start is False
    assert "xmtp" not in DEFAULT_CONFIG_YAML


def test_load_existing_valid(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
funds:
  - alpha
  - beta
auto_start: true
sherwood_bin: /usr/local/bin/sherwood
backoff_max_seconds: 60
inject_mentions_only: false
concentration_threshold_pct: 25.0
""".strip()
    )
    cfg = load_config(cfg_path)
    assert cfg.funds == ["alpha", "beta"]
    assert cfg.auto_start is True
    assert cfg.sherwood_bin == "/usr/local/bin/sherwood"
    assert cfg.backoff_max_seconds == 60
    assert cfg.inject_mentions_only is False
    assert cfg.concentration_threshold_pct == 25.0


def test_load_malformed_yaml_raises(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("this: is: not: valid")
    with pytest.raises(ValueError, match="config parse error"):
        load_config(cfg_path)


def test_load_wrong_types_raises(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("funds: not-a-list")
    with pytest.raises(ValueError, match="funds must be a list"):
        load_config(cfg_path)


def test_load_negative_backoff_raises(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("backoff_max_seconds: -1")
    with pytest.raises(ValueError, match="backoff_max_seconds must be positive"):
        load_config(cfg_path)


def test_load_default_concentration_threshold(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    cfg = load_config(cfg_path)
    assert cfg.concentration_threshold_pct == 30.0


def test_an_old_config_with_xmtp_keys_still_loads(tmp_path: Path, caplog):
    """0.8.x wrote `xmtp_summaries`; 0.9.0 ignores it with one log line and loads the rest."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("funds: [alpha]\nauto_start: true\nxmtp_summaries: false\n")
    with caplog.at_level("DEBUG", logger="sherwood_monitor.config"):
        cfg = load_config(cfg_path)
    assert cfg.funds == ["alpha"] and cfg.auto_start is True
    assert not hasattr(cfg, "xmtp_summaries")
    records = [r for r in caplog.records if r.name == "sherwood_monitor.config"]
    assert len(records) == 1 and "xmtp_summaries" in records[0].getMessage()
