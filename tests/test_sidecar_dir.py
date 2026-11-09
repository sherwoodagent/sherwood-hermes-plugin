"""Where register() looks for the XMTP sidecar build (cold install of 0.8.3, finding 3).

Hermes loads the plugin from $HERMES_HOME/plugins/sherwood-monitor/ but installs its Python
package, editable, from a copy in its dependency workspace, where setup.py builds the sidecar.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import sherwood_monitor

OWN = Path(sherwood_monitor.__file__).parent.parent / "xmtp_sidecar"


class _Dist:
    def __init__(self, record: object):
        self.record = record

    def read_text(self, name: str) -> str | None:
        assert name == "direct_url.json"
        return None if self.record is None else json.dumps(self.record)


def _build(directory: Path) -> Path:
    (directory / "xmtp_sidecar" / "dist").mkdir(parents=True)
    (directory / "xmtp_sidecar" / "dist" / "index.js").write_text("// built")
    return directory / "xmtp_sidecar"


@pytest.fixture
def own_unbuilt(monkeypatch):
    real = sherwood_monitor._sidecar_built
    monkeypatch.setattr(sherwood_monitor, "_sidecar_built", lambda d: False if d == OWN else real(d))


def _installed(monkeypatch, record: object) -> None:
    monkeypatch.setattr(sherwood_monitor._metadata, "distribution", lambda name: _Dist(record))


def test_the_workspace_build_is_used_when_the_plugin_dir_has_none(tmp_path, monkeypatch, own_unbuilt):
    built = _build(tmp_path / "plugin-sources" / "sherwood-monitor-abc")
    _installed(monkeypatch, {"url": (tmp_path / "plugin-sources" / "sherwood-monitor-abc").as_uri(),
                             "dir_info": {"editable": True}})
    assert sherwood_monitor._sidecar_dir() == built


def test_a_path_with_spaces_in_the_record_is_decoded(tmp_path, monkeypatch, own_unbuilt):
    built = _build(tmp_path / "with space")
    _installed(monkeypatch, {"url": (tmp_path / "with space").as_uri(), "dir_info": {"editable": True}})
    assert sherwood_monitor._sidecar_dir() == built


@pytest.mark.parametrize("record", [
    None,  # no direct_url.json
    {"url": "https://example.com/x.whl", "dir_info": {"editable": True}},
    {"url": "file:///nowhere/at/all", "dir_info": {"editable": True}},  # not built there
    {"url": "file:///nowhere", "dir_info": {}},  # not editable
    "not a dict",
])
def test_without_a_workspace_build_the_plugin_dir_is_named(monkeypatch, own_unbuilt, record):
    _installed(monkeypatch, record)
    assert sherwood_monitor._sidecar_dir() == OWN  # where preflight tells the operator to build


def test_no_install_record_at_all_is_the_plugin_dir(monkeypatch, own_unbuilt):
    def missing(name):
        raise sherwood_monitor._metadata.PackageNotFoundError(name)

    monkeypatch.setattr(sherwood_monitor._metadata, "distribution", missing)
    assert sherwood_monitor._sidecar_dir() == OWN


def test_a_build_in_the_plugin_dir_wins(tmp_path, monkeypatch):
    _build(tmp_path / "elsewhere")
    _installed(monkeypatch, {"url": (tmp_path / "elsewhere").as_uri(), "dir_info": {"editable": True}})
    monkeypatch.setattr(sherwood_monitor, "_sidecar_built", lambda d: True)
    assert sherwood_monitor._sidecar_dir() == OWN
