# Head-to-head round 3, 2026-10-11: held-out tasks

About ten server changes (#244 to #271) were each diagnosed and checked on round 2's
five tasks, so round 2's final score (dam 0.85, web 0.75, tu 0.67 on 0.69.1) may be
fitted to those tasks. Round 3 asks the same kind of question on five new tasks the
server was never tuned on, chosen to reach archives round 2 barely touched. The
server was frozen at 0.69.1 (main 67ed613) for this run.

Same driver, prompt frame and scorer as round 2 (`../run.py`): Claude Sonnet 5.5,
headless `claude -p`, two runs per task and arm.

- **dam**: this server only, 0.69.1;
- **tu**: ToolUniverse 1.5.6 only (the latest release on 2026-10-11; rounds 1 and 2 ran 1.5.4);
- **web**: no MCP server, only web search, web fetch and a shell.

The answer keys (`keys/T*.yaml`) were built by one subagent per task from each
archive's own API and from papers' data-availability statements, with no access to
either server, and committed (098a4c4) before any run. Borderline studies are
optional. The 50 listed entries the keys did not cover were then judged blind (the
judge did not know which arm listed them) by the subagent that built that task's key
(`adjudication.json`, reasons in `adjudication_reasons.yaml`):

- 1 new qualifying study (T5 A1, a 2026 coral heatwave metabolomics deposit on Zenodo);
- 11 copies of keyed studies;
- 3 borderline;
- 35 did not qualify.

A second study the key had missed, T5 A2 (an NGDC/OMIX coral heat-stress metabolome,
PRJCA028562), was listed by the dam rerun on #278 and judged the same way afterwards. No
round-3 run listed it, so it lowers every arm's T5 recall by 21/22; the scores below
include it.

Per-run data: `scores_2026-10-11.json`. Raw transcripts are kept outside the repo, in
`~/data-aggregator-mcp-artifacts/h2h/r3_runs/` (and `r3_071/` for the 0.71.0 rerun below).
**Rescored 2026-10-16** (fixed scorer, one validity rule, leak scan, intervals): no row
changed; dam vs web is not separated (4 of 5 tasks, sign test p = 0.38). See
`../README.md` and `../summary.tsv`.

| task | need | studies in key | dam | tu | web |
|---|---|---|---|---|---|
| T1 | mouse olfactory bulb recordings during odors | 25 | 0.36 / 0.36 | 0.16 / 0.16 | 0.28 / 0.20 |
| T2 | human tendon/ligament single-cell RNA-seq | 22 | 0.64 / 0.91 | 0.41 / 0.46 | 0.50 / 0.55 |
| T3 | methane from thermokarst lakes and ponds | 27 | 0.59 / 0.63 | 0.26 / 0.30 | 0.26 / 0.26 |
| T4 | labeled honey bee images for ML | 26 | 0.35 / 0.27 | 0.12 / 0.08 | 0.31 / 0.23 |
| T5 | coral heat-stress metabolomics | 22 | 0.18 / 0.68 | 0.36 / 0.36 | 0.50 / 0.45 |

| arm | mean recall | median recall | precision | duplicates | median tool calls | median input tokens | cost (10 runs) |
|---|---|---|---|---|---|---|---|
| dam | 0.50 | 0.48 | 0.82 | 1 | 12 | 505k | $5.56 |
| tu | 0.27 | 0.28 | 0.97 | 1 | 28 | 789k | $6.13 |
| web | 0.35 | 0.29 | 0.89 | 0 | 20 | 230k | $4.44 |

## What it shows

- **The lead holds on held-out tasks.** dam has the highest mean recall on 4 of the 5
  tasks and overall (0.50 vs web 0.35), meeting the round's test (dam's mean recall at
  or above web's). The gap is widest on T3 (0.61 vs 0.26), where the studies sit in
  PANGAEA, the Arctic Data Center, Nordicana D and ORNL DAAC.
- **All arms score far lower than in round 2.** These keys are larger (22 to 27 scored
  studies, against 8 to 13 for four of round 2's five tasks) and were built to be
  exhaustive, so no arm comes close to them.
- **T5 is the exception, and dam's runs disagree with each other.** dam scored 0.18 in
  one run (6 tool calls) and 0.68 in the other (24); web scored about 0.50 both times.
  dam's mean is 0.43, against web's 0.48.
- **dam lists more entries that don't qualify.** Its precision is 0.82, against 0.89
  for web and 0.97 for tu; 30 of dam's 159 entries were rejected.
- **T4 has a ceiling for dam.** 7 of the 26 scored studies are only on Kaggle, which
  this server does not search, so dam's T4 recall cannot pass 0.73. Web found 1 of
  those 7.
- **12 studies were found by web and never by dam**, among them T2 S2 (a tendon atlas held only in
  CELLxGENE and HCA, also found by tu in both runs) and T5 S3 and S4 (both in
  Metabolomics Workbench, found by web and tu in both runs). Diagnosing them comes after this commit, under the held-out rule.

## Caveats

- Two runs per cell; dam's T2 and T5 runs differ by 0.27 and 0.50.
- Each key's builder also judged the pooled entries for that key. Judging was blind to
  arm, and it did add two studies the key had missed (T5 A1, A2), but it is not an independent
  second judge.
- The T3 key was first built on the backup topic (microplastics in lake sediment cores,
  3 firm studies) because the primary passed the 30-study cap; it was rebuilt on the
  primary, with a 40-study cap, before any run.

## Rerun on 0.71.0, 2026-10-12

The same five tasks, three arms and two runs, after the fixes the gap audit led to: CELLxGENE
reads queries as the rest of the server does (#277), and the Metabolomics Workbench
(#278) and MassIVE (#281) are searched natively. ToolUniverse and the web arm were
unchanged. The 53 new entries the rerun listed were judged blind in the same way, by
three judges who did not build the keys: 3 new qualifying studies (T3 A1, T4 A1, T4 A2),
1 borderline (T4 B2), 22 copies of keyed studies and 27 that did not qualify. Both days
are scored on the enlarged key, so the 0.69.1 numbers below are a little lower than in
the table above (`scores_2026-10-11_rescored.json`, `scores_2026-10-12_071.json`).

| task | dam 0.69.1 → 0.71.0 | tu | web |
|---|---|---|---|
| T1 | 0.36 → 0.48 | 0.16 → 0.12 | 0.24 → 0.32 |
| T2 | 0.77 → 0.77 | 0.43 → 0.39 | 0.52 → 0.48 |
| T3 | 0.59 → 0.61 | 0.27 → 0.39 | 0.25 → 0.27 |
| T4 | 0.29 → 0.25 | 0.09 → 0.23 | 0.27 → 0.39 |
| T5 | 0.43 → 0.66 | 0.36 → 0.36 | 0.48 → 0.70 |
| **mean** | **0.49 → 0.55** | **0.26 → 0.30** | **0.35 → 0.43** |
| precision | 0.82 → 0.86 | 0.97 → 0.89 | 0.89 → 0.82 |

- **dam still leads, but the gain is not separable from day-to-day variation.** The two
  arms that did not change moved by +0.04 (tu) and +0.08 (web), about as much as dam
  (+0.07). With two runs per cell, this rerun cannot credit the fixes with a number.
- **T5 is where the fixes aimed.** dam found S4 (Workbench) and S15 (MassIVE) in both
  runs and S3 (Workbench) in one, none of which it found on 0.69.1; its T5 recall rose
  from 0.43 to 0.66. web's rose about as much (0.48 to 0.70).
- **NCBI was failing during the rerun.** Its taxonomy efetch answered about half of all
  requests with HTTP 400 and a garbled body (11 of 20 plain requests at 17:08 EDT), so 8 of
  dam's 10 runs carried `errors.taxonomy` and searched without organism resolution, so
  dam's 0.71.0 numbers are a lower bound. 7 of tu's 10 runs also called NCBI; whether
  they were hit was not checked.
- A local mutation run used 8 of 22 cores for most of the rerun, so its wall times are
  not comparable with round 3's.
