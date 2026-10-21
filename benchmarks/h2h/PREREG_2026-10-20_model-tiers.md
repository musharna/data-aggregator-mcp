# Pre-registration, 2026-10-20: round 5 on weaker models (Haiku 5.5, Sonnet 5.5)

Committed and pushed before either confirmatory sweep. On Opus 5.5, round 5 did not
separate this server from web search (see [`README.md`](README.md#re-run-2026-10-18)).
The question here: does the server lift a weaker model's recall? Anything run after a
sweep other than the adjudication and analysis below is exploratory and labelled so.

## Tiers

- **Haiku 5.5** (`--model claude-haiku-5-5`), then **Sonnet 5.5** (`--model
  claude-sonnet-5-5`) in this session, right after the Haiku sweep, with the same design.
- All four arms are re-run on each tier, interleaved; no Opus row is reused for the
  primary comparison.

## Design (round 5 as in [`PREREG_2026-10-18.md`](PREREG_2026-10-18.md), except as stated)

- Tasks and key: round 5's 10 tasks, [`round5/tasks.json`](round5/tasks.json), unchanged
  (sha256 `c12b1d2e...e872e5`, matching [`round5/KEYS_SHA256.txt`](round5/KEYS_SHA256.txt)).
  The oracle passes on 2026-10-20 (key 100%, wrong list 0% on all 10 tasks). Live check of
  the key the same day: 114 of the 115 keyed studies with a DOI resolve at doi.org; the
  other (V6 S5, PANGAEA 484983/484984) has no doi.org handle but answers at PANGAEA's own
  API, as when web and tu found it in round 5. The 76 accession-only studies were not
  re-checked. No key is rebuilt.
- Arms `dam,tu,web,bare`; 3 reps; 120 runs per tier. Job order as round 5 (rep 1 of every
  task and arm, then rep 2, then rep 3; within a rep task by task, arms dam, tu, web, bare).
- dam = data-aggregator-mcp **0.75.4**, the current PyPI release, installed from its
  release commit: `--server-spec git+https://github.com/musharna/data-aggregator-mcp@8e10a5b5b0009ed57c5efb95a780fd9a98df95d6`
  (tag v0.75.4). The PyPI wheel was uploaded 2026-10-20 03:18 UTC, after the dependency
  cutoff, so `uvx --exclude-newer 2026-10-17T00:00:00Z` cannot resolve it from PyPI; the
  commit is installed with that same cutoff for its dependencies, as round 5 installed
  0e3661f. 0.75.2 to 0.75.4 add to 0e3661f a wider tool-name prefix note, a clearer Kaggle
  403 message and fixes from mutation testing. tu = `tooluniverse==1.5.7` (still the latest
  release). Claude Code 2.1.305 (recorded per row). $6 cap, 900-second timeout, as round 5.
- Workers: `--workers 2` (round 5 ran 1). The harness is network-bound, and the Haiku
  pilot (round 4's U2, all 4 arms, 1 rep, 2 workers, exploratory, not part of any result)
  gave 4 of 4 valid rows in 5.5 minutes; 1 worker would hold the shared sweep lock about
  twice as long. With 2 workers, a task's dam and tu runs overlap, then its web and bare.
- Runs under `flock <shared sweep lock> env HEAVY_RUN_MEM=6G heavy-run bash
  gated_sweep_tiers.sh ...` (round 5's start gate: five-hour below 0.50, at least 5 GB
  free), run directories `~/data-aggregator-mcp-artifacts/h2h/r5_haiku_runs` and
  `r5_sonnet_runs`. `run.py` refuses an output directory started on another model,
  records the model asked for and the models served in `versions.json`, and a run served
  another model than it asked for is invalid.
- Controls unchanged: oracle before the sweep, validity rule, leak scan, the model-free
  baselines (round 5's DataCite rows), and the rate-limit rule: a row the rate limit
  rejected before the model answered is a harness failure, re-run once; a second such
  failure stays as the row and scores invalid.

## Usage (the 10-20 amendment: the user said not to wait for the weekly reset)

- seven-day was 0.69 at 18:39 EDT. The sweep stops at seven-day >= 0.90 (`run.py
  --stop-at 0.90`, and `STOP_AT=0.90` for the start gate); a five-hour reading >= 0.85
  pauses it until that window resets. Rows made before a stop are kept and scored as they
  stand (rep-major order, so with fewer reps), and the shortfall is reported.
- Projection: seven-day added per dollar of API-equivalent cost, from rounds 4 and 5
  (0.06 for $90.61 and 0.13 for $185.00, about 0.0007 per $), times 120 runs at the
  tier's pilot cost per run. Haiku pilot: $0.44 for 4 runs, so about $13 and +0.01 for
  120 runs; the reading before it was 0.75. Sonnet: a 1-task pilot (same task, 4 arms)
  runs first; the Sonnet sweep starts only if the reading before it plus 30 x the pilot's
  cost x 0.0007 is at most 0.90; otherwise it does not run and the projection is
  reported. Pilots are exploratory and enter no result.

## Adjudication

Each sweep's pooled new entries (`pool.json`, scored against round 5's adjudication, no
arm, shuffled) are judged blind exactly as round 5's were: one fresh headless `claude -p`
session per task on Opus 5.5 with no MCP server, tools Bash/Read/Write, the archives' own
APIs only, transcripts audited. One change: the judge also sees round 5's earlier
verdicts' studies (as `prior-A1`...) so that it can mark an entry as another copy of one,
and new studies get the labels `HA1`... (Haiku) and `SA1`... (Sonnet)
(`adjudicate.py --prior --prefix`). The Sonnet pass takes the Haiku result as its prior.
Both tiers and the cross-set comparisons are scored with the final file,
`round5/adjudication_tiers_2026-10-20.json` (round 5's verdicts plus both passes').

## Endpoint, test and rule

- **Primary endpoint per tier**: round 5's. Each task's mean recall over its 3 runs after
  adjudication, dam vs web, paired by task. **Timeouts: a run that hits the 900-second
  timeout is invalid and scores recall 0** (round 5's rule; web timed out in 11 of 30
  Opus runs and that drove round 5's primary).
- **Tier verdict**, round 5's rule unchanged: "dam better" if dam's task mean is above
  web's on at least 8 of 10 tasks (ties count against dam) AND the bootstrap 95% interval
  of the mean per-task difference (dam - web) is above 0 AND the valid-only check agrees;
  "dam worse" the mirror image; otherwise "not separated".
- **Valid-only (pre-registered secondary)**: the same comparison with every invalid run
  (timeouts included) dropped, and a task with no valid run in either arm dropped; reported
  with its own verdict under the same rule, as well as feeding the agreement check.
- **p for the tiers**: the exact two-sided sign test on the per-task differences
  (`analyze.sign_test`, ties dropped), Holm-adjusted across Haiku and Sonnet.
- **Lift at a tier**: verdict "dam better" AND Holm-adjusted p < 0.05. **Loss at a tier**:
  "dam worse" AND Holm-adjusted p < 0.05.
- **Win** (per the 10-20 ledger): a lift at one or both tiers AND no loss at the other.
  **Loss**: a loss at one or both tiers and no lift. Anything else: tie. If the Sonnet
  sweep is stopped by the usage gate, its partial rows are analyzed as they stand and the
  verdict says so.
- With 10 tasks a lift needs at least 9 tasks higher (9 of 10 with 1 lower: p = 0.021;
  Holm compares the smaller p with 0.025), or 8 higher with the other 2 tied (p = 0.008).
- Intervals: percentile bootstrap 95% over tasks, 10,000 draws, seed 0.

## Secondary and mechanism (reported, not part of the verdict)

- dam vs tu, each arm vs bare (the floor); precision, cost, wall time and tool calls with
  intervals; the DataCite baselines.
- Mechanism: data-aggregator tool calls per dam run (median and range), and dam runs with
  no server call ("mechanism did not fire"); the same for tu.
- **Substitute claim (non-concurrent)**: each tier's dam against the Opus round-5 web and
  dam rows (sweep 2026-10-18/19), the same statistic and interval, both scored with the
  final adjudication file. Labelled non-concurrent: a different day, Claude Code version
  and dam commit.

## Reporting

Every result is reported, whatever it is, in a dated README section "Haiku and Sonnet
re-run 2026-10-20"; the headline and the top-level summary change only from the
confirmatory result.
