"""The Hermes catalog entry in catalog/ must match plugin.yaml (catalog rule 6)."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog" / "sherwood-monitor.yaml"
SHA_PLACEHOLDER = "<sha of the v0.7.0 tag commit>"


def _load() -> tuple[dict, dict]:
    return yaml.safe_load(CATALOG.read_text()), yaml.safe_load((ROOT / "plugin.yaml").read_text())


def test_catalog_capabilities_mirror_the_manifest():
    entry, manifest = _load()
    caps = entry["capabilities"]
    assert caps["provides_tools"] == manifest["provides_tools"]
    assert caps["provides_hooks"] == manifest["provides_hooks"]
    assert caps["provides_middleware"] == []
    assert caps["requires_env"] == [e["name"] for e in manifest["requires_env"]]


def test_catalog_entry_metadata_matches_the_manifest():
    entry, manifest = _load()
    assert entry["name"] == manifest["name"] == "sherwood-monitor"
    assert entry["version"] == manifest["version"]
    assert entry["requires_hermes"] == manifest["requires_hermes"]
    assert entry["repo"] == "https://github.com/sherwoodagent/sherwood-hermes-plugin"
    assert entry["tier"] == "community"
    assert entry["category"] in {"desktop", "memory", "platform", "web", "tools", "voice", "automation", "models", "general"}
    for key in ("description", "maintainer"):
        assert isinstance(entry[key], str) and entry[key].strip()
    assert "Disclosure:" in entry["description"]


def test_catalog_sha_is_a_placeholder_or_a_full_pin():
    entry, _ = _load()
    # The file in this repo carries the placeholder; the copy submitted to the
    # catalog carries the 40-hex commit of the release tag.
    assert entry["sha"] == SHA_PLACEHOLDER or re.fullmatch(r"[0-9a-f]{40}", entry["sha"])
