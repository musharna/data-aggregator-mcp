# Head-to-head: one agent, one tool set, "find every public dataset that matches a need"

`run.py` drives headless `claude -p` runs, one arm's tools per run, and scores each run
from its transcript and the list it wrote against a key built without either server.
`analyze.py` rescores every archived run set and writes `summary.tsv` (per arm, with
model-free baseline rows) and `comparisons.tsv` (paired per-task tests). Round
write-ups: `RESULTS.md` (round 1), `round2/`, `round3/`, `round4/`.

## What the numbers say so far, plainly

Only round 3's first run (`r3_heldout`) is held-out evidence: round 2's five tasks were
used to diagnose about ten server changes, and round 3's were used for three more after
it ran. Five tasks, two runs each, Claude Sonnet 5.5:

| arm | mean recall [95% CI] | median cost | median tool calls | median wall time |
|---|---|---|---|---|
| dam | 0.49 [0.35, 0.65] | $0.57 [0.52, 0.69] | 12.5 | 83 s |
| web | 0.35 [0.25, 0.46] | $0.46 [0.28, 0.63] | 19.5 | 73 s |
| tu | 0.26 [0.15, 0.37] | $0.62 [0.59, 0.67] | 28.5 | 63 s |
| DataCite top 100 (no model) | 0.17 [0.06, 0.28] | | | |
| empty list / another task's key (no model) | 0.00 | | | |

- **dam vs web is not separated.** dam was higher on 4 of 5 tasks (exact sign test
  p = 0.38); the mean per-task difference is +0.14 [0.01, 0.26]. A bootstrap over five
  tasks is too narrow to rely on, and the 0.71.0 rerun on the same tasks gives
  +0.12 [-0.04, 0.29] (3 of 5 tasks). With 5 tasks no sign test can go below p = 0.0625.
- **dam vs tu**: dam higher on 5 of 5 tasks in both round-3 runs (p = 0.0625 each, the
  floor), difference +0.23 [0.14, 0.31].
- **What does separate the arms is the work, not the cost**: dam made fewer tool calls
  than web (-7.7 per run [-13.1, -1.8]) and than tu (-24.9 [-47.1, -12.8]) on held-out
  tasks, and the interval excludes 0 in all 8 run sets (fewer than tu on every task of
  every set); dam vs web cost is not separated (+$0.11 [-0.04, 0.25]).
- Precision (relevant / listed, unjudged entries counted wrong), held out: dam 0.82,
  web 0.89, tu 0.97. tu is more precise than dam (-0.15 [-0.24, -0.06], 4 of 5 tasks);
  dam vs web is not separated (-0.07 [-0.15, 0.01]).

All intervals are percentile bootstrap 95% intervals resampling tasks (10,000 draws,
seed 0); medians over runs; "mean recall" is the mean over tasks of the task's mean over
runs. Every run set's rows are in `summary.tsv`.

## Controls

- **Oracle (positive and negative)**: every round's key, listed by its own ids, scores
  recall 1.0 / precision 1.0 / nothing unjudged, and a list of near-miss ids (each id
  with a suffix) scores 0.0, for all 15 tasks (`run.oracle_problems`, tested in CI, and
  run before any sweep, which stops if it fails).
- **Baselines with no model**, through the same scorer (`summary.tsv`, arms `base_*`):
  an empty list; a constant answer (every other task's key); DataCite's top 25 and 100
  datasets for a short keyword query per task (queries written down before the fetch,
  `analyze.DATACITE_QUERY`; results kept in `baselines/datacite.json`, fetched
  2026-10-16). Round 3: 0.00, 0.00, 0.06, 0.17. A `bare` arm (the model with no way to
  search) runs from round 4 on.
- **Validity, one rule for every arm** (`run.validity`): a run counts only if it
  finished (no timeout, harness error or budget stop), started with exactly its arm's
  built-in tools and server (read from the init event), used a data tool at least once
  (bare excepted) and touched nothing of the benchmark. An invalid run scores recall 0;
  `summary.tsv` also gives the valid-only mean. A finished run is never re-run; a run the
  harness died in is moved to `_aborted/` and logged in `aborted.jsonl`, the same for
  every arm.
- **Leak scan** (`run.leak_hits`): every tool input and output of every run is searched
  for the key and adjudication files, this benchmark's paths (locally and on GitHub,
  `github.com/...` and `raw.githubusercontent.com/...`: the repository is public, so the
  keys are on the web), and other runs' directories. 243 archived runs, 10,010 tool
  blocks: **2 hits**, both scratch files an agent wrote one level above its own run
  directory (r2_066 web R2 r1, r2_0691 tu R5 r2), no key or other run read. They are
  invalid by the rule. Control: without removing a run's own directory, 240 of the 240
  old runs hit (their path names the round), so the scan does see the transcripts.

## History (what changed, why, and what it did to the numbers)

| when | what | why | result then (mean recall dam / web / tu) | archive |
|---|---|---|---|---|
| 10-04 | round 1: 8 "find this dataset, download a file" tasks | first comparison | found 16/16 every arm; bytes 15 / 15 / 12 of 16 | `scores_2026-10-04.json` |
| 10-05 | round 2: 5 "find every dataset" tasks, list scorer, blind adjudication | round 1 was at ceiling | 0.58 / 0.74 / 0.65 | `r2_runs` |
| 10-05 to 10-07 | server changes #244 to #271, each checked on round 2 (reruns) | misses diagnosed from round-2 transcripts | final (0.69.1): 0.85 / 0.75 / 0.58; **tuned on these tasks** | `r2_rerun`, `r2_066`, `r2_0671`, `r2_266_3arm`, `r2_0691` |
| 10-11 | round 3: 5 new held-out tasks, dam frozen at 0.69.1, ToolUniverse 1.5.6 | round 2 was fitted | 0.49 / 0.35 / 0.26 | `r3_runs` |
| 10-12 | #277, #278, #281 fixed on round 3; rerun on 0.71.0 | gap audit | 0.55 / 0.43 / 0.30; **now tuning data** | `r3_071` |
| 10-16 | harness isolation (this PR): pinned PyPI releases with `uvx --exclude-newer`, a fresh `/tmp` directory per run, no user settings, one system prompt, Opus 5.5, provenance per row | runs inherited the developer's settings and a checkout | pilot only | `r4_pilot` |
| 10-16 | fairness fixes (this PR): server arms get `Read`; `bare` arm; ToolUniverse cache per run; no silent re-run; validity rule; leak scan; oracle and version preflight; URL-form ids for every archive, accessions matched exactly | ToolUniverse could not read its own saved oversized results in 24 of 30 runs (r2_0691, r3_runs, r3_071; dam 0 of 30); a timed-out run had been kept as valid | rescore below | |

Rescoring every archived run with the fixed scorer changed **no** row; the only
differences from the committed score files are round 2 R4, whose key grew from 35 to 36
studies through later adjudication (each arm's R4 recall drops by 1/36). The validity
rule made 3 runs invalid: r2_266 web R5 r1 (a timeout, kept as valid before), and the
two leak-scan hits. Mean recall: r2_066 web 0.76 → 0.67, r2_266_3arm web 0.76 → 0.70,
r2_0691 tu 0.67 → 0.58. No run in rounds 2 and 3 holds an unjudged entry; the round-4
pilot holds one (web), so its numbers are pre-adjudication.

Rounds 1 to 3 ran Claude Sonnet 5.5 with a $3 cap and ToolUniverse 1.5.4 (1, 2) or
1.5.6 (3); the dam arm ran from a checkout. Their transcripts are kept outside the repo,
in `~/data-aggregator-mcp-artifacts/h2h/` (`MANIFEST.txt` has their hashes), not in it.

## Reproduce

    python3 benchmarks/h2h/analyze.py --artifacts ~/data-aggregator-mcp-artifacts/h2h \
        --write-to benchmarks/h2h/rescored

Future rounds: build keys and run directories under `~/data-aggregator-mcp-artifacts/`,
outside any checkout; commit the keys' sha256 before the sweep and the keys after it.
