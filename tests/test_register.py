from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Test-only key (not a real wallet). The plugin takes it from the declared secret
# SHERWOOD_PRIVATE_KEY, never from the Sherwood CLI's config file.
TEST_KEY = "0x" + "11" * 32


def test_register_calls_all_ctx_methods(tmp_path: Path, mock_ctx, monkeypatch):
    monkeypatch.setenv("SHERWOOD_PRIVATE_KEY", TEST_KEY)

    fake_version = MagicMock(returncode=0, stdout="0.5.0")
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("sherwood_monitor.preflight.subprocess.run", MagicMock(return_value=fake_version)):
        from sherwood_monitor import register
        register(mock_ctx)

    # 5 monitor tools + 11 fund tools, 5 hooks (session_start, session_end,
    # pre_tool_call, post_tool_call, pre_llm_call), ONE `hermes sherwood` CLI
    # command (start/stop/status/tail/install-cron are its subcommands),
    # 5 slash commands, 1 + 3 fund skills
    assert mock_ctx.register_tool.call_count == 17
    assert mock_ctx.register_hook.call_count == 5
    assert mock_ctx.register_cli_command.call_count == 1
    assert mock_ctx.register_command.call_count == 5
    assert mock_ctx.register_skill.call_count == 4


def test_register_with_missing_cli_still_registers(tmp_path: Path, mock_ctx, monkeypatch):
    monkeypatch.delenv("SHERWOOD_PRIVATE_KEY", raising=False)
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("sherwood_monitor.preflight.subprocess.run", MagicMock(side_effect=FileNotFoundError)):
        from sherwood_monitor import register
        register(mock_ctx)

    # Tools/hooks still register even when CLI is missing
    assert mock_ctx.register_tool.call_count == 17
    # pre_llm_call hook was registered (buffer carries the warning now)
    hook_names = [call.args[0] for call in mock_ctx.register_hook.call_args_list]
    assert "pre_llm_call" in hook_names


def test_register_never_reads_the_sherwood_cli_config(tmp_path: Path, mock_ctx, monkeypatch):
    """A key in ~/.sherwood/config.json is ignored: the plugin never opens it (catalog rule 11)."""
    monkeypatch.delenv("SHERWOOD_PRIVATE_KEY", raising=False)
    (tmp_path / ".sherwood").mkdir()
    (tmp_path / ".sherwood" / "config.json").write_text('{"privateKey": "' + TEST_KEY + '"}')

    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes
    real_open = Path.open
    opened: list[str] = []

    def spy_read_text(self, *args, **kwargs):
        opened.append(str(self))
        return real_read_text(self, *args, **kwargs)

    def spy_read_bytes(self, *args, **kwargs):
        opened.append(str(self))
        return real_read_bytes(self, *args, **kwargs)

    def spy_open(self, *args, **kwargs):
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    fake_version = MagicMock(returncode=0, stdout="0.71.0")
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("sherwood_monitor.preflight.subprocess.run", MagicMock(return_value=fake_version)), \
         patch.object(Path, "read_text", spy_read_text), \
         patch.object(Path, "read_bytes", spy_read_bytes), \
         patch.object(Path, "open", spy_open):
        from sherwood_monitor import register
        register(mock_ctx)

    assert not any(p.endswith("/.sherwood/config.json") for p in opened)


def test_register_with_a_malformed_key_still_loads(tmp_path: Path, mock_ctx, monkeypatch):
    """A malformed SHERWOOD_PRIVATE_KEY is no monitor wallet; the plugin still loads."""
    import sherwood_monitor

    monkeypatch.setenv("SHERWOOD_PRIVATE_KEY", "not-a-key")
    monkeypatch.delenv("SHERWOOD_AGENT_ADDRESS", raising=False)
    fake_version = MagicMock(returncode=0, stdout="0.71.0")
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("sherwood_monitor.preflight.subprocess.run", MagicMock(return_value=fake_version)):
        sherwood_monitor.register(mock_ctx)
    assert sherwood_monitor._monitor_wallet_set() is False
    assert mock_ctx.register_tool.call_count == 17


def test_an_install_with_a_built_xmtp_sidecar_on_disk_still_loads(tmp_path: Path, mock_ctx, monkeypatch):
    """Upgrading from 0.8.x with files 0.8.x left on disk.

    `hermes plugins install --force --ref` carries untracked files into the new tree except
    `node_modules/`, so a built `xmtp_sidecar/dist/` and its logs may remain; an unpinned
    `hermes plugins update` (git pull) also keeps `node_modules/`. Seeded here: all three,
    the sidecar database under ~/.hermes/xmtp/ and a config that still has `xmtp_summaries`.
    With `auto_start: true` the first turn starts the fund's stream: the only process spawned
    is `sherwood session check <fund> --stream --no-xmtp` (no node), its event arrives, nothing
    mentions a sidecar or XMTP, and the leftovers are untouched.
    """
    import asyncio

    import yaml

    import sherwood_monitor
    from sherwood_monitor.event_buffer import EventBuffer
    from tests.test_lifecycle import _stub_sherwood, _wait_for

    monkeypatch.setenv("SHERWOOD_PRIVATE_KEY", TEST_KEY)
    plugin_dir = tmp_path / ".hermes" / "plugins" / "sherwood-monitor"
    leftovers = (
        "xmtp_sidecar/dist/index.js",
        "xmtp_sidecar/npm-debug.log",
        "xmtp_sidecar/node_modules/@xmtp/node-sdk/package.json",
    )
    for rel in leftovers:
        (plugin_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (plugin_dir / rel).write_text("// left by 0.8.x")
    db = tmp_path / ".hermes" / "xmtp" / "sidecar.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"old-db")
    stub = _stub_sherwood(tmp_path)
    (plugin_dir / "config.yaml").write_text(yaml.safe_dump({
        "funds": ["alpha-fund"], "auto_start": True, "sherwood_bin": str(stub), "xmtp_summaries": True,
    }))

    pushed: list[str] = []
    original_push = EventBuffer.push

    def capture(self, msg: str) -> None:
        pushed.append(msg)
        return original_push(self, msg)

    spawned: list[tuple] = []
    real_spawn = asyncio.create_subprocess_exec

    async def record_spawn(*args, **kwargs):
        spawned.append(tuple(str(a) for a in args))
        return await real_spawn(*args, **kwargs)

    fake_version = MagicMock(returncode=0, stdout="0.71.0")
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("sherwood_monitor.preflight.subprocess.run", MagicMock(return_value=fake_version)), \
         patch.object(EventBuffer, "push", capture), \
         patch.object(asyncio, "create_subprocess_exec", record_spawn):
        sherwood_monitor.register(mock_ctx)
        (pre_llm,) = [c.args[1] for c in mock_ctx.register_hook.call_args_list if c.args[0] == "pre_llm_call"]
        try:
            asyncio.run(pre_llm(session_id="s1"))  # the first turn starts the auto_start stream
            assert _wait_for(lambda: any('type="VoteCast"' in m for m in pushed)), "the stream's event never arrived"
        finally:
            sherwood_monitor._RUNTIME["teardown"]()

    assert mock_ctx.register_tool.call_count == 17
    assert mock_ctx.register_hook.call_count == 5
    assert spawned == [(str(stub), "session", "check", "alpha-fund", "--stream", "--no-xmtp")]
    assert not any("sidecar" in m.lower() or "xmtp" in m.lower() for m in pushed)
    assert db.read_bytes() == b"old-db"
    for rel in leftovers:
        assert (plugin_dir / rel).read_text() == "// left by 0.8.x"
