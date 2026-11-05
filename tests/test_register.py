from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Test-only key (not a real wallet). The plugin takes it from the declared secret
# SHERWOOD_PRIVATE_KEY, never from the Sherwood CLI's config file.
TEST_KEY = "0x" + "11" * 32


def _first_turn(mock_ctx) -> None:
    """Fire the plugin's pre_llm_call (the first turn starts the sidecar on the plugin's
    own loop), then give that loop a moment and stop it."""
    import asyncio

    import sherwood_monitor

    (pre_llm,) = [c.args[1] for c in mock_ctx.register_hook.call_args_list if c.args[0] == "pre_llm_call"]
    asyncio.run(pre_llm(session_id="s1"))
    sherwood_monitor._RUNTIME["bg"].run(asyncio.sleep(0.2), timeout=5)
    sherwood_monitor._RUNTIME["teardown"]()


def _make_mock_sidecar(*, address="0xSIDECARWALLET", member_of: list[str] | None = None):
    """Build a mock Sidecar suitable for register() tests."""
    member_of = member_of if member_of is not None else []
    mock = MagicMock()
    mock.start = AsyncMock(return_value={"address": address, "inbox_id": "inbox_abc"})
    mock.address = address
    mock.sidecar_ok = True
    mock.call = AsyncMock(return_value={"ok": True, "group_id": "0xGROUP"})
    mock.is_member = AsyncMock(side_effect=lambda sub: sub in member_of)
    mock.stream_start = AsyncMock(return_value="stream_1")
    mock.on_stream_event = MagicMock()
    mock.shutdown = AsyncMock()
    return mock


def test_register_calls_all_ctx_methods(tmp_path: Path, mock_ctx, monkeypatch):
    monkeypatch.setenv("SHERWOOD_PRIVATE_KEY", TEST_KEY)

    # Sidecar dist exists so sidecar is attempted
    sidecar_dir = tmp_path / "xmtp_sidecar"
    (sidecar_dir / "dist").mkdir(parents=True)
    (sidecar_dir / "dist" / "index.js").write_text("// built")

    mock_sidecar_instance = _make_mock_sidecar(member_of=["alpha-fund"])

    fake_version = MagicMock(returncode=0, stdout="0.5.0")
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("sherwood_monitor.preflight.subprocess.run", MagicMock(return_value=fake_version)), \
         patch("sherwood_monitor._sidecar_dir", return_value=sidecar_dir), \
         patch("sherwood_monitor.Sidecar", return_value=mock_sidecar_instance):
        from sherwood_monitor import register
        register(mock_ctx)

    # 5 monitor tools + 11 fund tools, 5 hooks (session_start, session_end,
    # pre_tool_call, post_tool_call, pre_llm_call), ONE `hermes sherwood` CLI
    # command (start/stop/status/tail/install-cron are its subcommands),
    # 5 slash commands, 1 + 3 fund skills
    assert mock_ctx.register_tool.call_count == 16
    assert mock_ctx.register_hook.call_count == 5
    assert mock_ctx.register_cli_command.call_count == 1
    assert mock_ctx.register_command.call_count == 5
    assert mock_ctx.register_skill.call_count == 4


def test_register_with_missing_cli_still_registers(tmp_path: Path, mock_ctx, monkeypatch):
    # No SHERWOOD_PRIVATE_KEY — sidecar will be skipped (no-op path)
    monkeypatch.delenv("SHERWOOD_PRIVATE_KEY", raising=False)
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("sherwood_monitor.preflight.subprocess.run", MagicMock(side_effect=FileNotFoundError)):
        from sherwood_monitor import register
        register(mock_ctx)

    # Tools/hooks still register even when CLI is missing
    assert mock_ctx.register_tool.call_count == 16
    # pre_llm_call hook was registered (buffer carries the warning now)
    hook_names = [call.args[0] for call in mock_ctx.register_hook.call_args_list]
    assert "pre_llm_call" in hook_names


def test_register_starts_sidecar_when_key_is_set(tmp_path: Path, mock_ctx, monkeypatch):
    """Sidecar constructor is called and start() is scheduled when SHERWOOD_PRIVATE_KEY is set."""
    monkeypatch.setenv("SHERWOOD_PRIVATE_KEY", TEST_KEY)

    sidecar_dir = tmp_path / "xmtp_sidecar"
    (sidecar_dir / "dist").mkdir(parents=True)
    (sidecar_dir / "dist" / "index.js").write_text("// built")

    mock_sidecar_instance = _make_mock_sidecar()
    mock_sidecar_cls = MagicMock(return_value=mock_sidecar_instance)

    fake_version = MagicMock(returncode=0, stdout="0.5.0")
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("sherwood_monitor.preflight.subprocess.run", MagicMock(return_value=fake_version)), \
         patch("sherwood_monitor._sidecar_dir", return_value=sidecar_dir), \
         patch("sherwood_monitor.Sidecar", mock_sidecar_cls):
        from sherwood_monitor import register
        register(mock_ctx)

    mock_sidecar_cls.assert_called_once()
    # start() is called inside the background task — the task is created (not awaited here)
    # so we verify the Sidecar was constructed with the right params
    call_kwargs = mock_sidecar_cls.call_args
    assert call_kwargs is not None
    # sidecar_dir and db_path should be passed
    assert "sidecar_dir" in call_kwargs.kwargs or len(call_kwargs.args) >= 1


def test_register_injects_membership_warnings(tmp_path: Path, mock_ctx, monkeypatch):
    """When sidecar.is_member returns False, a warning is pushed to the buffer."""
    monkeypatch.setenv("SHERWOOD_PRIVATE_KEY", TEST_KEY)

    sidecar_dir = tmp_path / "xmtp_sidecar"
    (sidecar_dir / "dist").mkdir(parents=True)
    (sidecar_dir / "dist" / "index.js").write_text("// built")

    # alpha-fund: NOT a member
    mock_sidecar_instance = _make_mock_sidecar(address="0xABCDEF", member_of=[])
    mock_sidecar_cls = MagicMock(return_value=mock_sidecar_instance)

    injected_warnings: list[str] = []
    import asyncio

    # Intercept EventBuffer.push to capture injected messages
    original_register = None

    fake_version = MagicMock(returncode=0, stdout="0.5.0")

    # We need to intercept the buffer's push calls — use a custom config
    # with alpha-fund in funds so the membership check fires
    import yaml as _yaml
    cfg_dir = tmp_path / ".hermes" / "plugins" / "sherwood-monitor"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text(
        _yaml.dump({"funds": ["alpha-fund"], "sherwood_bin": "sherwood"})
    )

    from sherwood_monitor.event_buffer import EventBuffer

    original_push = EventBuffer.push

    def capturing_push(self, msg: str) -> None:
        injected_warnings.append(msg)
        return original_push(self, msg)

    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("sherwood_monitor.preflight.subprocess.run", MagicMock(return_value=fake_version)), \
         patch("sherwood_monitor._sidecar_dir", return_value=sidecar_dir), \
         patch("sherwood_monitor.Sidecar", mock_sidecar_cls), \
         patch.object(EventBuffer, "push", capturing_push):
        from sherwood_monitor import register
        register(mock_ctx)

        # Run the event loop briefly to let the background start task execute
        _first_turn(mock_ctx)

    # The background task should have pushed a membership warning
    all_text = "\n".join(injected_warnings)
    assert "alpha-fund" in all_text
    assert "0xABCDEF" in all_text or "sherwood chat alpha-fund add" in all_text


def test_register_stream_starts_per_fund(tmp_path: Path, mock_ctx, monkeypatch):
    """stream_start is called for each fund where the sidecar is a member."""
    monkeypatch.setenv("SHERWOOD_PRIVATE_KEY", TEST_KEY)

    sidecar_dir = tmp_path / "xmtp_sidecar"
    (sidecar_dir / "dist").mkdir(parents=True)
    (sidecar_dir / "dist" / "index.js").write_text("// built")

    # alpha-fund IS a member, beta-fund is NOT
    mock_sidecar_instance = _make_mock_sidecar(member_of=["alpha-fund"])
    mock_sidecar_cls = MagicMock(return_value=mock_sidecar_instance)

    import asyncio
    import yaml as _yaml

    cfg_dir = tmp_path / ".hermes" / "plugins" / "sherwood-monitor"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text(
        _yaml.dump({"funds": ["alpha-fund", "beta-fund"], "sherwood_bin": "sherwood"})
    )

    fake_version = MagicMock(returncode=0, stdout="0.5.0")
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("sherwood_monitor.preflight.subprocess.run", MagicMock(return_value=fake_version)), \
         patch("sherwood_monitor._sidecar_dir", return_value=sidecar_dir), \
         patch("sherwood_monitor.Sidecar", mock_sidecar_cls):
        from sherwood_monitor import register
        register(mock_ctx)

        _first_turn(mock_ctx)

    # stream_start should be called once (only for alpha-fund, the member)
    mock_sidecar_instance.stream_start.assert_awaited_once_with("alpha-fund")


def test_register_never_reads_the_sherwood_cli_config(tmp_path: Path, mock_ctx, monkeypatch):
    """A key in ~/.sherwood/config.json is ignored: no SHERWOOD_PRIVATE_KEY, no sidecar (catalog rule 11)."""
    monkeypatch.delenv("SHERWOOD_PRIVATE_KEY", raising=False)
    (tmp_path / ".sherwood").mkdir()
    (tmp_path / ".sherwood" / "config.json").write_text('{"privateKey": "' + TEST_KEY + '"}')
    sidecar_dir = tmp_path / "xmtp_sidecar"
    (sidecar_dir / "dist").mkdir(parents=True)
    (sidecar_dir / "dist" / "index.js").write_text("// built")
    mock_sidecar_cls = MagicMock()

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
         patch("sherwood_monitor._sidecar_dir", return_value=sidecar_dir), \
         patch("sherwood_monitor.Sidecar", mock_sidecar_cls), \
         patch.object(Path, "read_text", spy_read_text), \
         patch.object(Path, "read_bytes", spy_read_bytes), \
         patch.object(Path, "open", spy_open):
        from sherwood_monitor import register
        register(mock_ctx)

    mock_sidecar_cls.assert_not_called()
    assert not any(p.endswith("/.sherwood/config.json") for p in opened)


def test_register_refuses_a_malformed_key(tmp_path: Path, mock_ctx, monkeypatch):
    monkeypatch.setenv("SHERWOOD_PRIVATE_KEY", "not-a-key")
    sidecar_dir = tmp_path / "xmtp_sidecar"
    (sidecar_dir / "dist").mkdir(parents=True)
    (sidecar_dir / "dist" / "index.js").write_text("// built")
    mock_sidecar_cls = MagicMock()
    fake_version = MagicMock(returncode=0, stdout="0.71.0")
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("sherwood_monitor.preflight.subprocess.run", MagicMock(return_value=fake_version)), \
         patch("sherwood_monitor._sidecar_dir", return_value=sidecar_dir), \
         patch("sherwood_monitor.Sidecar", mock_sidecar_cls):
        from sherwood_monitor import register
        register(mock_ctx)
    mock_sidecar_cls.assert_not_called()
    assert mock_ctx.register_tool.call_count == 16
