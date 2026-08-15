# ChatGPT Pro engineering task: BTC 5m/15m relative-value v0.7

## Role

Act as the external senior engineer for this repository. Work from the attached
sanitized source bundle only. You cannot assume access to the local checkout,
private repositories, the paper host, AWS, credentials, or any data that is not
inside the bundle. Research the problem deeply, modify the extracted source,
run every test that your environment supports, and return a machine-applicable
delivery. The Codex lead will independently review and test everything; your
output is not accepted merely because you say it works.

## Baseline and current evidence

- Repository: `Lens-less/poly-mm`, Python 3.10+, public-data-only Polymarket
  edge-discovery and execution-replay lab.
- Source baseline: commit `c557160d0c98e195a988f4353bbe19a3b00b3576`
  on `codex/settlement-regime-v06` plus the three untracked review reports named
  in the bundle manifest.
- v0.6 is a frozen prospective experiment. Do not change its preregistration,
  hashes, semantics, action path, evidence directories, or historical outputs.
- Live orders, signing, credentials, authenticated endpoints, proxies, and
  production mutation remain forbidden. `assert_new_orders_disabled()` and all
  public/paper-only guards must remain fail-closed.
- Current v0.6 evidence is development shadow only: 48 settled attempts across
  24 independent expiry clusters, net `+87.523635` USDC, cluster t-stat about
  `1.46`, largest expiry contribution `48.5%`, and qualified PnL `null`. This is
  not evidence of a true edge.
- Windows/Python 3.14 full collection is already blocked at baseline by five
  Linux-only `fcntl` imports. Do not misattribute that baseline issue to your
  patch. The relevant suites and a Linux Python 3.10-3.12 full run remain
  required.

## Confirmed defect

The current settlement regime is Chainlink BTC/USD 60-second TWAP for both the
5-minute and 15-minute markets. A same-expiry pair therefore has one shared
terminal 60-second TWAP at the common close; only the two opening strikes differ.
The current code still models a legacy pair of terminal values:

- `SettlementScenario(twap_30, twap_60)`;
- `JointDistribution.from_scenarios()` classifies 5m using `twap_30` and 15m
  using `twap_60`;
- `simulate_ewma_joint_distribution()` creates 30s and 60s terminal proxies;
- the report builder passes a 60s stream value into the misleading
  `current_twap_30` slot for v0.6.

This can assign probability to structurally impossible outcome combinations
and routinely emits 0/1-like marginals. One-second tick-quantized EWMA variance
also collapses, so the model overstates expected PnL by roughly 3.6x in the
small current sample and its nominal loss probabilities are badly uncalibrated.

## Objective

Design and implement a separate, opt-in v0.7 research/evidence track that:

1. models the 60s/60s same-expiry settlement correctly from one causal BTC path
   and one shared terminal 60-second TWAP;
2. preserves v0.6 byte-for-byte behavior and evidence identity;
3. avoids probability saturation using a defensible, preregistered volatility
   and model-uncertainty design without tuning on the 48 known shadow attempts;
4. supports train-only market-probability shrinkage/calibration and locked OOS
   evaluation without current-event leakage;
5. can replay raw public captures and emit a fully reproducible counterfactual
   report, while failing closed if the required raw histories, book surfaces,
   fee metadata, settlement boundaries, or timestamps are absent;
6. cannot label a result profitable merely because 100 correlated tau rows are
   positive.

## Required design boundaries

- Introduce a new explicit settlement-model/version boundary. Prefer names that
  describe settlement semantics (`shared_terminal_twap_60`) rather than keeping
  misleading 30/60 field names. Keep a compatibility path for frozen v0.5/v0.6.
- For 60s/60s same-expiry markets, both outcomes must be derived from the same
  simulated terminal TWAP compared with `strike_5` and `strike_15`. Enforce the
  resulting structural outcome constraints implied by strike ordering.
- Use only observations available by `decision_at_ms`. Any calibrator,
  shrinkage weight, volatility hyperparameter, or residual distribution must be
  selected on training expiry clusters only, with validation veto-only and a
  locked test split.
- Do not choose a volatility floor, shrinkage lambda, clipping threshold, tau,
  or veto because it improves the 48 known results. If a numeric default is
  necessary, justify it ex ante, expose it in the new preregistration, and add a
  sensitivity grid that is diagnostic rather than action-changing.
- Market probabilities must use executable sides at decision time and must not
  introduce midpoint/current-book/lookahead substitutions.
- Preserve `Decimal` for money and exact evidence values. Floating point is
  acceptable inside Monte Carlo only where the current code already uses it,
  with deterministic seeds and canonical serialized outputs.
- Keep execution replay conservative: fees, full depth walk, 250ms taker delay,
  leg delay, partial fills, unwind, and failed economic attempts remain in PnL.
- Do not lower any existing promotion gate. The repository's 500-attempt v0.6
  promotion policy remains untouched. Add a clearly non-promotional user check
  for at least 100 distinct settled expiry clusters and at least 100 explainable
  locked-OOS economic attempts with positive net PnL. A "true edge" claim must
  additionally require expiry-cluster bootstrap 95% lower bound above zero,
  max single-event contribution at most 20%, complete cost/execution evidence,
  OOS Brier improvement over the executable-market baseline for both horizons,
  ECE at most 0.05, and stable neighboring preregistered settings.
- When fewer than 100 independent settled clusters are available, report
  `insufficient_data`/`null`, never fabricate, resample, duplicate tau rows, or
  call synthetic fixtures a 100-trade validation.

## Implementation scope

Inspect at minimum:

- `src/edge_lab/btc_twap_relative_value.py`
- `src/edge_lab/btc_twap_relative_value_*`
- `scripts/build_btc_twap_relative_value_pilot_report.py`
- `scripts/build_btc_twap_relative_value_validation_summary.py`
- `scripts/build_btc_regime_candidate_scoreboard.py`
- `scripts/run_btc_twap_relative_value_service.py`
- the relevant tests and v0.5/v0.6 frozen research artifacts

Implement the smallest coherent change. Expected pieces include:

- a v0.7 shared-terminal scenario/distribution/model API or an equally clear
  versioned abstraction;
- causal volatility/model-uncertainty handling that does not collapse on
  one-second tick quantization;
- train-only shrinkage/calibration plumbing, with provenance in artifacts;
- a counterfactual replay/evaluation CLI for immutable raw captures;
- cluster-level PnL, Brier, ECE, bootstrap, concentration, sample-count, and
  parameter-neighborhood evidence;
- a new v0.7 strategy specification and preregistration draft whose hashes are
  internally consistent but which does not pretend a deployment occurred;
- tests proving safety, version isolation, causality, structure, deterministic
  replay, null-until-qualified semantics, and the 100-independent-event gate.

If careful inspection shows that a requested implementation would be
statistically invalid or cannot be supported by available data, do not force
it. Implement the safe evidence mechanism and document the exact blocker.

## Required tests

Run and report exact commands and results:

1. New focused unit tests for the v0.7 model and evaluation path.
2. All BTC TWAP/regime/qualification/replay/service/validation tests.
3. Safety/network-guard tests covering no orders, credentials, authenticated
   endpoints, or proxy use.
4. `python -m compileall -q src scripts`.
5. Ruff on every changed Python file (at least E/F/I or the repository's current
   strict rule set).
6. Full `python -m pytest -q` on Linux Python 3.10-3.12 if available.
7. `git diff --check` equivalent on the extracted tree/patch.

Tests using generated fixtures prove invariants only. Label them as such; they
do not prove live profitability or satisfy the 100-event economic gate.

## Deliverables

Return one downloadable ZIP, ideally `poly-mm-v07-pro-delivery.zip`, containing:

- `changes.patch`: unified diff against baseline commit `c557160d...`;
- `DELIVERY_REPORT.md`: design, assumptions, changed files, test commands and
  exact outcomes, security analysis, statistical limitations, and remaining
  risks;
- any new/modified files under their repository-relative paths, so the Codex
  lead can recover if patch application differs;
- `SHA256SUMS.txt` covering every delivered file.

Also summarize the result in the chat and state explicitly whether the available
evidence does or does not satisfy the true-edge and 100-independent-trade gates.

## Forbidden actions and claims

- Do not access or request credentials, cookies, API keys, wallets, private
  repositories, authenticated endpoints, AWS, the paper host, or user data.
- Do not place orders, sign transactions, deploy, alter servers, change cloud
  configuration, migrate databases, create PRs, commit, or push.
- Do not modify frozen v0.5/v0.6 preregistration or reinterpret its shadow PnL
  as qualified evidence.
- Do not weaken tests, safety guards, evidence gates, cost assumptions, or
  null-not-zero semantics to obtain a positive result.
- Do not claim a backtest, paper/shadow replay, synthetic fixture, or 100
  correlated rows is production validation or guaranteed future profit.

## Acceptance criteria for this engineering delivery

The delivery is acceptable only if the patch is complete and applies cleanly;
v0.6 behavior is demonstrably unchanged; the v0.7 shared-terminal model is
structurally correct and causal; all relevant tests pass; safety guards remain
fail-closed; evidence outputs are reproducible and hashable; and the report is
truthful about whether current data reaches the 100-independent-event and
true-edge gates.

