# BTC 5m/15m relative-value v0.7 counterfactual research specification

## Status and scope

This is an **opt-in, paper-only counterfactual research track**. It is a draft preregistration, not evidence of deployment, trading, profitability, or production readiness. The frozen v0.5 and v0.6 tracks, hashes, evidence directories, action paths, qualification rules, dependencies, lock files, safety guards, and promotion policy remain unchanged.

This v6 draft was generated at `2026-08-15T13:21:59Z`, after the v0.6 development-shadow result of 48 settled attempts across 24 common expiries and net `+87.523635` USDC was already known. It therefore supplies no ex-ante or preregistered qualification for those 48 known attempts, does not retest them as locked OOS evidence, and must not be cited as though it predated their outcomes.

The eligible universe is one same-expiry BTC pair: the final 5-minute market nested inside one 15-minute market, with both public rules bound to the current Chainlink BTC/USD 60-second TWAP settlement regime.

## Settlement semantics

The settlement model is `shared_terminal_twap_60.v1`; the probability model is `btc_5m_15m_shared_terminal_twap_60.v07`.

There is exactly one terminal Chainlink BTC/USD 60-second TWAP at the common close:

- 5m Up iff `terminal_twap_60 >= strike_5`;
- 15m Up iff `terminal_twap_60 >= strike_15`.

The model never constructs independent terminal values. Strike ordering fixes the feasible joint cells. If `strike_5 < strike_15`, `5down_15up` is impossible and `q_5_up >= q_15_up`. If `strike_5 > strike_15`, `5up_15down` is impossible and `q_15_up >= q_5_up`. Equal strikes require equal outcomes and marginals.

A Jeffreys pseudo-count of `0.5` is applied only to feasible cells. Structurally impossible cells remain exactly zero.

## Causal predictor and preregistered model uncertainty

Only observations whose source and corrected receipt timestamps are at or before `decision_at_ms` may enter the model. Predictor history must be a contiguous one-second BTC series with at least 300 observations; the last observation may be at most five seconds stale.

The frozen model uses:

- annualized RMS log-return estimates from non-overlapping 15-second and 60-second returns;
- annualized volatility equal to the maximum of those estimates and a `0.25` floor;
- standardized Student-t innovations with 5 degrees of freedom;
- fixed volatility scales `0.75`, `1.0`, and `1.5`, with mixture weights `0.2`, `0.6`, and `0.2`;
- deterministic stratified path allocation across those scales;
- 8,192 total paths, base seed 712, and common random numbers across fixed scales and neighboring volatility settings;
- one one-second BTC path and one terminal 60-second average per path;
- the causal Chainlink-minus-local 60-second basis carried to the terminal proxy;
- no empirical oracle-residual distribution.

The qualification penalty is **not** Monte Carlo integration error and contains no `1 / sqrt(n_paths)` term. For each action, the model computes the net expected value under every fixed volatility-scale conditional distribution after applying the same market shrinkage and structural projection. The preregistered downside is:

`max(0, mixture_net_ev - minimum_fixed_scale_net_ev)`.

The uncertainty-adjusted edge is:

`mixture_net_ev - uncertainty_multiplier * model_scale_downside`.

Changing the numerical integration path count may improve convergence, but it cannot mechanically relax qualification through a shrinking standard-error denominator.

## Binding executable-market baseline and shrinkage

The binding market baseline uses a frozen target quantity of 5 outcome shares for each binary market. For Up and Down separately, the builder walks all decision-time ask levels needed for that quantity and adds the captured dynamic taker fee at each fill level. It then normalizes the two all-in average costs:

`all_in_up_cost_per_share / (all_in_up_cost_per_share + all_in_down_cost_per_share)`.

Insufficient depth at the frozen target quantity makes the binding baseline unavailable and the case fails closed. Top-of-book ask normalization is retained only as a non-binding diagnostic. Midpoints, post-decision books, settlement books, and current-book substitutions are forbidden.

For each same-expiry pair, the two executable-market marginals are projected into the same shared-terminal feasible domain used by the forecast. This coherent projected pair is the binding Brier baseline. A model weight of zero therefore produces exactly the coherent market baseline and cannot satisfy a strict Brier-improvement gate.

A single model weight is selected from `{0, 0.25, 0.5, 0.75, 1}` on training common-expiry clusters only. Selection minimizes common-expiry-equal mean two-horizon Brier score; ties choose the lower weight. The blend is projected to the feasible shared-terminal domain. Validation is veto-only and passes only if the selected forecast has **strictly lower** Brier score than the coherent executable-market baseline for both horizons.

## Canonical pair integrity, common-expiry cluster, and split policy

The track uses two different canonical identifiers, with non-interchangeable roles.

The **pair-integrity key** is derived by canonical SHA-256 from the captured common expiry timestamp plus the 5m and 15m market IDs and condition IDs. It has prefix `v07-expiry-pair-sha256:`. It proves which two captured contracts form the pair, but it is not an independent statistical observation and never increases any sample count.

The **independent statistical cluster key** is derived separately by canonical SHA-256 from the captured common `expiry_ms` only. It has prefix `v07-common-expiry-sha256:`. Forecast identity, attempt reconciliation, train/validation/test separation, Brier, ECE, bootstrap, concentration, and the 100-cluster gate all use this common-expiry key. Pair hashes and manifest aliases never count as additional clusters.

A manifest `event_cluster_id` is only a human-readable alias. Every case must declare the exact derived pair-integrity key, but a manifest is forbidden from supplying `expiry_ms`; the builder obtains the common expiry from both immutable captured market targets and verifies that they agree.

This strategy permits exactly one 5m/15m pair, one manifest case, one alias, and one split per common expiry. The builder rejects the same captured expiry under different pair identities, aliases, cases, or splits; it also rejects one pair mapped to multiple expiries, one alias mapped to multiple expiries, and duplicate common-expiry/tau rows. Renaming 100 pairs at one expiry therefore remains one dependent label and is rejected rather than counted as 100 observations.

Splits are chronological and common-expiry-disjoint. Every training label must predate fitting; every validation label must predate veto evaluation; training labels must predate validation decisions; validation labels must predate test decisions. Label availability is the maximum of expiry, the corrected receipt of the exact closing TWAP, and the corrected receipts of both official resolutions.
## Auditable pre-label lock provenance

An immutable capture proves replay inputs did not change; it does not by itself prove that the test universe, forecast, or action was fixed before the label. Qualification therefore requires a separate immutable `v07_prelabel_lock` journal.

The journal must contain:

1. exactly one test-universe receipt under journal schema `btc-5m-15m-v07-lock-journal.v4`, whose canonical hash covers the preregistration hash, each captured `expiry_ms`, each derived common-expiry key, each pair-integrity key, and every decision tau, with both lock and receipt strictly before the earliest test decision;
2. exactly one forecast/decision receipt for every common-expiry/tau, binding the test-universe hash, captured `expiry_ms`, derived common-expiry key, pair-integrity key, `decision_at_ms`, canonical forecast-payload hash, and canonical decision-payload hash;
3. `prediction_locked_at_ms == decision_at_ms`, receipt time not earlier than the lock, and the immutable receipt strictly before the captured common `expiry_ms`. The receipt timestamp, not a backdated lock field, is the forecast’s actual availability time.

Receipts at or after common expiry, retrospectively selected test-universe receipts, hash mismatches, duplicate receipts, or missing claimed receipts fail closed. Being earlier than a later official-resolution receipt is insufficient, and a backdated `prediction_locked_at_ms` can never replace the immutable receipt clock. A replay without this journal is explicitly `counterfactual_insufficient`; it may produce diagnostics and counterfactual PnL but can never satisfy `true_edge_gate_satisfied` or produce non-null `qualified_net_pnl`.

## Builder-issued qualification authority

The public evaluator is deliberately non-authoritative. Caller-created dataclasses, receipt-shaped hashes or timestamps, and self-reported `complete_*` or `immutable_public_capture_evidence` booleans are descriptive inputs only. The public entry point always remains `counterfactual_insufficient` and always emits `qualified_net_pnl: null`, even when 100 caller-created rows are profitable and appear to carry verified receipts.

Economic qualification requires an opaque, builder-issued capability that is unavailable from the public evaluator API. The builder may issue it only after it has actually verified all of the following in the same run:

- exact strict capture configuration and `generated_fixture: false` for every test context;
- immutable capture and predictor tree identity;
- the pre-label test-universe and per-decision lock journal;
- one replay cycle per locked test context and one reconciliation row per actionable decision;
- canonical row-set hashes for forecasts and reconciliations;
- the exact diagnostic parameter-neighborhood document.

The capability binds the preregistration hash, test-universe hash, forecast-row hash, reconciliation-row hash, neighborhood hash, capture/config verification hash, journal identity hash, cycle/reconciliation hash, predictive/structural/non-edge forecast counts, predictive/structural reconciliation counts, and the count of structural reconciliations with executable positive floors. Any changed row or neighborhood invalidates it. Missing journal evidence, any generated fixture, missing reconciliation, or a public-evaluator call cannot receive qualification authority.

Synthetic gate arithmetic is exercised only through a dedicated non-economic mechanism diagnostic. That diagnostic can show which mathematical thresholds would be met, but its result type is permanently `non_economic_mechanism_diagnostic`, `true_edge_gate_satisfied` is false, and qualified PnL is null. No generated or synthetic test invokes the economic evaluator with fabricated builder authority or emits a true-edge economic result object.

## Forecast availability and paper-replay timing

For a builder-verified primary forecast, `forecast_available_at_ms` is exactly the immutable forecast/decision journal receipt’s corrected `received_at_ms`. It must be strictly earlier than common expiry and is serialized in the forecast payload, decision payload, forecast row, decision, replay cycle, reconciliation row, and report. The canonical forecast and decision hashes in that receipt bind this availability time to the exact payloads.

The first executable book surface is eligible only at or after:

`forecast_available_at_ms + captured_taker_delay_ms`.

The captured taker delay remains 250ms. Any book or fill before that eligibility time is excluded. Second-leg eligibility, timeout, and unwind continue to be measured from the actual first-leg surface selected by replay, not from a backdated decision clock. The report records the effective signal-to-execution latency from original decision time to the first observed executable surface.

An unlocked counterfactual has no immutable computation receipt. It therefore uses the frozen, preregistered conservative computation-availability delay of 5,000ms after `decision_at_ms`, followed by the same captured 250ms taker delay. This is a paper-replay timing assumption and diagnostic only. It is not a measurement of production hardware, server runtime, network latency, or deployability, and it cannot support an economic attempt or qualified PnL without the real pre-expiry lock journal and builder verification chain. No local benchmark is treated as production validation.

## Causal metadata and immutable capture identity

Each case identifies one immutable capture tree, one immutable predictor tree, and `capture_root/capture-config.json`. The report records capture-config and tree hashes. A capture config that postdates the earliest decision is invalid.

Capture configuration is fail-closed. `schema_version` must be explicitly present and equal exactly `btc-5m-15m-relative-value-capture.v1`; `paper_only`, `public_only`, and `new_orders_disabled` must each be explicitly present and the JSON boolean `true`; and `generated_fixture` must be explicitly present and a JSON boolean. Missing fields, unknown schemas, wrong types, or any false safety flag abort the case. Credential and proxy scanning remains mandatory. Only an explicit `generated_fixture: false` may contribute immutable public-capture evidence; `true` remains a generated invariant fixture and cannot support economic qualification.

Public rule and fee metadata must come from captured public snapshots received by the earliest decision. The builder reconstructs canonical contract identities and rule hashes instead of trusting asserted hashes. Conflicting snapshots, late-only metadata, mismatched fees, or mismatched rule identity abort the case.

Official resolutions must identify the expected condition, winning token, and outcome, must not be received before expiry, and must agree with the exact shared-terminal boundary. Both horizons are required.

## Decision edge basis and execution reconciliation

Decision readiness is split into common safety/causality checks and predictive-only readiness. After common safety passes, the strategy evaluates structural candidates first. The structural primitive is derived directly from the verified shared-terminal settlement rule, the captured `strike_5`/`strike_15` ordering, causal decision-time books, and captured dynamic fees. The structural primitive uses no Monte Carlo scenario and does not require forecast probabilities, predictor history, shrinkage, Brier improvement, or validation-veto availability. Only when no executable positive structural candidate exists does the predictive path require simulation, train-only shrinkage, and the validation veto.

For each action, the structural primitive enumerates every payoff state feasible under the captured strike ordering and computes the minimum two-leg payoff. Quantity is an optimization variable; the 25 USDC pair-risk value is an upper bound, not a target to consume. Deterministic quantity candidates are the joint minimum order, every cumulative ask-depth breakpoint from either leg that remains jointly executable, and the exact six-decimal fee-inclusive risk-budget boundary. Every candidate walks full ask depth on both legs and applies the captured dynamic taker fee with the execution schedule's per-fill-level rounding.

`structural_net_floor_per_pair` is:

`minimum_feasible_shared_terminal_payoff - full_depth_dynamic_fee_all_in_cost_per_pair`.

For structural sizing, eligible candidates require both legs to fill the same quantity, total cost within the risk bound, and strictly positive floor. The primary objective is to maximize:

`selected_guaranteed_total_pnl = quantity * structural_net_floor_per_pair`.

Ties choose the higher per-pair structural floor, then the lower quantity, then the lexicographically lower action ID. Increasing the risk ceiling therefore cannot remove a smaller already feasible positive-floor quantity. For predictive sizing, eligible candidates require per-pair uncertainty-adjusted edge strictly above `0.015` and positive total adjusted PnL. The objective is to maximize `quantity * uncertainty_adjusted_pnl_per_pair`; ties choose the higher per-pair adjusted edge, then the lower quantity, then action ID. Predictive sizing cannot turn a small positive edge into a no-trade merely by consuming deeper negative-edge liquidity.

The basis is `structural` only when the selected quantity has a strictly positive fee-inclusive floor. Missing or failed predictive validation cannot erase that paper action. Insufficient depth, stale or future books, missing legs, unsafe rule/fee state, or a non-positive floor still fail closed. Structural forecast probabilities, market probabilities, Brier, and ECE are explicitly `null`/non-applicable rather than fabricated to satisfy predictive types. All non-structural positive candidates remain `predictive` and use the preregistered forecast, model-scale downside, shrinkage, and veto.

The decision, replay cycle, forecast row, reconciliation row, setting summary, and final report record `edge_basis`, selected quantity, feasible-state worst payoff, fee-inclusive structural floor, quantity executability, `quantity_selection_basis`, candidate-breakpoint count, selected guaranteed total PnL, and selected uncertainty-adjusted total PnL. Structural and predictive PnL are reported separately. A structural floor cannot be relabeled as predictive model edge.

The primary tau window is 45 to 240 seconds. Conservative replay retains full depth walking, dynamic taker fees, exact 250ms taker delay, 750ms maximum leg delay, partial fills, timeout, first-leg unwind, and hold-to-settlement accounting. Pair risk is 25 USDC, minimum qualification edge is 0.015 per pair, initial paper cash is 10,000 USDC, and the binding market-baseline quantity is 5 shares.

Every locked actionable decision must appear exactly once in the reconciliation ledger as one of:

- a settled fill, partial fill, unwind, or failed-unhedged execution with realized net PnL;
- a causally demonstrated `NO_FILL` with exactly zero PnL;
- an invalid evaluation window when a required causal execution or settlement surface is absent.

A causal no-fill is retained but does not count as an economic attempt. A no-trade forecast remains in the decision/forecast record but does not create an economic attempt. Silent dropping of an actionable decision is forbidden.

Every v0.7 decision remains `development_shadow` in the legacy carrier and `v07_locked_oos_counterfactual` in this track. It can never populate v0.5/v0.6 qualified evidence.

## Evidence unit and gates

Tau rows are not independent, and different market or condition IDs at the same close do not create new terminal labels. The capture-derived common-expiry key is the sole counting, Brier/ECE weighting, bootstrap, and concentration unit. The pair-integrity hash is diagnostic integrity metadata only. A settled common-expiry cluster for a track's 100-cluster gate has at least one explainable economic attempt of that same `edge_basis`; no-trade and causal no-fill rows cannot pad the attempt count.

The raw mathematical diagnostic `diagnostic_positive_sample_pnl` reports only whether sample-count and arithmetic-positive-PnL conditions are met; it is explicitly non-qualifying. The qualified-semantics field `positive_net_pnl_user_check_passed` requires a complete real builder-verified predictive or structural gate. Synthetic, generated, public-evaluator, or self-reported rows therefore leave that field false even when their arithmetic sample PnL is positive.

v6 implements an independent fail-closed structural true-edge gate alongside the predictive gate. Predictive and structural evidence use two independent sample gates. Each track separately requires at least 100 distinct captured common expiries, at least 100 explainable locked-OOS economic attempts of that same `edge_basis`, and positive realized track net PnL. A mixed sample of 50 predictive and 50 structural attempts satisfies neither track. Causal no-fills remain in track PnL and cluster bootstrap as zero, but do not count as economic attempts.

Both true-edge tracks require the same real evidence spine:

- a real builder-issued capability bound to strict immutable public captures with `generated_fixture: false`;
- verified test-universe and per-decision receipts whose forecast availability is strictly before common expiry;
- receipt-timed replay with no pre-receipt surface, complete depth/fee evidence, partial-fill/timeout/unwind handling, official settlement evidence, and one-to-one reconciliation;
- positive realized track net PnL;
- a 5,000-resample common-expiry-cluster bootstrap, seed 712, whose lower 5% mean-PnL bound exceeds zero;
- binding concentration `largest_positive_cluster_net_pnl / total_net_pnl <= 0.20`; non-positive total PnL makes the metric unavailable and fails the gate;
- the two non-binding diagnostics `largest_positive / total_positive` and `largest_absolute / total_absolute`.

The predictive track additionally requires common-expiry-equal Brier strictly better than the coherent executable-market baseline for both horizons, common-expiry-equal 10-bin ECE at most 0.05 for both horizons, and positive PnL under every available preregistered neighboring setting. Structural rows are excluded from all predictive forecast metrics and predictive sample counts.

The structural track does not require Brier, ECE, model-neighborhood stability, Monte Carlo, or predictor history. It instead requires every structural reconciliation row to bind the exact decision-time selected quantity, both-leg executability, a strictly positive `structural_net_floor_per_pair`, the structural quantity-selection objective, and the matching positive guaranteed total. Theoretical floor is not realized evidence: at least 100 actual explainable structural economic attempts at 100 common expiries, complete execution/settlement reconciliation, positive realized PnL, bootstrap, and concentration must all pass.

`predictive_true_edge_gate_satisfied` and `structural_true_edge_gate_satisfied` are serialized separately, with separate qualified PnL values. Top-level `true_edge_gate_satisfied` is the logical OR of the two complete track gates; top-level qualified PnL is non-null only for tracks that actually pass. `positive_net_pnl_user_check_passed` shares this qualified semantics and cannot be satisfied by raw arithmetic alone.

Without the builder-issued verification chain, status is `counterfactual_insufficient`, `positive_net_pnl_user_check_passed` is false, both true-edge flags are false, and all qualified PnL fields are `null`, regardless of caller-provided receipts, evidence booleans, retrospective sample size, theoretical floors, or PnL. Generated fixtures and mechanism diagnostics prove implementation arithmetic only and can never emit an economic predictive or structural true-edge result.

## Diagnostic sensitivity grid

Sensitivity settings never select or modify primary actions. The builder reruns immutable data under volatility-floor offsets `-0.05` and `+0.05` when in range, and model-weight offsets `-0.25` and `+0.25` when in `[0, 1]`. Out-of-range neighbors are omitted. Volatility settings refit shrinkage on training only; model-weight settings are traceable diagnostic overrides. Sensitivity forecasts are counterfactual diagnostics and do not inherit primary pre-label lock claims.

## Safety boundary

The track uses public captured data only. Live orders, signing, wallets, credentials, cookies, authenticated endpoints, ambient proxy routing, production mutation, cloud changes, deployment, commits, pushes, and pull requests are forbidden. The builder confirms `assert_new_orders_disabled()` fails closed and performs no network request.
