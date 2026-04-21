"""Build the XMTP sidecar as part of pip install.

The plugin's main metadata lives in pyproject.toml (PEP 621). This file exists
solely so we can hook setuptools to run ``npm ci && npm run build`` inside
``xmtp_sidecar/`` after the Python package installs.

Modern pip uses the PEP 517 build backend:
- ``pip install .``      → calls ``setuptools.build_meta:build_wheel``
                           → runs ``build_py`` command  ← hooked here
- ``pip install -e .``   → calls ``setuptools.build_meta:build_editable``
                           → runs ``editable_wheel`` command ← hooked here
- Legacy ``python setup.py install`` → runs ``install`` command ← hooked too

Skip the sidecar build entirely with:
    SHERWOOD_MONITOR_SKIP_SIDECAR_BUILD=1 pip install ...
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.develop import develop as _develop
from setuptools.command.install import install as _install

ROOT = Path(__file__).resolve().parent
SIDECAR_DIR = ROOT / "xmtp_sidecar"
SKIP_ENV = "SHERWOOD_MONITOR_SKIP_SIDECAR_BUILD"


def _banner(msg: str) -> str:
    bar = "=" * 72
    return f"\n{bar}\n[sherwood-monitor] {msg}\n{bar}\n"


def _build_sidecar() -> None:
    if os.environ.get(SKIP_ENV) == "1":
        sys.stderr.write(_banner(f"sidecar build skipped ({SKIP_ENV}=1)"))
        return

    if not SIDECAR_DIR.exists():
        sys.stderr.write(
            _banner(
                f"sidecar directory missing at {SIDECAR_DIR}; XMTP unavailable."
            )
        )
        return

    npm = shutil.which("npm")
    if npm is None:
        sys.stderr.write(
            _banner(
                "npm not found on PATH. XMTP will be unavailable until you "
                "install Node >=20 and run:\n"
                f"    cd {SIDECAR_DIR}\n"
                "    npm ci && npm run build\n"
                "On-chain monitoring, risk hooks, and exposure still work."
            )
        )
        return

    sys.stderr.write(
        f"\n[sherwood-monitor] building XMTP sidecar in {SIDECAR_DIR} "
        "(this takes ~30s; one-time)...\n"
    )

    rc = subprocess.call([npm, "ci", "--no-audit", "--no-fund"], cwd=str(SIDECAR_DIR))
    if rc != 0:
        sys.stderr.write(
            _banner(
                f"npm ci failed (exit {rc}). XMTP unavailable until rebuilt:\n"
                f"    cd {SIDECAR_DIR} && npm ci && npm run build\n"
                "If this is a sandboxed install, try:\n"
                f"    {SKIP_ENV}=1 pip install ..."
            )
        )
        return

    rc = subprocess.call([npm, "run", "build"], cwd=str(SIDECAR_DIR))
    if rc != 0:
        sys.stderr.write(
            _banner(
                f"tsc build failed (exit {rc}). XMTP unavailable until rebuilt:\n"
                f"    cd {SIDECAR_DIR} && npm run build"
            )
        )
        return

    sys.stderr.write("[sherwood-monitor] sidecar build complete.\n")


class BuildPyWithSidecar(_build_py):
    """Hook build_py — called by pip install . (wheel build path)."""

    def run(self) -> None:
        super().run()
        _build_sidecar()


# editable_wheel is the PEP 660 command used by pip install -e .
# Import it lazily so we don't break on older setuptools that lack it.
try:
    from setuptools.command.editable_wheel import editable_wheel as _editable_wheel

    class EditableWheelWithSidecar(_editable_wheel):
        """Hook editable_wheel — called by pip install -e . (PEP 660 path)."""

        def run(self) -> None:
            super().run()
            _build_sidecar()

    _extra_cmdclass: dict = {"editable_wheel": EditableWheelWithSidecar}
except ImportError:
    _extra_cmdclass = {}


# Keep legacy install / develop for pip install --no-build-isolation,
# python setup.py install, etc.
class InstallWithSidecar(_install):
    def run(self) -> None:
        super().run()
        _build_sidecar()


class DevelopWithSidecar(_develop):
    def run(self) -> None:
        super().run()
        _build_sidecar()


setup(
    cmdclass={
        "build_py": BuildPyWithSidecar,
        "install": InstallWithSidecar,
        "develop": DevelopWithSidecar,
        **_extra_cmdclass,
    }
)
