// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
    function transfer(address, uint256) external returns (bool);
}

interface ISyndicateGovernor {
    struct Call {
        address target;
        bytes data;
        uint256 value;
    }

    /// @dev `IProtocolConfig.MgmtSplit` - THREE fields.
    struct MgmtSplit {
        uint16 agentBps;
        uint16 protocolBps;
        uint16 guardianBps;
    }

    /// @dev `IProtocolConfig.PerfSplit` - FOUR fields. The trailing `ownerBps` is
    ///      the one the mgmt split does not carry; both are static structs inlined
    ///      into the proposal's ABI head, so decoding this one as a 3-field struct
    ///      shifts every field after it by a word.
    struct PerfSplit {
        uint16 agentBps;
        uint16 protocolBps;
        uint16 guardianBps;
        uint16 ownerBps;
    }

    /// @dev 28 fields, in ABI order. Decoding a shorter or reordered struct
    ///      silently mis-reads every field after the first divergence, so keep
    ///      this in lockstep with `getProposal`'s tuple. Optimistic voting tallies
    ///      only the against-side, so `votesAgainst` is the sole vote counter.
    struct StrategyProposal {
        uint256 id;
        address proposer;
        address vault;
        address strategy;
        string metadataURI;
        uint256 performanceFeeBps;
        uint256 strategyDuration;
        uint256 votesAgainst;
        uint256 snapshotTimestamp;
        uint256 voteEnd;
        uint256 reviewEnd;
        uint256 executeBy;
        uint256 executedAt;
        uint8 state;
        uint256 vetoThresholdBps;
        address snapshotProtocolFeeRecipient;
        address snapshotGuardiansFeeRecipient;
        uint256 maxCapital;
        uint16 maxDrawdownBps;
        uint8 envelopeTier;
        uint256 requiredCoverage;
        uint256 proposerBondWood;
        address proposerBondEscrow;
        MgmtSplit snapshotMgmtSplit;
        PerfSplit snapshotPerfSplit;
        address proposerBondLedger;
        uint256 effectiveMaxCapital;
        uint256 votableSupply;
    }

    function getProposal(uint256 proposalId) external view returns (StrategyProposal memory);
    function getExecuteCalls(uint256 proposalId) external view returns (Call[] memory);
    function getSettlementCalls(uint256 proposalId) external view returns (Call[] memory);
    function getCapitalSnapshot(uint256 proposalId) external view returns (uint256);
    function getRequiredCoverage(uint256 proposalId) external view returns (uint256);
}

interface ISyndicateVault {
    function asset() external view returns (address);
    function totalAssets() external view returns (uint256);
    function agentFeeBps() external view returns (uint256);
}

/// @title SimulateProposal - Fork-test a governance proposal before it auto-passes
/// @notice Run with env vars: PROPOSAL_ID, GOVERNOR_ADDRESS, VAULT_ADDRESS
/// @dev forge test --fork-url $RPC_URL --match-test test_simulateProposalCalls -vvvv
///
///      Every call in the proposal runs through `vault.executeGovernorBatch`, so
///      the vault is `msg.sender` for all of them. There is no second dispatch
///      path and no separate payload to fetch: the execute set and the settle
///      set are the whole committed batch.
contract SimulateProposal is Test {
    function test_simulateProposalCalls() public {
        uint256 proposalId = vm.envUint("PROPOSAL_ID");
        address governor = vm.envAddress("GOVERNOR_ADDRESS");
        address vault = vm.envAddress("VAULT_ADDRESS");

        // Fetch proposal metadata
        ISyndicateGovernor.StrategyProposal memory proposal = ISyndicateGovernor(governor).getProposal(proposalId);

        emit log_named_address("Proposer", proposal.proposer);
        emit log_named_address("Vault", proposal.vault);
        emit log_named_address("Strategy", proposal.strategy);
        emit log_named_string("Metadata URI", proposal.metadataURI);
        emit log_named_uint("Strategy Duration (s)", proposal.strategyDuration);
        // The agent fee is no longer a per-proposal input - it lives on the vault
        // as `agentFeeBps` (vault owner sets it via `vault.setAgentFeeBps`). The
        // governor snapshots the vault's `agentFeeBps` onto the proposal at propose
        // time and uses that snapshot, clamped to `maxPerformanceFeeBps`, at settlement.
        emit log_named_uint("Vault agent fee (bps)", ISyndicateVault(vault).agentFeeBps());
        emit log_named_uint("State", uint256(proposal.state));
        // Coverage the approver cohort has to underwrite before execute. A tier-2
        // (uncertified) call set is priced at full notional.
        emit log_named_uint("Envelope tier", uint256(proposal.envelopeTier));
        emit log_named_uint("Required coverage", proposal.requiredCoverage);
        emit log_named_uint("Max capital", proposal.maxCapital);

        address asset = ISyndicateVault(vault).asset();
        uint256 balanceBefore = IERC20(asset).balanceOf(vault);
        emit log_string("===");
        emit log_named_address("Vault asset", asset);
        emit log_named_uint("Vault balance BEFORE", balanceBefore);

        // Governor batch - execute then settle, as the vault. The legacy
        // `getProposalCalls` concat helper was dropped in V1.5; we now build the
        // unified array off-chain to keep simulation semantics identical
        // (executeCalls run first, then settleCalls).
        ISyndicateGovernor.Call[] memory exec = ISyndicateGovernor(governor).getExecuteCalls(proposalId);
        ISyndicateGovernor.Call[] memory settle = ISyndicateGovernor(governor).getSettlementCalls(proposalId);
        ISyndicateGovernor.Call[] memory calls = new ISyndicateGovernor.Call[](exec.length + settle.length);
        for (uint256 i = 0; i < exec.length; i++) {
            calls[i] = exec[i];
        }
        for (uint256 i = 0; i < settle.length; i++) {
            calls[exec.length + i] = settle[i];
        }

        emit log_named_uint("Number of batch calls", calls.length);

        for (uint256 i = 0; i < calls.length; i++) {
            emit log_string("---");
            emit log_named_uint("Call index", i);
            emit log_named_address("Target", calls[i].target);
            emit log_named_uint("Value", calls[i].value);
            emit log_named_bytes("Data", calls[i].data);

            if (calls[i].data.length >= 4) {
                bytes4 selector;
                assembly {
                    selector := mload(add(mload(add(calls, mul(add(i, 1), 0x20))), 0x60))
                }
                emit log_named_bytes32("Selector", bytes32(selector));
            }
        }

        simulateBatchCalls(vault, calls);

        uint256 balanceAfter = IERC20(asset).balanceOf(vault);
        emit log_string("===");
        emit log_named_uint("Vault balance AFTER", balanceAfter);

        if (balanceAfter >= balanceBefore) {
            emit log_named_uint("Balance INCREASE", balanceAfter - balanceBefore);
        } else {
            emit log_named_uint("Balance DECREASE", balanceBefore - balanceAfter);
        }
    }

    function test_simulateSettlement() public {
        uint256 proposalId = vm.envUint("PROPOSAL_ID");
        address governor = vm.envAddress("GOVERNOR_ADDRESS");
        address vault = vm.envAddress("VAULT_ADDRESS");

        // Get capital snapshot for P&L comparison
        uint256 capitalSnapshot = ISyndicateGovernor(governor).getCapitalSnapshot(proposalId);
        emit log_named_uint("Capital snapshot", capitalSnapshot);

        address asset = ISyndicateVault(vault).asset();
        uint256 currentBalance = IERC20(asset).balanceOf(vault);
        emit log_named_uint("Current balance", currentBalance);

        if (currentBalance >= capitalSnapshot) {
            emit log_named_uint("Profit", currentBalance - capitalSnapshot);
        } else {
            emit log_named_uint("Loss", capitalSnapshot - currentBalance);
        }

        // Settlement simulation: try calling settleProposal
        // This verifies the proposal CAN be settled without reverting
        (bool success,) = governor.call(abi.encodeWithSignature("settleProposal(uint256)", proposalId));
        assertTrue(success, "settleProposal would revert - needs emergency settle");
    }

    function simulateBatchCalls(address vault, ISyndicateGovernor.Call[] memory calls) internal {
        vm.startPrank(vault);
        for (uint256 i = 0; i < calls.length; i++) {
            (bool success, bytes memory ret) = calls[i].target.call{value: calls[i].value}(calls[i].data);
            assertTrue(success, string(abi.encodePacked("Call ", vm.toString(i), " FAILED")));
            emit log_named_uint("Call succeeded", i);
            emit log_named_bytes("Return data", ret);
        }
        vm.stopPrank();
    }
}
