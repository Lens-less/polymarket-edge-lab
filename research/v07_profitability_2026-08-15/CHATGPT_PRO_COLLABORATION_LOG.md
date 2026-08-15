# ChatGPT Pro collaboration log

Recorded: 2026-08-16 (Asia/Shanghai)

## Shared source bundle

- Baseline commit: `c557160d0c98e195a988f4353bbe19a3b00b3576`.
- Archive: `poly-mm-chatgpt-pro-c557160-20260815T0313Z.zip`.
- Size: `1,286,032` bytes.
- SHA-256: `3fdcf7eef59f811cf7935c6953ea3b1bd822967bddbd298e72336d458c2708c0`.
- Scope/scan manifest:
  `research/v07_profitability_2026-08-15/SOURCE_BUNDLE_MANIFEST.json`.
- The ZIP contained 242 entries. It excluded Git metadata, environments,
  dependencies, caches, build output, databases, runtime/browser state, and
  credential-like files. Targeted secret-pattern scanning found no matches.
  `gitleaks` and `trufflehog` were not installed, so they were not claimed.
- The user explicitly authorized uploading and sending this bundle to the
  signed-in ChatGPT Pro conversation.

## Conversations

1. Implementation and correction loop:
   https://chatgpt.com/c/6a7fe211-89a8-83ec-88dc-083193386308
2. Independent quantitative edge audit:
   https://chatgpt.com/c/6a7fe366-7ce4-83ec-9afa-f193454d6eb7

The external model had no assumed access to the local checkout, private
repository, server, credentials, or runtime. Both conversations received the
sanitized bundle and explicit engineering briefs.

## Independent edge audit

The accepted audit delivery is
`poly-mm-edge-audit-delivery-v3-adb66bea.zip`:

- size: `34,668` bytes;
- outer SHA-256:
  `adb66bea12e7128b761eed2c7ef7f515103c8aedf51444b545f1bf1ada6fa9f3`;
- all five payload hashes, path/symlink checks, patch checks, nine focused
  tests, Ruff E/F/I, and `git diff --check` passed independently.

Corrections required before accepting audit v3:

- v1 let callers lower the 100-cluster/100-attempt floors, loosen
  concentration/ECE, and reduce bootstrap work; one synthetic cluster could
  then return `validated`;
- v1 also lacked credible forecast-lock/label-availability provenance;
- v2 fixed the fail-open gates but still failed the required Ruff E/F/I gate
  with 16 E501 findings;
- v3 was the formatting-only accepted audit.

The accepted quantitative conclusion remains no-go:

- 48 development-shadow attempts over 24 expiry clusters;
- net PnL `+87.523635` USDC;
- cluster t-statistic `1.459910073`;
- one-sided cluster-bootstrap fifth percentile
  `-0.153821583` USDC/cluster;
- bootstrap probability of positive mean `0.94168`;
- largest positive cluster / net PnL `48.5237%`;
- model Brier worse than executable-market diagnostic on both horizons.

Those observations do not prove a real edge or satisfy the requested
100-independent-expiry profitability gate.

## Implementation correction history

ChatGPT Pro deliveries were advisory. Codex verified each archive and returned
reproducible defects rather than treating a delivery statement as proof.

| Round | Delivery identity | Main rejection or result |
|---|---|---|
| v1 | `poly-mm-v07-pro-delivery.zip`, SHA-256 `ad36b46c7e826750347b9fe4067ed6edd1e87634acb7591cce12df45ea13d98b` | Rejected: path-count-dependent uncertainty, incoherent market baseline, renameable expiry clusters, missing pre-label lock provenance, wrong concentration denominator, and dropped causal no-fills. |
| v2 | replacement | Rejected: fail-open capture configuration and clustering by renamed pair identity instead of common expiry. |
| v3 | outer SHA-256 prefix `593de35a`; patch SHA-256 prefix `136d3593` | Substantial remediation accepted for further review, but later sizing/structure review remained open. |
| v4 | outer SHA-256 prefix `6ce66ed3` | Rejected: receipt/execution timing, structural opportunities blocked by predictive veto, and synthetic qualification. |
| v5 | outer SHA-256 prefix `786fb341` | Rejected: risk-budget sizing could erase an existing edge, structural logic remained Monte-Carlo-dependent, and structural true-edge status was hard-coded false. |
| v6 | outer SHA-256 `f178bb3e436fe2ab8fc2dee1a3b2cf26b56d69a624c69d8d65e57aec47b854d4` | Economic sizing and separate structural/predictive gates passed, but final review found Windows `st_rdev` failure and an overstated in-process authority boundary. |
| v6 report correction | `DELIVERY_REPORT_v6-corrected-e3bce27e.md`, SHA-256 `e3bce27ede50f03f2fe991613942e4f65d999da0a5024588acdbd93e7c189356` | Report-only correction of the inconsistent preregistration timestamp; v6 source ZIP unchanged. |
| v7 | `poly-mm-v07-pro-replacement-v7-dbb904a5.zip` | Accepted production-source replacement after independent archive, code, and test review; two Windows-only test expectations still required minimal follow-ups. |
| v7.1 | test-only patch, 574 bytes, claimed SHA-256 `aec14731bedd681e9c4c63dba11f224c9243e8d0812750b062a277be861f007d` | Added `@pytest.mark.timeout(90)` only to the deterministic full-builder test; no global timeout or simulation reduction. |
| v7.2 | test-only patch, 659 bytes, claimed SHA-256 `0d62c49853cc408c1419238c108d2afeb0ba2eab2617985b312829df250cde12` | Made the cache-read expectation platform-aware; production source remained unchanged. |

The v7.1/v7.2 exact unified diffs were also rendered inline in the Pro
conversation and matched the independently applied one-line/test-only changes.
The browser security layer did not expose those small patch attachments as local
files, so their attachment hashes remain Pro-claimed; the actual repository
diff and resulting test outcomes were independently inspected.

## Accepted v7 archive

- Local archive:
  `C:/Users/28340/Desktop/poly-mm-artifacts/poly-mm-v07-pro-replacement-v7-dbb904a5.zip`.
- Size: `246,311` bytes.
- Outer SHA-256:
  `dbb904a5ea84233188425b2d849ad74c9af7157875b15d741826e0ec048767b8`.
- `changes.patch` SHA-256:
  `be2b319776ec68121c902d83212e0d40fbe732f0eff58619064e62913f80fa17`.
- `DELIVERY_REPORT.md` SHA-256:
  `0aefb261344c19c0723a2b85eddd731ba5f4c4ccca09e604ba58260045ccd459`.
- `SHA256SUMS.txt` SHA-256:
  `e1e4825ef90f41983179a0bd06c31aa46ea6a5d5ff2361ae4c72c6494945879e`.
- Archive verification: 29 exact/case-fold unique regular files, no unsafe
  paths, no symlinks, CRC clean, and 28/28 payload hashes matched.
- Patch verification: strict apply against `c557160d`, 12 additions,
  two existing-file modifications, zero deletions, and 14/14 repository
  files byte-identical to the packaged copies before v7.1/v7.2.
- The exact external report is preserved as
  `research/v07_profitability_2026-08-15/CHATGPT_PRO_V7_DELIVERY_REPORT.md`.

## Final independent review

Two fresh read-only review lanes returned no actionable findings:

- Standards: Windows missing-stat fallback is fail-closed, POSIX token bytes
  remain unchanged, the local test timeout is appropriate, and dependencies/
  live-order scope did not expand.
- Spec: the old generic authority helpers are absent, public evaluation stays
  fail-closed, high-level builder revalidation is present, and the documents
  honestly describe an API-misuse guard rather than cryptographic provenance.

The accepted feature commit is
`802f52a20408443836819116012762648347ed13`; the reviewed merge into
`main` is `a0b3dbd9572d1bc2699159bc199df27666989dff`.

## Authority and economic boundary

The repository can prevent supported public API misuse, but it cannot make an
in-process Python result unforgeable to an actor who controls local execution or
source. No external signature or third-party timestamp exists. That residual is
recorded rather than hidden.

The current machine-readable verdict remains:

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

No paper, fixture, Wine, counterfactual, or synthetic run is represented as real
production profitability.
