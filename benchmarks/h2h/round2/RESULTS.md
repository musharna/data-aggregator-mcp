# Head-to-head round 2, 2026-10-05: find every dataset that matches a need

Round 1 asked for one dataset its description pinned down, and plain web search tied
this server. Round 2 asks for every public dataset that matches a need, across
archives, which is what a search across archives is for.

Five tasks (`tasks.json`), two runs each, three arms, same agent (Claude Sonnet 5.5,
headless `claude -p`):

- **dam**: this server only, 0.60.0;
- **tu**: ToolUniverse 1.5.4 only (stdio config from its README, no skills);
- **web**: no MCP server, only web search, web fetch and a shell.

The server arms had no built-in tools except Write, for the answer file. The agent
writes `datasets.json` (one entry per study, copies in other archives under
`same_as`), and `run.py` scores it:

- **recall**: studies in the key the list names, over studies in the key;
- **precision**: listed entries that qualify, over listed entries that were judged;
- **duplicates**: one study listed as two or more separate entries.

The answer key (`keys/R*.yaml`) was built per task from each archive's own API, not
through any arm, before any run. Borderline studies are optional: listing one is not
wrong and missing one is not a miss. Every entry an arm listed that the key did not
cover (29) was judged afterwards from the archive record, without knowing which arm
listed it (`adjudication.json`, reasons in `adjudication_reasons.yaml`): none was a
new qualifying study, 4 were copies of keyed studies, 3 were borderline, 22 did not
qualify. Per-run data: `scores_2026-10-05.json`.

| task | need | studies in key | dam | tu | web |
|---|---|---|---|---|---|
| R1 | *Phelipanche aegyptiaca* sequencing | 13 | 0.77 / 0.85 | 0.85 / 0.77 | 0.85 / 0.85 |
| R2 | axolotl limb regeneration single-cell | 10 | 0.70 / 0.60 | 0.70 / 0.40 | 0.80 / 0.70 |
| R3 | tardigrade desiccation omics | 9 | 0.67 / 0.67 | 0.56 / 0.67 | 0.78 / 0.56 |
| R4 | wild snow leopard field data | 35 | 0.37 / 0.49 | 0.60 / 0.77 | 0.89 / 0.89 |
| R5 | *Chlamydomonas* N-starvation proteomics | 8 | 0.50 / 0.25 | 0.50 / 0.75 | 0.62 / 0.50 |

| arm | median recall | precision | duplicates | median tool calls | median input tokens | cost (10 runs) |
|---|---|---|---|---|---|---|
| dam | 0.63 | 0.84 | 2 | 10 | 411k | $4.74 |
| tu  | 0.68 | 0.92 | 1 | 23 | 617k | $7.45 |
| web | 0.79 | 0.89 | 4 | 14.5 | 380k | $4.59 |

## What it shows

- **This server found the fewest datasets.** Web search found the most, at about the
  same cost. Before the runs, a web arm within 10 points of this server was set as the
  sign that cross-archive discovery is not where it adds value as shipped. Web is 16
  points ahead.
- The loss is in what the agent saw, not in duplicates: all arms kept duplicates low.
- The logs show why. Of this server's 95 tool calls:
  - **9 returned more than Claude Code accepts from one tool** (57k to 427k
    characters for a 30 to 50 hit page, about 1.5k to 8.5k characters per hit). The
    result was saved to a file the agent had no tool to read, so the page was lost.
    Both R4 runs lost their first, broadest search this way. ToolUniverse: 3 of 282.
  - **12 searches returned nothing with no error.** All were limited to the omics
    sources, where NCBI and OmicsDI require every word: "tardigrade dehydration"
    finds 3 studies, "tardigrade dehydration tun" finds 0, and the response does not
    say why.
  - 3 searches came back empty because the `kind` filter, applied after fetching,
    removed the whole page; 3 more hit NCBI rate limits or an OmicsDI timeout.

## Re-run with the first two fixes

The same tasks, runs and arms, the same day, with the dam arm on 0.60.0 plus #244
(a search page fits in one tool result) and #245 (OmicsDI filters and pages upstream;
`errors.all_words` names an every-word source that matched nothing). The 12 new
pooled entries were judged blind the same way: 3 copies of keyed studies, 9 did not
qualify. Per-run data: `scores_2026-10-05_rerun.json`.

| task | dam | tu | web |
|---|---|---|---|
| R1 | 0.85 / 0.85 | 0.85 / 0.85 | 0.85 / 0.85 |
| R2 | 0.80 / 0.70 | 0.60 / 0.40 | 0.80 / 0.80 |
| R3 | 0.78 / 0.56 | 0.56 / 0.78 | 0.67 / 0.78 |
| R4 | 0.63 / 0.40 | 0.63 / 0.57 | 0.89 / 0.86 |
| R5 | 0.75 / 0.62 | 0.50 / 0.38 | 0.62 / 0.88 |

| arm | median recall | precision | duplicates | median tool calls | median input tokens | cost (10 runs) |
|---|---|---|---|---|---|---|
| dam | 0.72 (was 0.63) | 0.85 | 2 | 8 | 414k | $5.38 |
| tu  | 0.59 (was 0.68) | 0.89 | 2 | 23 | 714k | $6.42 |
| web | 0.82 (was 0.79) | 0.95 | 3 | 14 | 491k | $5.15 |

- **No result was lost to size.** This server's results saved to a file fell from 9 to
  0; every page of every run reached the agent. ToolUniverse: 29.
- **The every-word note was read.** `errors.all_words` appeared in all 10 dam runs. R5
  (OmicsDI's task) rose from 0.38 to 0.69 on average.
- **Web is still ahead by 10 points, almost all of it on R4.** Web found 10 snow
  leopard studies that this server found in neither run. Probing the fixed server
  afterwards, per-source searches paged to 125–240 hits reach 8 of them (DataCite:
  4 for "snow leopard", 2 more for "Panthera uncia"; NCBI omics: 2). The default
  all-source search, paged to 201 hits of 47,971, reaches 1. The agent made 8 calls
  per run and did not search source by source or page that deep. Two (Figshare)
  were not reached at all.
- **ToolUniverse fell 9 points** on the same tasks, a measure of run-to-run noise
  with two runs per task.

## R4 after #247 and #248

R4 alone, dam arm only, four runs on main 85efa15 (released as 0.62.0), on
2026-10-06. Two fixes went in:

- #247: the query as written outranks its words scattered, and DataONE copies merge
  with their deposits.
- #248: sources that match words exactly are sent each word with its plural.

Scored with the same adjudication; every new entry had already been judged.
Per-run data: `scores_2026-10-06_r4.json`. The other arms did not change.

| | runs | recall | median recall | median precision | web-only studies found | cost |
|---|---|---|---|---|---|---|
| before | 2 | 0.63 / 0.40 | 0.51 | 0.88 | 0 / 0 | $0.97 |
| after | 4 | 0.74 / 0.60 / 0.66 / 0.46 | 0.63 | 0.94 | 4 / 2 / 3 / 1 | $2.18 |

- **Some of the web-only studies are now found.** Five of the 10 appeared across the
  four runs: S26, S29, S32, S33 and S36. The two Figshare studies (S29, S32) are among
  them, reached through the plural form.
- **The agent still stops early.** Each run made 6–8 calls and listed 17–28 studies.
  A 6-page dataset search reaches 7 of the 10 web-only studies, so depth per run, not
  reach, is now the limit. Web (0.89 / 0.86) is still ahead on this task.

## R4 after #250 and #251

R4 alone, dam arm only, four runs each on 2026-10-06:

- **#250:** a hit leaves out its empty fields, so a page holds 44–50 hits instead of 30–36. Run on main 58e71b7.
- **#251:** `errors.next_page` counts the fetched hits not yet sent that name every query word. Run on that branch, released as 0.63.0.

Scored with the same adjudication. Per-run data: `scores_2026-10-06_r4_dense.json` and `scores_2026-10-06_r4_next.json`.

| | recall | median / mean | keyed studies seen per run | distinct hits seen per run | cost |
|---|---|---|---|---|---|
| 0.62.0 | 0.74 / 0.60 / 0.66 / 0.46 | 0.63 / 0.61 | 22.75 | 127 | $2.18 |
| + #250 | 0.69 / 0.57 / 0.77 / 0.49 | 0.63 / 0.63 | 23.5 | 150 | $2.06 |
| + #251 | 0.74 / 0.77 / 0.57 / 0.77 | 0.76 / 0.71 | 27 | 182 | $2.26 |

- **Fuller pages alone did nothing.** The agents saw more hits, but few of the extra ones were snow leopard studies.
  - The agents list nearly every keyed study they are shown: 1–4 were missed per run.
  - Studies thin out after page 1: six pages of "snow leopard" hold 27 of the 35, 16 of them on page 1.
- **The note made the agents page.** Every #251 run asked for a next page once, against under half the runs before. The next page held studies they had not seen.
- Four runs per arm: the gain is about one run's spread. It matches the transcripts, but it is not a significance test.
- **Still behind web, and still out of reach:** web stays ahead (0.89 / 0.86). Four studies (S27, S28, S31, S34) appear in no six-page plain search. One #250 entry, Zenodo 17695964, is unjudged and left out of precision.

## R4 after #253

#253 asks DataCite and Zenodo for title matches as a request of their own, so a deposit
naming "snow leopard" in its title enters the fetched window however deep the source's
own order puts it. R4 alone, dam arm, four runs on that branch on 2026-10-06, same
adjudication. Per-run data: `scores_2026-10-06_r4_title.json`.

| | recall | median / mean | calls per run | cost |
|---|---|---|---|---|
| 0.63.0 | 0.74 / 0.77 / 0.57 / 0.77 | 0.76 / 0.71 | 6 / 7 / 7 / 8 | $2.26 |
| + #253 | 0.71 / 0.60 / 0.80 / 0.69 | 0.70 / 0.70 | 5 / 5 / 6 / 8 | $1.97 |

- **Recall did not move.** The difference is within one run's spread.
- **The studies it targeted came in:** S14 in 4 of 4 runs (was 0), S28 in 3 (was 0), S31
  in 3 (was 1). Nine others were found in one or two fewer runs.
- **Not crowding:** the default "snow leopard" search, 6 pages of 50, reaches 12 / 20 / 23 /
  24 / 27 / 30 keyed studies by page with #253 and 11 / 18 / 22 / 26 / 26 / 30 without;
  the 30 differ by four studies each way.
- Zenodo now returns S27 and S28 on page 3 of a Zenodo search; neither was in its first
  8 pages. The agents read one or two pages, so what bounds recall now is how far they
  read, not what a search can reach.

## Limits

- Five tasks and two runs each: per-task differences of one run are noise; the
  overall order is what holds.
- Keys are complete only as far as the archive searches and the pooled lists reach.
- The bench ran without an NCBI API key, which a real user may set.
- ToolUniverse ran without its optional skills.
