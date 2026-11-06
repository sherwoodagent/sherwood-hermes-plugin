# Management plan for a live proposal

Do this before every `sherwood strategy propose`, for any strategy type. The user should know how the position will be run before it executes, not after.

## 1. Research the strategy's own mechanics

Read the strategy contract (sherwood-protocol `src/strategies/`, or the template source for a cloned or hand-written strategy) and its docs page. Write down, with the file and line you got each from:

- **What can change mid-proposal.** Which functions the proposer can call between execute and settle, what they do, and what is frozen. Some strategies only restore their original setup; some allow nothing.
- **Risk limits and how they are enforced.** Which limits are checked continuously, which only at settle, and which are not enforced at all. A limit checked only at settle is a floor that can block settle, not a stop-loss.
- **Settle.** Who can settle and when. On the governor today, the proposer can settle 1 hour after execute (`MIN_STRATEGY_DURATION_BEFORE_SELF_SETTLE`), and anyone can settle once the duration has passed.
- **Expiry.** Nothing settles by itself when the duration ends. The position stays open until someone calls settle.
- **Conditions that block a transaction.** For example price-feed age limits, market hours, cooldowns or slippage floors.

Use only facts you can verify from the contract, the docs or on-chain reads. Mark anything else as an assumption.

## 2. Check event risk in the window

List what falls between execute and the planned settle: earnings, macro releases, token unlocks, protocol upgrades, market closures and holidays. Check each date at its source and cite it. If you can't confirm a date, say so.

## 3. Propose the plan to the user

Keep it short. It should cover:

- **Triggers:** what you watch and the level at which you would act (or recommend acting).
- **Risk levels:** a warn level, a recommend-exit level and an exit-today level. Set them well inside any limit that can block settle, and show the math.
- **Settle timing:** a target window, chosen to avoid the events above and any period when settle can't go through.
- **Check cadence:** how often you check, and when. Checks are read-only.

Share the plan with the user in chat and adjust it until they agree. **Keep the full plan private.** Save it as a local file you keep (for example `proposals/<id>/plan.md` in your workspace) and don't pin, attest or post it. The thesis passed with `--attest-thesis` and the `--description` are public. They get only a short summary with no exact risk levels, trigger prices or settle times, for example: "Management plan agreed: periodic rebalance checks, early-settle rules, target settle before <event>." The proposal metadata schema (`sherwood/proposal/v1`) has no plan field.

**Never rebalance, adjust or settle without the user's approval for that action.** The plan says when you will ask, not that you may act.

## Example (illustrative only, one portfolio case)

These numbers are for one portfolio proposal only. Work out your own from your strategy's mechanics.

A 60 USDG META/MSFT/GME 40/30/30 Portfolio in a 90 USDG vault, maxDrawdown 15%:

- `rebalanceDelta()` only restores the weights set at init. Changing weights reverts `WeightsFrozen`/`RoutesFrozen`. Feeds older than 26 h revert, so nothing works from about Sat 18:40 UTC to Mon 00:00 UTC (observed; `sources get calendar` has the live window, [why](https://docs.sherwood.sh/explanations/oracle-staleness)).
- maxDrawdown is checked on the whole vault's price per share at settle ([why](https://docs.sherwood.sh/explanations/settlement-and-drawdown)). Floor: 0.85 × 90 = 76.5 USDG. The 30 USDG idle in the vault counts, so the basket must return at least 46.5 USDG, a −22.5% basket loss. Below that, a normal settle reverts.
- Plan: rebalance only if a leg drifts more than 5 points. Warn at −5%, recommend settling at −8%, settle the same day at −12% (well above −22.5%). META reports inside the window, so settle that day before the report. Check twice on weekdays.
