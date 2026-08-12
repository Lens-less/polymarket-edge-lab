# Data Quality

As of: `2026-07-24T11:27:10.995744Z`

Canonical DATA_MANIFEST: `capture_freeze_entity_clock_strict_v3/DATA_MANIFEST.json` (`ba5d86527a4bece9046591187f256d6e677596336904a3257209c954a821ecab`). Summary: `{"capture_domain_record_count": 32210, "capture_path_inventory_stable": true, "capture_record_count": 32872, "checkpoint_count": 6, "computed_input_checksum_count": 1369, "finalized_batch_count": 663, "legacy_artifact_count": 31, "legacy_embedded_sample_count": 32859, "legacy_jsonl_record_count": 21, "legacy_keyed_sample_row_count": 32859, "legacy_l1_artifact_count": 28, "legacy_reused_sample_row_count": 22456, "legacy_unique_condition_count": 173, "legacy_unique_sample_observation_key_count": 10403, "partial_file_count": 6, "raw_without_manifest_count": 0, "unsafe_input_path_count": 0}`

Matching DATA_QUALITY: `capture_freeze_entity_clock_strict_v3/DATA_QUALITY.json` (schema and manifest hash matched), reported status=`degraded`. A non-pass status remains a readiness blocker.

Input inventory contains 37 entries; 0 are missing or invalid.

| 类型 | 路径 | 状态 | SHA-256 / 原因 |
|---|---|---|---|
| baseline | `BASELINE_EVIDENCE_PACK.json` | loaded | `078bd4dfacf5b05334589e8d9dd4e2113441d2474625e81087892268fcb0ecb8` |
| rewards | `selective-liquidity-rewards-20260724T101146.689408Z.json` | loaded | `a239ae357bfaa9648ff2c13d5a95886e27e3e699a748a9487ad097d3be130d83` |
| rewards | `selective-liquidity-rewards-20260724T101235.403362Z.json` | loaded | `b8fd64883649c261594c3158bffe28d787107e46021b401d09cea690c7e7a7af` |
| rewards | `selective-liquidity-rewards-20260724T101413.804863Z.json` | loaded | `4c87ee903cd71b162291ee35139ff800bc16df35966285fa855df860f16947e9` |
| rewards | `selective-liquidity-rewards-20260724T103217.948461Z.json` | loaded | `1399101bbe3fe201e6430c9876fd92a338bb56e267afdc5936cb7dea79efca4d` |
| reward_manifest | `reward_runs/selective-liquidity-rewards-20260724T112636.637029Z/RUN_MANIFEST.json` | loaded | `613e46d4effbde03992d989c81b4c0d27771124939f36ba1bcb0267aaba0e498` |
| rewards | `reward_runs/selective-liquidity-rewards-20260724T112636.637029Z/EXPERIMENT.json` | loaded | `a5a7d6ca0ebd954b9703a050b0c2a26cae37591cd0bd2f71c8abc91f614e5a78` |
| constraints | `constraint_experiment_runs/current-20260724-augmented-v1/summary.json` | loaded | `16f33ac712990bab9600ef9e2fc2e288c776c4c8458ad13ad3c94889378dc828` |
| constraints | `constraint_experiment_runs/current-20260724-augmented-v2/summary.json` | loaded | `cf3555e2117042160b31f70bcae9c6b0d4665942aea42d0261f5e17f979663b2` |
| constraints | `constraint_experiment_runs/current-20260724-augmented-v3-provenance/summary.json` | loaded | `448d8c4a2109a3c247fdfb59552cd1d1359792cdfef3c75b0ca6a37dd536b942` |
| constraints | `constraint_experiment_runs/current-20260724-augmented-v4-final-provenance/summary.json` | loaded | `56f99cbf5c7e1f2efe170ad05157f7d1b4d3d9d96e1dd172189f98ae4b690417` |
| constraints | `constraint_experiment_runs/current-20260724-augmented-v5-security-final/summary.json` | loaded | `6c1485fed29e506f54c01941a9c3ac84cf08031c3447569f2fab44ba4f8c8b4b` |
| constraints | `constraint_experiment_runs/current-20260724-standard-v1/summary.json` | loaded | `9d1cdd3d1fd8bfdeb2b8dfc8adbcec06a599b347f2f5c58cf3026e29c438b3a9` |
| constraints | `constraint_experiment_runs/current-20260724-standard-v2/summary.json` | loaded | `db8cf3073a2c5a1059e986406b6c32c04a8d1a85057b8a505742cd68cf0ef4b6` |
| constraints | `constraint_experiment_runs/current-20260724-standard-v3-skew30s/summary.json` | loaded | `b7585960ed26b3c67ec07831adc5016ee705e767882368aa555e985b9f5d5a25` |
| constraints | `constraint_experiment_runs/current-20260724-standard-v4-provenance/summary.json` | loaded | `23cc213a944507cd0614b27d21af47a2137d34b576f67fe9a129c56545a11cc5` |
| constraints | `constraint_experiment_runs/current-20260724-standard-v5-final-provenance/summary.json` | loaded | `a4d90493a5ac4cd4a5917df79ee4a77529ff2a8e2dd46b779493c95007636f28` |
| constraints | `constraint_experiment_runs/current-20260724-standard-v6-security-final/summary.json` | loaded | `5b93edb71a57d2360dc53269012cca5e7e759fec6a9cc149d3d99be2f74c270f` |
| constraints | `constraint_experiment_runs/replay-20260724-augmented-v3-provenance/summary.json` | loaded | `ed4406b8f18eff81d0c25bc29b8f01320592b49ced4d440417c39f1ff109adb2` |
| constraints | `constraint_experiment_runs/replay-20260724-augmented-v4-final-provenance/summary.json` | loaded | `a7580e4a9871da932e7dd8b826b9fb9951637f38cebc166ee9c73b99a67fc4e5` |
| constraints | `constraint_experiment_runs/replay-20260724-augmented-v5-security-final/summary.json` | loaded | `ce2dbf2c6d529333de04b740a29b603f5eba400cceb5d635e14d4d3b58453987` |
| constraints | `constraint_experiment_runs/replay-20260724-standard-v5-final-provenance/summary.json` | loaded | `6fc2d29f935a5df86dfc43a51fd15c9bcb9de1b035b940ed16935cfebc514dc9` |
| constraints | `constraint_experiment_runs/replay-20260724-standard-v6-security-final/summary.json` | loaded | `6754a6fc5f2e78bc2ef2b68a9669265f3d169128b638cf9f640234f09b63db61` |
| latency | `LATENCY_CAPTURE_EXPERIMENT.json` | loaded | `52894f76bfe53986f4b21dd9f40636125029f53e5a131780639a184d5426a9e9` |
| latency | `LATENCY_CAPTURE_EXPERIMENT_v1_capture-latency-6fc27a1af41426368176.json` | loaded | `d9e19acfb77107cd90f159b9f9043f25760137efd3f08fe43f9be119d2363166` |
| latency | `LATENCY_CAPTURE_EXPERIMENT_v1_capture-latency-fddaaadb3cdbf2fc8c93.json` | loaded | `54767a449cbd2fba3548999d9ec1a726294a8a97d02a6440abddfe5c669be0cc` |
| weather | `weather_public_run/RUN_MANIFEST.json` | loaded | `47553bc8c6bf8ec033c9957b3b8d4e38e37de00e8a1cdf727a1bdd141f5ac242` |
| weather_experiment | `weather_public_run/EXPERIMENT.json` | loaded | `d52982d7aae3e99153fd8ae414aa6c815acafd9d5bdd9b9e9b333462cd3d8d83` |
| data_manifest | `capture_freeze_entity_clock_strict/DATA_MANIFEST.json` | loaded | `ceb672a439a9e13cfdf3fe7ddef9c1aeb1690d05ea136fdda695cddb70f884b1` |
| data_quality | `capture_freeze_entity_clock_strict/DATA_QUALITY.json` | loaded | `9e8210e8e11414c73c6f9f128678a01c1eb83fcff35b9661b1dd4748be6a48f9` |
| data_manifest | `capture_freeze_entity_clock_strict_v2/DATA_MANIFEST.json` | loaded | `bc1b1f2d398523801728806370c8059c343ab5442467c31493fce808f1936b09` |
| data_quality | `capture_freeze_entity_clock_strict_v2/DATA_QUALITY.json` | loaded | `e4d0d14aeebdf6e3c660f913cb6d0d86e26b376768b97ffe2c3cb9a55f157f39` |
| data_manifest | `capture_freeze_entity_clock_strict_v3/DATA_MANIFEST.json` | loaded | `2ad81394e977dfb4067ce49f529340d52abfb789d5cbe2438e2ecffb000c12d4` |
| data_quality | `capture_freeze_entity_clock_strict_v3/DATA_QUALITY.json` | loaded | `2df9cfdae33f514df4dc6ebf9866383b0c2c513b9e5bf44e230be9068bb45c22` |
| data_manifest | `capture_freeze_strict/DATA_MANIFEST.json` | loaded | `3ee18d223a74fece813c927bafb4f3da2d3c20c4245ed4f93a3caecadb3cfe92` |
| data_quality | `capture_freeze_strict/DATA_QUALITY.json` | loaded | `ffb26b569cf01bcb02ed8c740692b75dcc96149f0dbf71ba67f134c0ec1b90f2` |
| data_manifest | `DATA_MANIFEST.json` | loaded | `c91999de661d57056777d219f22fb07a93a46df11e2b9c191a03330d3e0318e8` |

## Evidence ceilings

- L0 synthetic and simulator artifacts are diagnostic only.
- L1 public histories, touches, flow proxies and reward snapshots cannot establish fills.
- L2 forward streams support replay only when integrity, clock, queue and settlement gates all pass.
- Missing metrics remain `null` with a reason; they are never imputed.
