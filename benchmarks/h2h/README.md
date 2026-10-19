# Head-to-head: one agent, one tool set, "find every public dataset that matches a need"

We give Claude a dataset-search task with one tool set per run and score its list
against a key built without either server. On the one held-out round so far
(round 3: 5 tasks, 2 runs each, Claude Sonnet 5.5), mean recall was 0.49 with
this server, 0.35 with web search and 0.26 with ToolUniverse; this server against
web search is not separated (higher on 4 of 5 tasks, exact sign test p = 0.38).
What separates the arms is tool calls: this server made 7.7 fewer per run than
web search (95% CI 1.8 to 13.1) and 24.9 fewer than ToolUniverse (12.8 to 47.1),
while cost and wall time against web search are not separated. The main
limitation is size: 5 held-out tasks, with every earlier round used to tune the
server. Round 4 (2026-10-19, Opus 5.5, keys and adjudication by agents without this
server) did not separate the arms either: mean recall 0.66 with this server, 0.66 with
web search, 0.59 with ToolUniverse; see [Re-run 2026-10-18](#re-run-2026-10-18).

## Arms

- `dam`: this server (data-aggregator-mcp) only.
- `tu`: [ToolUniverse](https://github.com/mims-harvard/ToolUniverse) only.
- `web`: no MCP server; Claude Code's web search and web fetch, and a shell.
- `bare` (round 4 on): no way to search, so the model lists from memory.

## Results: round 3, held out

| arm | mean recall (95% CI) | precision | median cost (95% CI) | median tool calls (95% CI) | median wall time, s (95% CI) | valid runs |
|---|---|---|---|---|---|---|
| dam | 0.49 (0.35–0.65) | 0.82 | $0.57 (0.52–0.69) | 12.5 (9–18) | 83 (66–189) | 10 of 10 |
| web | 0.35 (0.25–0.46) | 0.89 | $0.46 (0.28–0.63) | 19.5 (15–31) | 73 (59–268) | 10 of 10 |
| tu | 0.26 (0.15–0.37) | 0.97 | $0.62 (0.59–0.67) | 28.5 (24–75) | 63 (56–125) | 10 of 10 |
| DataCite top 100, no model | 0.17 (0.06–0.28) | 0.21 | | | | |
| DataCite top 25, no model | 0.06 (0.00–0.14) | 0.18 | | | | |
| empty list, or another task's key, no model | 0.00 | 0.00 | | | | |

Paired comparisons, per task (the mean per-task difference, then the sign test):

- Recall, dam vs web: +0.14 (95% CI 0.01 to 0.26), dam higher on 4 of 5 tasks,
  p = 0.38. The 0.71.0 rerun, after tuning on these tasks, gives +0.12 (−0.04 to
  0.29) on 3 of 5. Round 3 is read with the exact sign test, which cannot separate
  4 of 5 tasks even though this bootstrap interval excludes 0; round 4's criterion
  (at least 4 of 5 tasks and an interval above 0) was pre-committed afterwards,
  would call round 3's first run "dam better" and its rerun not separated, and
  applies to round 4 only.
- Recall, dam vs tu: +0.23 (0.14 to 0.31), dam higher on 5 of 5 tasks in both
  round-3 runs (p = 0.0625 each, the floor for 5 tasks).
- Tool calls: dam made fewer than web (−7.7 per run, −13.1 to −1.8) and than tu
  (−24.9, −47.1 to −12.8). The interval excludes 0 in all 8 run sets of rounds 2
  and 3, and dam made fewer calls than tu on every task of every set.
- Cost: dam vs web +$0.11 (−0.04 to 0.25) and dam vs tu −$0.06 (−0.15 to 0.02),
  neither separated.
- Wall time: dam vs web −54 s per run (−135 to +19), not separated, although the
  medians in the table point the other way. dam took longer than tu, +16 s
  (1.7 to 30.8).
- Precision (relevant / listed, unjudged entries counted wrong): tu is more
  precise than dam (−0.15, −0.24 to −0.06, 4 of 5 tasks); dam vs web is not
  separated (−0.07, −0.15 to 0.01).

All intervals are percentile bootstrap 95% intervals that resample tasks (10,000
draws, seed 0). Medians are over runs. Mean recall is the mean over tasks of each
task's mean over runs. [`summary.tsv`](summary.tsv) has every run set by arm, with
the baseline rows; [`comparisons.tsv`](comparisons.tsv) has every paired test.

## Re-run 2026-10-18

Rules for rounds 4 and 5 were committed before either ran:
[`PREREG_2026-10-18.md`](PREREG_2026-10-18.md). Every $ figure is the API-equivalent
price Claude Code reports. The runs used a subscription with overage off, so the
binding budget was seven-day utilization, reported alongside.

**Where the tokens go** ([`diagnose.py`](diagnose.py), 80 round-2/3 runs per arm,
medians): dam spends $0.54 a run against web's $0.43 and tu's $0.57. About 80% of dam's
cost is cache writes (108k tokens against web's 48k), and about half of those are
search output: roughly 1k characters per hit, with the model asking for 30 hits.
Tool definitions are about 4.8k tokens (21.5k fixed context against web's 17.8k).
Tool inputs are small (3.3k characters), so no payloads are pasted. The one tool-name
failure: 11 calls to a bare `resolve` in 5 of 60 round-2 dam runs, none since. Output
size was left alone because the endpoint is recall, not cost.

**Server change** (round 5 only, run from commit 0e3661f with `--server-spec`): a
description that names another tool first says the client may list it with a prefix
(`mcp__data-aggregator__resolve`).

**Round 4** (5 held-out tasks × 4 arms × 3 runs, dam 0.74.0 from PyPI, tu 1.5.7; U5's
key re-frozen 2026-10-18 after an OSF file-level re-check; 87 pooled entries judged
blind by fresh agents with no MCP server):

| arm | mean recall (95% CI) | valid-only | precision | median cost | median calls | median wall, s | valid runs |
|---|---|---|---|---|---|---|---|
| dam | 0.66 (0.46–0.86) | 0.66 | 0.88 | $1.87 | 20 | 176 | 15 of 15 |
| web | 0.66 (0.44–0.87) | 0.83 | 0.77 | $1.86 | 57 | 677 | 12 of 15 |
| tu | 0.59 (0.34–0.81) | 0.59 | 0.81 | $2.16 | 34 | 187 | 15 of 15 |
| bare | 0.02 (0.00–0.04) | 0.02 | 0.33 | $0.05 | 1 | 19 | 15 of 15 |
| DataCite top 100, no model | 0.17 (0.04–0.32) | | 0.39 | | | | |

Per task (dam / web): U1 0.55 / 0.64, U2 0.95 / 0.95, U3 0.90 / 0.30 (two web
timeouts), U4 0.63 / 0.92, U5 0.29 / 0.49. **Against the pre-committed rule
(dam above web on at least 4 of 5 tasks and the interval above 0): not separated.**
dam was higher on 1 task, web on 3, and they tied on 1. The difference is +0.004
(−0.22 to 0.32). Counting only valid runs, web leads (0.83 against 0.66). What does
separate: dam made fewer tool calls than web (−34 a run, −50 to −15) and finished
faster (−454 s, −633 to −220), and it was more precise (+0.12, 0.02 to 0.24). It cost
less than tu (−$0.51, −0.83 to −0.25). Cost against web was not separated. Bare-name
refusals: 0 in 15 dam runs. The sweep cost $90.61 and seven-day utilization rose
0.24 → 0.30.


- Oracle (positive and negative): for all 15 tasks of rounds 2 to 4, the key
  listed by its own ids scores recall 1.0, precision 1.0 and nothing unjudged, and
  a list of near-miss ids (each id with a suffix) scores 0.0 (`run.oracle_problems`,
  tested in CI and run before any sweep, which stops if it fails).
- Baselines with no model, through the same scorer (arms `base_*` in
  `summary.tsv`): an empty list, another task's key, and DataCite's top 25 and 100
  datasets for a short keyword query per task (queries written down before the
  fetch in `analyze.DATACITE_QUERY`; results in
  [`baselines/datacite.json`](baselines/datacite.json), fetched 2026-10-16).
- Validity, one rule for every arm (`run.validity`): a run counts only if it
  finished (no timeout, harness error or budget stop), started with exactly its
  arm's built-in tools and server, used a data tool at least once (bare excepted)
  and touched nothing of the benchmark. An invalid run scores recall 0, and
  `summary.tsv` also gives the valid-only mean. A finished run is never re-run; a
  run the harness died in is moved to `_aborted/` and logged, the same for every
  arm. 3 of 243 archived runs are invalid, all in round 2: a timeout in `r2_266`
  (web) that had been kept as valid, and the two leak-scan hits. Round 3 has none.
- Leak scan (`run.leak_hits`): every tool input and output of every run is
  searched for the key and adjudication files, this benchmark's paths on disk and
  on GitHub, and other runs' directories. Across 243 runs and 10,010 tool blocks
  it found 2 hits, both scratch files an agent wrote one level above its own run
  directory (`r2_066` web R2 r1, `r2_final` tu R5 r2), with no key or other run
  read. As a control, without excluding a run's own directory, all 240 runs of
  rounds 2 and 3 hit (their path names the round), so the scan does read the
  transcripts.

## Limitations

- Five tasks per round. No sign test over 5 tasks can go below p = 0.0625, and a
  bootstrap over 5 tasks is too narrow to rely on.
- Only round 3's first run is held out. Round 2's tasks were used to diagnose
  about ten server changes (#244 to #271), and round 3's for three more (#277,
  #278, #281), so both are now tuning data.
- The repository is public, so the keys are on the web. The leak scan is the
  guard.
- Round 3's 50 pooled entries were judged blind to arm, but by the agents that
  built the keys.
- Rounds 1 to 3 gave the server arms Write but not Read, so ToolUniverse could not
  open its own oversized results, which Claude Code saves to a file. That hit 24
  of 30 ToolUniverse runs (`r2_final`, `r3_heldout`, `r3_071`) and 0 of 30 dam
  runs, which handicaps tu in those rounds.
- Rounds 1 to 3 ran Claude Sonnet 5.5 with a $3 cap, ran the dam arm from a
  checkout, and inherited the developer's Claude Code settings. Round 4 runs Opus
  5.5 with a $6 cap, both servers from pinned PyPI releases, and no user settings.

## Method

[`run.py`](run.py) drives headless `claude -p` runs, one arm's tools per run, and
scores each run from its transcript and the list it wrote. Recall is the share of
the key's studies the list names; borderline studies are optional. Entries the
key does not cover are pooled and judged blind to arm, then everything is
re-scored. [`analyze.py`](analyze.py) re-scores every archived run set and writes
`summary.tsv` and `comparisons.tsv`. The rounds' own write-ups:
[round 1](RESULTS.md), [round 2](round2/RESULTS.md), [round 3](round3/RESULTS.md),
[round 4](round4/README.md).

Run transcripts are kept outside the repository, in
`~/data-aggregator-mcp-artifacts/h2h/` (`MANIFEST.txt` has their hashes). The
History table gives each set's folder in brackets where it differs from the set
name.

## History

| when | what | why | mean recall dam / web / tu | set (folder) |
|---|---|---|---|---|
| 10-04 | round 1: 8 "find this dataset, download a file" tasks | first comparison | found 16/16 every arm; bytes 15 / 15 / 12 of 16 | [`scores_2026-10-04.json`](scores_2026-10-04.json) |
| 10-05 | round 2: 5 "find every dataset" tasks, list scorer, blind adjudication | round 1 was at ceiling | 0.58 / 0.74 / 0.65 | `r2_first` (`r2_runs`) |
| 10-05 to 10-07 | server changes #244 to #271, each checked on round 2 | misses diagnosed from round-2 transcripts | final, 0.69.1: 0.85 / 0.75 / 0.58; tuned on these tasks | `r2_rerun`, `r2_066`, `r2_0671`, `r2_266` (`r2_266_3arm`), `r2_final` (`r2_0691`) |
| 10-11 | round 3: 5 new held-out tasks, dam frozen at 0.69.1, ToolUniverse 1.5.6 | round 2 was fitted | 0.49 / 0.35 / 0.26 | `r3_heldout` (`r3_runs`) |
| 10-12 | #277, #278, #281 fixed on round 3; rerun on 0.71.0 | gap audit | 0.55 / 0.43 / 0.30; now tuning data | `r3_071` |
| 10-16 | harness isolation (#298): pinned PyPI releases with `uvx --exclude-newer`, a fresh `/tmp` directory per run, no user settings, one system prompt, Opus 5.5, provenance per row | runs inherited the developer's settings and a checkout | pilot only; the round-4 sweep has not run | `r4_pilot` |
| 10-16 | fairness fixes (#298): server arms get Read; `bare` arm; ToolUniverse cache per run; no silent re-run; validity rule; leak scan; oracle and version preflight; URL-form ids for every archive, accessions matched exactly | ToolUniverse could not read its own saved results; a timed-out run had been kept as valid | re-score below | |

Re-scoring every archived run with the fixed scorer changed no row. The only
differences from the committed score files are in round 2 R4, whose key grew from
35 to 36 studies through later adjudication, so each arm's R4 recall drops by
1/36. The validity rule made 3 runs invalid (see Controls), which moved three
means: `r2_066` web 0.76 → 0.67, `r2_266` web 0.76 → 0.70 and `r2_final` tu
0.67 → 0.58. No run in rounds 2 and 3 holds an unjudged entry; the round-4 pilot
holds one (web), so its numbers are pre-adjudication.

## Reproduce

    python3 benchmarks/h2h/analyze.py --artifacts ~/data-aggregator-mcp-artifacts/h2h \
        --write-to benchmarks/h2h/rescored

The round-4 sweep command is in [`round4/README.md`](round4/README.md). Future
rounds build their keys and run directories under `~/data-aggregator-mcp-artifacts/`,
outside any checkout, and commit the keys' sha256 before the sweep and the keys
after it.
