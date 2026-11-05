"""Fund state persistence.

Telegram bots never see each other's messages, so a host Hermes and its guest
Hermes agents coordinate only through this shared state. Two backends sit
behind one `FundStateBackend` protocol:

- `LocalJsonBackend`: one JSON file per fund under
  `~/.hermes/plugins/sherwood-monitor/funds/<fund>.json`. Host and guest
  processes on the same machine share it. Every write goes through `update()`,
  which holds an exclusive file lock across read -> mutate -> atomic replace,
  so two processes never interleave a read-modify-write.
- `HttpFundStateBackend`: stub for guests on other machines. The hosted
  fund-state API (another lane) fills it in; see the class docstring for the
  contract it must keep.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, TypeVar, runtime_checkable

from .models import SCHEMA_VERSION, FundState

try:  # POSIX (macOS, Linux): real advisory locks.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore[assignment]

T = TypeVar("T")

STATE_DIR_ENV = "SHERWOOD_FUND_STATE_DIR"
_FUND_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class FundStoreError(Exception):
    """Base error for fund state persistence."""


class FundNotFound(FundStoreError):
    pass


class FundExists(FundStoreError):
    pass


class SchemaTooNew(FundStoreError):
    pass


class LockTimeout(FundStoreError):
    pass


def default_state_dir() -> Path:
    """`$SHERWOOD_FUND_STATE_DIR`, else `~/.hermes/plugins/sherwood-monitor/funds`.

    Resolved lazily so tests can patch `Path.home()`.
    """
    env = os.environ.get(STATE_DIR_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes" / "plugins" / "sherwood-monitor" / "funds"


def validate_fund_name(name: str) -> str:
    """Fund names are Sherwood subdomains; they also become file names."""
    clean = (name or "").strip().lower()
    if not _FUND_NAME_RE.match(clean):
        raise FundStoreError(
            f"invalid fund name {name!r}: use lowercase letters, digits and dashes (max 63)"
        )
    return clean


@runtime_checkable
class FundStateBackend(Protocol):
    """Where fund state lives. All writes go through `create` or `update`."""

    def exists(self, fund: str) -> bool: ...

    def load(self, fund: str) -> FundState:
        """Return the fund's state; raise FundNotFound if there is none."""
        ...

    def create(self, state: FundState) -> FundState:
        """Persist a brand-new fund; raise FundExists if one is already there."""
        ...

    def update(self, fund: str, mutate: Callable[[FundState], T]) -> T:
        """Atomically load, apply `mutate` (in place), bump `revision`, save.

        If `mutate` raises, nothing is written and the exception propagates.
        Returns whatever `mutate` returned.
        """
        ...

    def list_funds(self) -> list[str]: ...


def _check_schema(raw: dict[str, Any], fund: str) -> None:
    version = int(raw.get("schema_version", 1))
    if version > SCHEMA_VERSION:
        raise SchemaTooNew(
            f"fund {fund!r} state is schema v{version}; this plugin reads up to "
            f"v{SCHEMA_VERSION}. Upgrade sherwood-monitor."
        )


class LocalJsonBackend:
    """One JSON file per fund, shared by every Hermes process on this machine."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        lock_timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._root = Path(root).expanduser() if root is not None else None
        self._lock_timeout = lock_timeout
        self._clock = clock

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else default_state_dir()

    def path_for(self, fund: str) -> Path:
        return self.root / f"{validate_fund_name(fund)}.json"

    # -- protocol ------------------------------------------------------------
    def exists(self, fund: str) -> bool:
        return self.path_for(fund).exists()

    def load(self, fund: str) -> FundState:
        path = self.path_for(fund)
        with self._locked(path, shared=True):
            return self._read(path, fund)

    def create(self, state: FundState) -> FundState:
        path = self.path_for(state.fund.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked(path):
            if path.exists():
                raise FundExists(f"fund {state.fund.name!r} already exists at {path}")
            state.schema_version = SCHEMA_VERSION
            state.revision = 1
            state.updated_at = self._clock()
            self._write(path, state)
        return state

    def update(self, fund: str, mutate: Callable[[FundState], T]) -> T:
        path = self.path_for(fund)
        with self._locked(path):
            state = self._read(path, fund)
            result = mutate(state)
            state.revision += 1
            state.updated_at = self._clock()
            self._write(path, state)
        return result

    def list_funds(self) -> list[str]:
        root = self.root
        if not root.exists():
            return []
        return sorted(p.stem for p in root.glob("*.json") if _FUND_NAME_RE.match(p.stem))

    # -- internals -------------------------------------------------------------
    def _read(self, path: Path, fund: str) -> FundState:
        if not path.exists():
            raise FundNotFound(f"no state for fund {fund!r} (looked in {path})")
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise FundStoreError(f"fund state {path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict) or "fund" not in raw:
            raise FundStoreError(f"fund state {path} is missing the `fund` record")
        _check_schema(raw, fund)
        return FundState.from_dict(raw)

    def _write(self, path: Path, state: FundState) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_dict(), indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)  # member telegram ids are not public
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    @contextmanager
    def _locked(self, path: Path, *, shared: bool = False) -> Iterator[None]:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        if _fcntl is None:  # pragma: no cover - Windows fallback
            with _exclusive_lockfile(lock_path, self._lock_timeout):
                yield
            return
        fh = open(lock_path, "a+")
        try:
            mode = _fcntl.LOCK_SH if shared else _fcntl.LOCK_EX
            deadline = time.monotonic() + self._lock_timeout
            while True:
                try:
                    _fcntl.flock(fh.fileno(), mode | _fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockTimeout(f"timed out waiting for lock on {lock_path}")
                    time.sleep(0.02)
            try:
                yield
            finally:
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
        finally:
            fh.close()


@contextmanager
def _exclusive_lockfile(lock_path: Path, timeout: float) -> Iterator[None]:  # pragma: no cover
    deadline = time.monotonic() + timeout
    marker = lock_path.with_suffix(".excl")
    while True:
        try:
            fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise LockTimeout(f"timed out waiting for lock on {marker}")
            time.sleep(0.02)
    try:
        yield
    finally:
        try:
            os.unlink(marker)
        except FileNotFoundError:
            pass


class HttpFundStateBackend:
    """Stub: fund state served by the hosted fund-state API (another lane).

    Contract the implementation must keep, so callers do not change:

    - `GET  {base_url}/funds/{fund}/state` -> FundState JSON (`FundState.to_dict()` shape), 404 when absent.
    - `PUT  {base_url}/funds/{fund}/state` with header `If-Match: <revision>` -> 200 with the stored
      state, 409 when the revision moved (caller re-reads and re-applies `mutate`), 412 on schema skew.
    - `POST {base_url}/funds` -> create; 409 if the fund exists.
    - `update()` = GET, `mutate`, PUT with If-Match, retry on 409 (bounded).
    - Auth: a bearer token resolved through `sherwood_monitor.fund.secrets.get_secret`.
    """

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token = token

    def _todo(self) -> FundStoreError:
        return FundStoreError(
            "remote fund state is not available yet "
            f"(HTTP backend at {self.base_url or '<unset>'} is a stub). "
            "Run host and guest on the same machine, or set fund_state_backend: local."
        )

    def exists(self, fund: str) -> bool:
        raise self._todo()

    def load(self, fund: str) -> FundState:
        raise self._todo()

    def create(self, state: FundState) -> FundState:
        raise self._todo()

    def update(self, fund: str, mutate: Callable[[FundState], T]) -> T:
        raise self._todo()

    def list_funds(self) -> list[str]:
        raise self._todo()


def open_backend(
    kind: str = "local",
    *,
    state_dir: str | Path | None = None,
    state_url: str = "",
) -> FundStateBackend:
    """Build the configured backend (`local` or `http`)."""
    kind = (kind or "local").strip().lower()
    if kind == "local":
        return LocalJsonBackend(state_dir or None)
    if kind == "http":
        return HttpFundStateBackend(state_url)
    raise FundStoreError(f"unknown fund_state_backend {kind!r} (use local or http)")


_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][^\\/\s'\"`]+){2,}")


def scrub_paths(text: object) -> str:
    """Error text for chat or the model, without local file paths (`<path>` instead)."""
    return _PATH_RE.sub("<path>", str(text))
