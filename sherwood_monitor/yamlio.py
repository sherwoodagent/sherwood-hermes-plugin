"""YAML loading that works in a bare Hermes venv.

The plugin declares PyYAML, and a real install brings it in. Hermes itself
ships ruamel.yaml, not PyYAML, so a plugin probed before its deps are installed
(``hermes plugins validate`` / ``doctor`` without ``--install-deps``) would fail
on ``import yaml``. ``safe_load`` uses PyYAML when present and falls back to
ruamel's safe loader; both raise :class:`YAMLError` on bad input.
"""
from __future__ import annotations

from typing import Any

try:  # the declared dependency
    import yaml as _pyyaml
except ImportError:  # pragma: no cover - exercised only in a bare Hermes venv
    _pyyaml = None


class YAMLError(ValueError):
    """Raised for unparseable YAML, whichever parser read it."""


def safe_load(text: str) -> Any:
    if _pyyaml is not None:
        try:
            return _pyyaml.safe_load(text)
        except _pyyaml.YAMLError as exc:
            raise YAMLError(str(exc)) from exc
    try:  # pragma: no cover - fallback path
        from ruamel.yaml import YAML
        from ruamel.yaml.error import YAMLError as _RuamelError
    except ImportError as exc:  # pragma: no cover
        raise YAMLError("no YAML parser installed (pip install PyYAML)") from exc
    try:  # pragma: no cover
        return YAML(typ="safe", pure=True).load(text)
    except _RuamelError as exc:  # pragma: no cover
        raise YAMLError(str(exc)) from exc
