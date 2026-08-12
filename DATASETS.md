# Dataset storage policy

The repository tracks source code, tests, configurations, lock files, research
reports, compact immutable freezes, manifests, checksums, and reproducibility
instructions.

It intentionally does not track two local runtime datasets:

- `data/`: append-only forward captures, service state, logs, active partials,
  and finalized raw/manifest pairs. At the 2026-07-26 publication audit this
  directory was approximately 50 GB and still growing.
- `research/edge_discovery_2026-07-24/weather_public_run/cache/`: reproducible
  public Gamma acquisition cache. Several individual responses are roughly
  200 MB and exceed GitHub's regular Git file limit.
- `research/edge_discovery_2026-07-24/constraint_experiment_runs/**/raw/` and
  `candidates.jsonl`: repeated public-source cache entries and large scan row
  sets. Each run's configuration, summary, graph, source manifest, and
  reproducibility metadata remain tracked.

The canonical compact evidence and the hashes needed to identify or rebuild
excluded inputs remain under
`research/edge_discovery_2026-07-24/`. See:

- `research/edge_discovery_2026-07-24/REPRODUCTION.md`
- `research/edge_discovery_2026-07-24/DATA_MANIFEST.json`
- `research/edge_discovery_2026-07-24/DATA_QUALITY.json`
- `research/edge_discovery_2026-07-24/DATA_DICTIONARY.md`
- `research/edge_discovery_2026-07-24/FINAL_BUNDLE_CONFIG.json`

Do not use `git add -f` to bypass these exclusions. If the complete raw capture
must be shared later, publish a frozen, checksummed snapshot through explicitly
approved object storage or metered Git LFS after confirming storage cost and
retention. Mutable `.jsonl.partial` files must never be published as finalized
evidence.
