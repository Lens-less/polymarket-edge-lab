# BTC 5m/15m relative-value v0.7 — v7 corrective delivery report

## 1. Delivery identity and scope

This is a complete v7 replacement against sanitized baseline commit
`c557160d0c98e195a988f4353bbe19a3b00b3576`. It is based on the accepted v6
replacement and preserves every v6 remediation except for the two narrowly scoped
v7 corrections described below.

The patch contains exactly:

- 12 v0.7 repository-relative files added by the prior replacements;
- one minimal portability modification to `src/edge_lab/data_store.py`;
- the corresponding regression additions to `tests/test_edge_lab_data_store.py`.

No existing file other than those two portability files is modified. No file is
deleted. The patch does not modify v0.5 or v0.6 assets, recorder/storage behavior
outside the integrity-token helper, network or order guards, dependencies,
requirements, project metadata, or lock files.

`changes.patch` is a unified Git patch against `c557160d...`:

- size: `581737` bytes;
- SHA-256: `be2b319776ec68121c902d83212e0d40fbe732f0eff58619064e62913f80fa17`.

The v7 preregistration draft records:

- `drafted_at`: `2026-08-15T15:37:51Z`;
- revision: `v7_windows_portability_and_trust_boundary_correction`;
- strategy-spec SHA-256:
  `7c384c23e6e8b7224450672ab441803af069997e6c9dd59204cd6e7d4d5e5028`;
- preregistration SHA-256:
  `de79f3e4d43b513a7c71e3196877ee110f04f7d31cec4b3cd060f6184f541bfe`.

The draft remains explicitly later than the already-known v0.6 development-shadow
result of 48 settled attempts across 24 expiry clusters and cannot give those known
results ex-ante qualification.

## 2. P0-22 — fail-closed Windows stat-token portability

### Confirmed failure

The prior integrity cache token accessed every POSIX `os.stat_result` field directly,
including `st_rdev`. Windows Python 3.14.6 does not expose `st_rdev`, so the v0.7
builder's call through `pilot_report._assert_clean_integrity()` raised
`AttributeError` before qualification replay could start.

### Minimal implementation

`_integrity_pair_token()` now returns `bytes | None`.

The required metadata tuple is frozen in the original order:

```text
st_dev, st_ino, st_mode, st_nlink, st_uid, st_gid,
st_rdev, st_size, st_mtime_ns, st_ctime_ns
```

When every field exists, the implementation emits exactly the original token bytes:
lexical path length and path bytes, followed by `lstat` and followed-`stat` metadata,
with each integer encoded as the same signed 16-byte big-endian value. No POSIX field,
ordering rule, path encoding, or digest algorithm changed.

When any required field is absent, the helper returns `None`. It does **not** encode a
zero, sentinel value, partial metadata tuple, platform name, or weaker reusable token.
The existing audit path then:

1. reads and validates the complete manifest;
2. hashes the complete raw content;
3. compares the content hash with the manifest;
4. deliberately refrains from inserting any result into the metadata-token cache.

Every later audit therefore repeats full manifest/content validation on such a
platform. The cache remains enabled on POSIX systems that can produce the complete
original token; this does not restore the previously rejected all-platform permanent
cache disable.

### Regression evidence

Two tests were added:

- `test_integrity_pair_token_preserves_posix_metadata_bytes` independently reconstructs
  the legacy POSIX digest and requires exact byte equality.
- `test_integrity_audit_revalidates_when_stat_token_is_unavailable` wraps real stat
  results with `st_rdev` absent, requires a `None` token, verifies that two consecutive
  audits rehash the raw file twice with an empty cache, and verifies that same-size
  tampering is detected on the next audit.

This closes the reported unguarded `st_rdev` code path without weakening cache identity.

### Windows execution limitation

The delivery environment is Linux x86-64 with CPython 3.13.5. It has no Windows Python
launcher/runtime, Wine/Wine64, QEMU, Docker, or Podman. Consequently, this delivery
**does not claim** that v7 was actually executed on Windows Python 3.14.6.

The exact independent Windows command is recorded in
`test-results/11-windows-validation-status.txt`. On Linux the same file set collects
108 tests: 89 v0.7 focused tests and 19 data-store tests. The executed cross-platform
regressions reproduce the missing-field semantics, but they are not represented as a
substitute for a real Windows run.

## 3. P1-23 — honest builder authority and residual trust boundary

### Supported public API remains fail-closed

`evaluate_locked_oos_evidence()` remains the supported public row evaluator and cannot
grant economic qualification. Caller-created dataclasses, receipt-shaped strings,
hashes, timestamps, `complete_*` booleans, or
`immutable_public_capture_evidence=True` remain diagnostic inputs only. A profitable
100-row synthetic input still returns:

```text
status = counterfactual_insufficient
positive_net_pnl_user_check_passed = false
true_edge_gate_satisfied = false
qualified_net_pnl = null
```

The internal row evaluator no longer accepts any builder verification digest,
verification chain, capability, or authority parameter. It is diagnostic-only.

### Removed generic issuer surfaces

The evaluator no longer distributes the generic private interfaces used by the review
reproduction:

- `_issue_verified_prelabel_lock_provenance`;
- `_issue_builder_verified_evidence`;
- `_evaluate_builder_verified_locked_oos_evidence`.

The builder also no longer distributes the prior generic
`_builder_verified_evidence(contexts, caller rows, caller chain, ...)` helper. Tests
assert that these names are absent and that the row evaluator has no authority-bearing
parameter.

### Builder-authoritative path

The supported qualification entry point is the high-level
`build_counterfactual_report()` builder. In the unmodified supported flow it:

1. loads the manifest and strict public/paper-only capture configuration;
2. rejects generated fixtures for economic authority;
3. invokes full capture/predictor integrity validation;
4. reads and validates the pre-expiry test-universe and forecast/decision journal;
5. verifies exact forecast and decision payload hashes and pre-expiry receipt timing;
6. performs receipt-timed replay and complete action reconciliation;
7. derives capture/config, journal, cycle/reconciliation, row-set, partition-count,
   structural-floor, and neighborhood digests from those loaded artifacts;
8. only then attaches builder-authoritative qualification diagnostics to the report.

No supported function accepts the former general shape “caller rows plus caller chain
become authority.” The private builder implementation helper consumes the builder's
loaded contexts, cycles, journal index, and preregistration identity rather than an
arbitrary caller verification mapping.

### Exact trust model; no security theatre

This mechanism is an **API misuse guard**, not a cryptographic or adversarial
provenance system. The repository contains no external signature, hardware-backed key,
transparency-log inclusion proof, independent third-party timestamp, or independently
controlled receipt verifier.

An actor with arbitrary local Python execution or source-modification authority can
still import or call private implementation details, synthesize internal objects,
monkeypatch validation, replace functions, or alter control flow to manufacture an
authority-shaped Python result. That is the explicit residual threat. The v7 code,
strategy specification, preregistration, serialized report, and tests all state that
this threat is outside the in-process API-misuse boundary and is **not claimed closed**.

The old exact private-helper reproduction is no longer available through those helper
names or through an authority parameter on the row evaluator. An equivalent attacker
with unrestricted local execution is nevertheless not cryptographically prevented; an
adversarial provenance claim would require an external independently verifiable
receipt/signature or third-party timestamp beyond this bundle.

Synthetic gate mathematics continues to use the dedicated non-economic diagnostic
result type. It can show which numerical predicates would pass, but it permanently
returns `true_edge_gate_satisfied=false` and `qualified_net_pnl=null`.

## 4. Preserved v6 economic and statistical behavior

All prior review remediations remain present, including:

- path-count-invariant model uncertainty;
- coherent depth- and fee-aware executable market baseline;
- capture-derived common-expiry clustering;
- strict capture configuration;
- pre-expiry immutable forecast receipt timing and receipt-delayed replay;
- corrected concentration denominator;
- retained no-fill reconciliation;
- structural-before-predictive decision order;
- breakpoint-optimized structural and predictive sizing;
- Monte-Carlo-independent structural primitives;
- separate predictive and structural true-edge predicates;
- synthetic/mechanism diagnostics that cannot emit economic qualification;
- `null`-until-qualified evidence semantics.

No v0.5/v0.6 behavior or evidence asset was modified.

## 5. Verification executed in this delivery environment

Environment:

```text
Linux 6.18.35 x86-64
CPython 3.13.5
pytest 9.0.2
Ruff 0.16.3
```

The unknown `pytest` configuration option `timeout` produced one non-failing warning in
each pytest invocation because `pytest-timeout` is not installed.

| Verification | Exact result |
|---|---:|
| v0.7 focused model/replay/evaluation/builder/assets | **89 passed**, 1 warning |
| Complete `tests/test_edge_lab_data_store.py` | **19 passed**, 1 warning |
| New P0-22/P1-23 targeted regressions | **8 passed**, 1 warning |
| BTC integration/compatibility suite | **288 passed**, 1 warning |
| Safety/network/recorder suite | **76 passed**, 1 warning |
| `python -m compileall -q src scripts` | exit 0 |
| Ruff `check --select E,F,I` on 11 changed Python files | All checks passed |
| `git diff --check` | exit 0 |
| `git apply --check --whitespace=error-all` | exit 0 |
| Actual patch application | exit 0 |
| Patch-applied content versus delivery files | **14/14 SHA-256 matches** |
| Existing baseline files modified/deleted | **2 / 0** |
| Added repository-relative files | **12** |

The integration suite preserves all 285 v6 cases and includes the three new focused
trust-boundary tests. The safety suite remains 76 tests because recorder/network/order
code was not changed.

### Commands

Focused:

```text
python -m pytest -q \
  tests/test_btc_twap_relative_value_v07_model.py \
  tests/test_btc_twap_relative_value_v07_replay.py \
  tests/test_btc_twap_relative_value_v07_evaluation.py \
  tests/test_btc_twap_relative_value_v07_counterfactual.py \
  tests/test_btc_twap_relative_value_v07_assets.py
```

Portability:

```text
python -m pytest -q tests/test_edge_lab_data_store.py
```

Integration:

```text
python -m pytest -q \
  tests/test_edge_lab_btc_twap_relative_value.py \
  tests/test_btc_twap_relative_value_oos_metrics.py \
  tests/test_btc_twap_relative_value_qualification_runtime.py \
  tests/test_btc_twap_relative_value_replay.py \
  tests/test_btc_twap_relative_value_service.py \
  tests/test_btc_twap_relative_value_service_runtime.py \
  tests/test_btc_twap_relative_value_walk_forward_runtime.py \
  tests/test_btc_twap_relative_value_pilot_report.py \
  tests/test_btc_twap_relative_value_pilot_v2.py \
  tests/test_btc_twap_relative_value_validation_summary.py \
  tests/test_btc_regime_candidate_scoreboard.py \
  tests/test_btc_twap_settlement_regime.py \
  tests/test_btc_twap_relative_value_v07_model.py \
  tests/test_btc_twap_relative_value_v07_replay.py \
  tests/test_btc_twap_relative_value_v07_evaluation.py \
  tests/test_btc_twap_relative_value_v07_counterfactual.py \
  tests/test_btc_twap_relative_value_v07_assets.py \
  tests/test_edge_lab_execution_replay_freeze.py \
  tests/test_edge_lab_execution_replay_promotion.py \
  tests/test_edge_lab_network_safety.py
```

Safety:

```text
python -m pytest -q \
  tests/test_edge_lab_network_safety.py \
  tests/test_edge_lab_recorder.py \
  tests/test_edge_lab_recorder_cancellation_regression.py
```

The requested real Windows run is not included among successful results; its exact
command and the missing runtime inventory are preserved in the test logs.

## 6. Patch and frozen-boundary verification

The patch applies cleanly to the sanitized baseline with strict whitespace checking.
After actual application, all 14 delivered repository-relative files match the
packaged copies byte-for-byte by SHA-256.

Baseline comparison found 241 existing regular files. Exactly two differ after patch
application:

```text
src/edge_lab/data_store.py
tests/test_edge_lab_data_store.py
```

No existing file is deleted. The 12 v0.7 files are additions. Therefore v0.5/v0.6,
recorder/network/order safety code, dependencies, and lock files retain their baseline
identity.

## 7. Current evidence verdict

No real immutable v0.7 capture/journal dataset establishing 100 distinct locked common
expiries and 100 qualified economic attempts is supplied in this delivery. Generated
fixtures and synthetic mechanism diagnostics prove implementation invariants only.

The current economic result remains:

```text
evidence_status = counterfactual_insufficient
predictive_true_edge_gate_satisfied = false
structural_true_edge_gate_satisfied = false
true_edge_gate_satisfied = false
positive_net_pnl_user_check_passed = false
qualified_net_pnl = null
predictive_qualified_net_pnl = null
structural_qualified_net_pnl = null
```

This delivery does not claim a real predictive edge, a real structural edge, 100
qualified profitable trades, production validation, deployment readiness, or future
profitability.
