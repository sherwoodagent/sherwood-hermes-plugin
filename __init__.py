"""Hermes directory-plugin entry point.

Hermes loads a directory plugin by importing the ``__init__.py`` that sits next
to ``plugin.yaml``, as ``hermes_plugins.<slug>`` with this directory as its
package path, and calls ``register(ctx)``. The code lives in the
``sherwood_monitor`` package (also what ``pip install`` ships), so this file only
re-exports its ``register``. Under Hermes the import is relative, because
``sherwood_monitor`` is a sub-package of the plugin module. Tools that import
this file as a plain top-level module (pytest collecting the repo root) have no
package to be relative to, so they get the installed / on-path package.
"""
if __package__:
    from .sherwood_monitor import register
else:
    from sherwood_monitor import register

__all__ = ["register"]
