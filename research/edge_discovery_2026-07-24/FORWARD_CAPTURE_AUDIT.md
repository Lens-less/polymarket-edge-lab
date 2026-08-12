# Forward recorder audit

Audit date: 2026-07-24 (UTC)  
LaunchAgent: `com.lens.polymarket-edge-lab-capture`  
Safety: public GET/WebSocket data only; credentials were not read; no orders
were submitted; `assert_new_orders_disabled()` remained active.

## Running service

The installed LaunchAgent runs:

```text
/Users/lens/Downloads/polymarket-mm-bot/.venv/bin/python
/Users/lens/Downloads/polymarket-mm-bot/scripts/run_edge_capture.py
--config /Users/lens/Downloads/polymarket-mm-bot/research/edge_discovery_2026-07-24/FORWARD_CAPTURE_CONFIG.json
--duration 3600
--proxy http://127.0.0.1:7897
```

It is configured with `RunAtLoad`, `KeepAlive`, a 30-second throttle, explicit
stdout/stderr paths, and one-hour bounded recorder sessions. Checkpoints and
immutable raw/manifest pairs make the capture recoverable across restarts.

At 2026-07-24T10:25:39Z and 10:26:01Z, `launchctl` reported `state=running`,
`runs=1`, PID `45088`, and no prior exit. The first process spent about 2.5
minutes in Python path initialization before opening capture partials. This is
recorded as a long cold start, not hidden as continuous availability.

## Growth verification

Two read-only snapshots proved forward progress:

| UTC snapshot | PID | finalized raw files | manifests | active partials | observation |
|---|---:|---:|---:|---:|---|
| 2026-07-24T10:27:43Z | 45088 | 63 | 63 | 6 | CLOB WS 13 rows; RTDS 2 rows; HTTP snapshots present |
| 2026-07-24T10:28:11Z | 45088 | 77 | 77 | 6 | 14 new finalized pairs; CLOB/RTDS batches rotated; checkpoints advanced |

At 2026-07-24T10:28:45Z:

- CLOB market WebSocket checkpoint: `recorder_records=1200`;
- RTDS checkpoint: `recorder_records=1300`;
- the most recent finalized CLOB batch contained 50 rows;
- the most recent finalized RTDS batch contained 52 rows;
- both raw files and their SHA-256 manifests existed;
- recorder session ID was `79000b83fc564593a9dcdbf7234b0bd4`;
- `recorder.stderr.log` remained empty.

## Integrity verification

`CaptureStore.audit_integrity()` at 2026-07-24T10:28:25Z reported:

```text
checksum_mismatches=0
invalid_manifests=0
manifest_without_raw=0
raw_without_manifest=0
orphan_partials=6
```

The six mutable `.partial` files belonged to the live PID and changed or
rotated between adjacent snapshots. At 2026-07-24T10:29:10Z, `lsof -nP -p
45088` also showed six writable file descriptors pointing to those exact
partials, proving that they were active writer-owned files rather than
orphans. `audit_integrity()` conservatively reports all partials as orphan
candidates because it does not inspect process ownership; the data manifest
must classify them as mutable L0/excluded until finalized, not as six
confirmed abandoned files.

## Supervised restart verification

The one-hour process boundary was allowed to complete naturally; no stop,
kickstart, or manual restart command was issued. At
2026-07-24T11:27:03Z, `launchctl` reported:

```text
state=running
runs=2
pid=9770
last exit code=0
```

This proves that LaunchAgent supervision recovered from a clean, bounded
recorder exit. The replacement process started at
2026-07-24T11:26:59Z. The hardened capture-path source files had modification
times between 2026-07-24T11:01:27Z and 11:11:25Z, before the replacement
process start, so the new process loaded the reviewed network-safety and
recorder implementation rather than the pre-hardening code.

The replacement recorder session ID is
`464e9a0d490c4fb39790750826ca4dde`. Two read-only snapshots proved forward
progress:

| UTC snapshot | PID | new-session manifests | active partials | writable partial FDs | stderr bytes |
|---|---:|---:|---:|---:|---:|
| 2026-07-24T11:27:36Z | 9770 | 20 | 6 | 6 | 0 |
| 2026-07-24T11:27:54Z | 9770 | 26 | 6 | 6 | 0 |

All six required source families were active: `gamma_http`, `clob_http`,
`clob_market_ws`, `rewards_http`, `rules_http`, and `rtds_ws`.
`CaptureStore.audit_integrity()` after restart reported zero checksum
mismatches, zero invalid manifests, zero manifest/raw pairing errors, and six
mutable partials, each owned by PID 9770. A high-confidence scan of the first
54 new-session files and 1,314 records found zero credential, sensitive-query,
or persisted sensitive-header findings. No literal `set-cookie` value was
present.

## Final handoff snapshot

At `2026-07-24T11:45:38Z`, the supervised replacement process was still
running as PID `9770` with `runs=2`. The replacement session had grown from
26 to 637 finalized manifests; the complete capture root contained 1,850
finalized manifests. All six `.jsonl.partial` files were open for writing by
that PID, recorder stderr remained empty, and a fresh
`CaptureStore.audit_integrity()` reported:

- checksum mismatches: `0`
- invalid manifests: `0`
- raw files without manifests: `0`
- manifests without raw files: `0`
- active partials: `6`

A final high-confidence scan covered 6,084 JSON/JSONL files and 114,429
records across the Edge Lab research directory and the complete live raw
capture root. It found zero persisted credential, private-key, token, JWT,
Basic-auth URL, sensitive-query, authorization-value, or sensitive-header
values. This snapshot is operating evidence only; it does not promote any
strategy or capture into a profitable backtest.

## Early-disconnect incident and verified repair

Later supervised sessions exposed a reconnect cleanup defect in the installed
`websockets 16.1.1` client. The recorder continued to recover and write data,
but stderr accumulated the following callback exception when a proxy/TCP
connection was lost before `connection_made` had initialized the receive
assembler:

```text
AttributeError: 'ClientConnection' object has no attribute 'recv_messages'
```

This is the same early-disconnect race reported in upstream
[`websockets` issue #1629](https://github.com/python-websockets/websockets/issues/1629).
A deterministic local reproduction called `connection_lost` before
`connection_made` and failed with the same exception. The recorder now passes
a narrow `RecorderClientConnection` subclass to `websockets.connect`; normal
connections retain the upstream path, while the pre-connection failure path
completes protocol EOF, ping, keepalive, waiter, and optional drain cleanup
without accessing the absent assembler.

The regression was added before the implementation and now passes together
with the recorder/capture/safety suite. To load the fix, PID `800` was
intentionally sent `SIGINT`; that controlled restart explains the LaunchAgent's
subsequent `last exit code=1` and the one `CancelledError` summary appended to
stderr. It was not an unprompted capture failure. LaunchAgent supervision
started PID `6912` at `2026-07-24T13:09:54Z` with recorder session
`fdbd937c412248d9811308326648f8e6`.

At `2026-07-24T13:17:30Z`, the repaired process had remained running for more
than seven minutes and showed:

- 277 new finalized manifests containing 14,356 finalized records;
- six current `.jsonl.partial` files, all open for writing by PID `6912`;
- all four HTTP families returning structured successful snapshots, alongside
  continuing CLOB and RTDS WebSocket batches;
- no growth in stderr after process start and no new `recv_messages`
  exception;
- zero JSON parse failures and zero high-confidence secret findings in a
  14,189-record current-session scan.

Six abandoned partials from the older
`bb549a2c2b41448f81094a328a437e21` session were retained and remain excluded
as mutable L0 evidence; they were not deleted or mistaken for current writer
files. This repair proves recorder stability under the observed disconnect
race. It does not make price touches executable fills or change any strategy's
profitability classification.

## Subsequent natural rollovers and shutdown hardening

The next bounded session was also allowed to end naturally. PID `6912`
completed at `2026-07-24T14:10:01Z`; LaunchAgent immediately started PID
`70547` with `runs=3` and `last exit code=0`. Session
`fdbd937c412248d9811308326648f8e6` finalized 2,176 JSONL/manifest pairs
containing 110,223 records and left zero session-owned partials.

Session `3a62773f8ffa46d9bdb4beb8d746d24b` then ran for the full hour. Its first
three CLOB HTTP snapshots each contained exactly one successful official
`GET /time` response and one `clock_probe`; none was missing, duplicated,
malformed or attached to the wrong resource/session. This confirms that
natural LaunchAgent rotation loaded the coarse CLOB server-time evidence.

At `2026-07-24T15:10:49Z`, the next natural rollover reported:

```text
state=running
runs=4
pid=37374
last exit code=0
```

The completed `3a627...` session finalized 1,298 JSONL/manifest pairs
containing 66,912 records, with `capture_error=null` and zero session-owned
partials. The replacement session is
`74170e7885fd4451a4783b09de405eaf`; all six source-family partials appeared
under that session.

Recorder shutdown was additionally hardened so concurrent callers share one
shielded stop operation, all producer/child/transport-close tasks must become
quiescent inside one deadline, and stale callbacks are generation-bound.
PID `37374` is the first naturally launched process that loaded this final
implementation. Its eventual live stop is not claimed yet; the stubborn-task,
concurrent-stop and restart boundaries are covered by deterministic regression
tests.

## Dynamic short-crypto sidecar

A separate public-only LaunchAgent now discovers strict BTC/ETH 5m/15m
announcements from finalized main-recorder batches:

```text
label=com.lens.polymarket-edge-lab-dynamic-short-crypto-capture
data root=data/edge_dynamic_short_crypto_2026-07-24
run isolation=runs/<UTC timestamp>-<UUID>
new orders disabled=true
authenticated endpoints used=0
```

An initial bounded smoke stopped during the large first ingest and correctly
left every input unconsumed for retry. It finalized two JSONL/manifest pairs,
89 Gamma identity verifications and zero worker/order/authentication activity.

The first supervised run
`20260724T150251.667971Z-0bde0ad3a65a4368a88d2cccfbc813cd`
was sent `SIGTERM` only to load the Gamma preactivation repair. LaunchAgent
reported `last exit code=0` and `runs=2`; both active control batches finalized,
leaving zero partials. The finalized files contain 128 service and 124 Gamma
records. Their SHA-256 values exactly match their manifests:

```text
service=df57877ca12e84834780e98ea8e9641725484fa9435b16ef3871df4af1af869e
gamma=c46ad7f5c227e93c3aee1f50a149a372546778ecbf064fb9c176d969d7c1a48f
```

The replacement run
`20260724T150551.207169Z-4135ce7246894253bab5d6b026d7eb5a`
started as PID `22835`; its lock file contained the same PID and sidecar stderr
remained zero bytes. At `2026-07-24T15:08:03Z`, cumulative finalized discovery
had grown from 124 to 126 strict targets. The live control log contained 122
`gamma_target_verified`, three `gamma_poll_deferred` for exact-identity markets
that had not activated yet, and one retryable public HTTP failure for a
just-announced target. No new-run target was permanently rejected.

The scheduled five-minute retry then promoted the two original preactivation
targets from deferred to verified using fresh public Gamma HTTP 200 evidence.
At `2026-07-24T15:14:13Z`, journal reconstruction reported 130 targets, 125
verified, five pending, zero rejected and zero active workers; the remaining
future targets were still far outside the five-minute subscribe lead. PID,
held lock FD and lock-file contents all agreed on `22835`; stderr remained
empty.

The sidecar finalizes discovery inputs before consuming them, rebuilds a
deterministic cumulative registry, persists decisions before worker effects,
and requires same-connection close-time PONG brackets. Exact finalized RTDS
Chainlink open/close boundary extraction and the atomic Gamma/token/liveness
settlement gate are now implemented. Gamma alone still cannot produce a valid
settlement.

Cross-process recovery is also implemented as a finalized-only reducer. Every
old pair is rechecked for schema, bytes, lines, SHA-256, canonical record ID and
checkpoint consistency; partial bytes are excluded. A new process must
durably finalize its recovery decision before network or worker effects.
Unique append-only run roots remain mandatory. This is running collector
evidence, not by itself a completed Phase 2 replay or profitable-strategy
result.

### Phase 2 implementation deployment

After `1064 passed, 11 skipped, 1 deselected`, a second bounded dynamic-only
rollover loaded the final public-trade composite identity code. The prior run
`20260724T170344.039600Z-af83b2ea531548de9cb6bdc871c7f4d5`
cleanly finalized three manifests containing 3,241 canonical records, with
zero remaining partials. LaunchAgent advanced from runs 3 to runs 4 and
reported `last exit code=0`.

The replacement run is
`20260724T174754.422951Z-bcbcaf6e19b449f0bf6b13ac76892da4`.
Before opening a public connection it finalized recovery record
`a0687a7100385b1a2532b4eb0a30642afbd4f64009251b3de43cd7c4cf2846bb`.
That decision replayed 10,703 records from four prior runs; all four were
classified `clean_completed`, with zero gaps, zero exclusions and zero worker
actions. Only afterward did PID `86287` open the loopback-proxied public
connection and start two active control partials. The lock file, LaunchAgent
PID and partial owner agree; sidecar stderr is zero bytes.

The process start time (`2026-07-24T17:47:54Z`) is later than the deployed
service, promoter and composite-identity file mtimes. The main recorder was
not signaled during either dynamic rollover. It naturally advanced to runs 6,
PID `59984`, with `last exit code=0`.

The latest production-shape lifecycle audit read 9,676 finalized manifests and
494,505 records, reconstructed 210 registered targets, and found zero targets
that had yet reached close plus the one-hour settlement deadline. It correctly
returned `waiting_for_mature_targets`, `actual_fill=false`,
`authenticated_fill=false`, `orders_submitted=0` and
`authenticated_endpoints_used=0`. Fourteen partials were reported and
excluded: eight were owned by the two active LaunchAgents and six were
preserved historical abandoned partials. None was used as evidence. This is
the expected pre-window state, not a successful tracer.

## Monitoring commands

```bash
launchctl print gui/$(id -u)/com.lens.polymarket-edge-lab-capture
launchctl print gui/$(id -u)/com.lens.polymarket-edge-lab-dynamic-short-crypto-capture
ps -p "$(launchctl print gui/$(id -u)/com.lens.polymarket-edge-lab-capture | awk '/pid =/{print $3; exit}')" \
  -o pid=,ppid=,lstart=,etime=,time=,state=,%cpu=,%mem=,command=
tail -n 100 data/edge_discovery_2026-07-24/service/recorder.stderr.log
tail -n 100 data/edge_dynamic_short_crypto_2026-07-24/service/dynamic-short-crypto.stderr.log
find data/edge_discovery_2026-07-24/raw -name '*.manifest.json' | wc -l
find data/edge_discovery_2026-07-24/raw -name '*.jsonl.partial' | wc -l
```

The PID is an audit-time value and will change after a supervised restart;
`launchctl print` is the authoritative current-status check.

## Restart-state application and registry-journal repair

An audit after the Phase 2 deployment found a real restart defect rather than
a market-data defect. The reducer produced and durably persisted a correct
`restart_recovery_decision`, but `DynamicShortCryptoService` only copied the
returned target dictionary into an otherwise unused field. Processed discovery
descriptors, cumulative targets, terminal supervisor states, settlement
deadlines, Gamma/Chainlink evidence, decision IDs and sequence watermarks were
not applied to the live service. A restarted process could therefore rebuild
old discovery work and reacquire public evidence unnecessarily.

The same audit explained observed `98.5%..99.9%` CPU in the old PID: whenever
one new finalized discovery file appeared, the service rebuilt the registry
from every historical file and wrote the complete cumulative inputs/targets
snapshot again. In one finalized old-format control batch, 32 v1 registry
revision records occupied 79,076,413 bytes.

The repair was developed with failing regression tests first and now enforces:

- the recovery decision and checkpoint are finalized before state application,
  network work or worker effects;
- every recovered discovery descriptor is revalidated against its current
  manifest, root, bytes, lines and SHA-256;
- processed paths, registry targets, terminal states, deadlines, persisted
  decision IDs, sequence watermark and audit-only liveness are rehydrated;
- finalized Gamma raw bytes/provenance and exact Chainlink boundary evidence
  are rehydrated, while stale liveness never proves a current connection;
- worker-action targets require fresh public revalidation before scheduling;
- only newly finalized discovery files are parsed, then merged into the
  cumulative registry with byte-identical full-rebuild semantics;
- new journal rows use compact
  `edge-lab-short-crypto-registry-revision.v2` deltas; v1, v2 and mixed
  histories remain fail-closed and exactly replayable.

The focused recovery/capture/registry/service suite passed 86 tests. The full
suite passed `1079 passed, 11 skipped, 1 deselected`; `compileall` and
`git diff --check` also passed.

Two dynamic-only controlled rollovers loaded the repair. The old runs
`20260724T174754.422951Z-bcbcaf6e19b449f0bf6b13ac76892da4` and
`20260724T182243.883759Z-f11b1bc461b54b48b7e7877524f69856`
both closed with zero remaining partials and LaunchAgent exit code 0. The
current run is
`20260724T183548.092637Z-ddcc99049ebd45c4989afcfed4d9c502`;
at `2026-07-24T18:42:13Z`, LaunchAgent reported `runs=6`, PID `17157`,
`last exit code=0`, and zero-byte sidecar stderr. The process started after the
service and recovery file mtimes. Its lock file contains `17157`, and `lsof`
shows that the same PID exclusively owns the two current control partials.
The main recorder was not signaled and remained independently supervised.

Before opening a new worker, the current run finalized recovery decision
`b326d64403f51b0584318c23319f3987034cbf2aa52441e279aef8a4dc6e32d5`.
It replayed 14,766 records from six prior runs, classified all six
`clean_completed`, and produced zero gaps, exclusions and worker actions.
An independent offline replay produced the same decision ID and
`state_hash=739280d19ba8cea16d471b9033efc439c58edda270893e042ffb527b756529ef`.

As a non-promotable operational diagnostic, the current mutable control batch
contained eight v2 registry revisions totaling 53,173 canonical JSON bytes at
the observation point, versus 79,076,413 bytes for 32 cumulative-v1 revisions
in the old finalized batch. The current `.partial` remains mutable and is not
used as lifecycle, replay or profitability evidence. Historical v1 replay
still has a measured one-time startup peak of about 1.74 GB RSS and 29 seconds;
v2 prevents that legacy amplification from continuing but does not rewrite or
delete immutable historical evidence.

At the fixed `as_of_ms=1784918533667` follow-up, finalized-only lifecycle
report
`4a345fd67b386009f66720304622bb054b5cbdc25ccfd680c1eca0df93f20637`
read 12,202 manifests and 626,371 records, reconstructed 236 registered
targets, and still found zero mature targets. It excluded 14 partials and
returned `waiting_for_mature_targets`, `actual_fill=false`,
`authenticated_fill=false`, `orders_submitted=0` and
`authenticated_endpoints_used=0`. The independent read-only execution-freeze
candidate listing returned `candidate_count=0`; no promoter was invoked.
The first finalized-registry target reaches close plus the one-hour settlement
timeout at `2026-07-25T19:25:00+08:00`; the twentieth reaches it at
`2026-07-25T20:00:00+08:00`. Zero mature targets before that interval is the
expected external-time gate, not permission to substitute fixture or partial
evidence.

The next fixed heartbeat at `as_of_ms=1784919513192` produced report
`f06d0f83a221260516a2571cdb84c92afdcf61f7bf58db3ee49ea81b5b876306`:
12,520 finalized manifests, 642,705 finalized records, 236 registered targets,
0 mature, 14 excluded partials and `waiting_for_mature_targets`. Safety counts
remained zero and no promoter was invoked.

### Main-recorder natural rollover after the recovery deployment

No signal was sent. At `2026-07-24T19:11:29Z`, the main LaunchAgent naturally
advanced from runs 7 / PID `4354` to runs 8 / PID `38155`, retaining
`last exit code=0`. The completed main session
`905db6c5061e440b91f19e82e6b4a8cf` ended with `capture_error=null`, 2,847
JSONL/manifest pairs, 144,194 records and zero session-owned partials. A fresh
full-root integrity audit found zero checksum mismatches, invalid manifests,
raw-without-manifest or manifest-without-raw artifacts. The only 12 partials
were the six preserved historical abandoned files and the six new-session
files opened by PID `38155`.

The dynamic sidecar remained PID `17157`, runs 6, `last exit code=0` and
zero-byte stderr throughout the main rollover. It immediately ingested the
new finalized discovery files; its mutable diagnostic registry reached 258
targets. Four temporary public Gamma status failures from the previous poll
all became verified on retry; the only latest non-verified entries were two
new future markets correctly classified `gamma_not_activated_yet`. These
mutable observations are not promoted evidence.

The post-rollover fixed lifecycle report
`1ea87575fb680905beee7799b2b719945c936ba09addc225bd2b4ba772a07cc2`
at `as_of_ms=1784920354808` read 12,964 finalized manifests and 665,125
records. It still found 236 finalized registered targets, 0 mature and 14
excluded partials, returning `waiting_for_mature_targets` with every safety
count at zero. The higher live registry count is intentionally absent until
its current control batch is finalized.

At `2026-07-24T18:52:46Z`, both read-only validation entrypoints remained
valid. Dynamic `--validate-only` checked 9,654 finalized discovery files and
reported `network_requests=false`, `writes_performed=false`,
`new_orders_disabled=true`, manifest validation enabled and separate input/
output roots. A fresh `CaptureStore.audit_integrity()` pass over the main root
and every dynamic control store found zero checksum mismatches, invalid
manifests, raw-without-manifest or manifest-without-raw artifacts. The main
root had six current-session partials owned by PID `4354` plus the same six
preserved historical abandoned partials; the dynamic root had only the two
current partials owned by PID `17157`, while all six closed dynamic runs had
zero partials. The network-safety, freeze, promoter and lifecycle focused
suite passed 60 tests.

The one new operational fault in that interval was a retryable public Gamma
HTTP status failure for future target
`btc-updown-15m-1785005100` at `2026-07-24T18:52:49.290Z`. The next scheduled
poll emitted `gamma_target_verified` for the same market at
`2026-07-24T18:58:04.857Z`; it was not permanently rejected. Both observations
were still in the PID-owned mutable control batch and are recorded here only
as runtime diagnostics, not promoted evidence.

A fresh full offline baseline then passed
`1079 passed, 11 skipped, 1 deselected in 95.74s`; `compileall` and
`git diff --check` also passed. A pre-window diagnostic selected the first 20
targets solely from the last finalized cumulative registry. Their latest live
Gamma status was 20/20 `gamma_target_verified`, and each of the 20 latest
control references pointed to an existing raw Gamma record ID. Those Gamma
records were still in PID `17157`'s mutable batch, so the check establishes
runtime referential consistency only and is not lifecycle or promotion
evidence.
