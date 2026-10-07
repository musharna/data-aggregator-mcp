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

## All five tasks on 0.64.0

The dam arm alone, two runs per task on 2026-10-06, on 0.64.0 (#247, #248, #250, #251
and #253 since the re-run above). Same adjudication; nothing listed was left unjudged.
Per-run data: `scores_2026-10-06_064.json`.

| task | 0.60.0 + #244/#245 | 0.64.0 |
|---|---|---|
| R1 | 0.85 / 0.85 | 0.85 / 0.85 |
| R2 | 0.80 / 0.70 | 0.70 / 0.80 |
| R3 | 0.78 / 0.56 | 0.56 / 0.67 |
| R4 | 0.63 / 0.40 | 0.77 / 0.63 |
| R5 | 0.75 / 0.62 | 0.62 / 0.75 |

Median recall 0.72 both times, mean 0.69 → 0.72, precision median 0.86 both, median tool
calls 8 → 11, cost $5.38 → $5.25. R4 gained; no other task moved beyond one run.

Where the 17 keyed studies no run found are held:

- **NGDC (China's GSA and BioProject), 5:** R1 S13 S14, R2 S5 S10, R4 S2. No arm found
  any of them, web included, and NGDC is not a source here. NGDC's own search finds all
  five for the task's words (probed 2026-10-06).
- **NCBI SRA / BioProject, 5:** R3 S3 S8 S9, R4 S36 S38; web found S3, S8 and S36.
- **figshare, 3:** R4 S29 S30 S32, reached here only through DataCite; web found all three.
- **PRIDE, 2:** R5 S7 S8, reached through OmicsDI; web found both.
- **Zenodo S27 and OSF S34,** one each.

## All three arms on 0.66.0

The same five tasks, two runs each, all three arms on 2026-10-06, with the dam arm on
0.66.0 (NGDC as a source, #256; data deposits a paper names, #258). ToolUniverse and the
web arm are unchanged. Two new pooled entries, both near misses the key already named
(R2 GSE284768, R4 figshare 12996977), were rejected; nothing was left unjudged. Per-run
data: `scores_2026-10-06_066.json`.

| task | dam | tu | web |
|---|---|---|---|
| R1 | 0.85 / 0.92 | 0.85 / 0.69 | 0.92 / 0.85 |
| R2 | 1.00 / 1.00 | 0.60 / 0.40 | 0.90 / 0.80 |
| R3 | 0.67 / 0.67 | 0.67 / 0.56 | 0.89 / 0.78 |
| R4 | 0.86 / 0.57 | 0.57 / 0.77 | 0.83 / 0.80 |
| R5 | 0.75 / 0.75 | 0.62 / 0.50 | 0.25 / 0.62 |

| arm | median recall | mean recall | precision | duplicates | median tool calls | median input tokens | cost (10 runs) |
|---|---|---|---|---|---|---|---|
| dam | 0.80 (was 0.72) | 0.80 | 0.86 | 3 | 7.5 | 362k | $5.12 |
| tu  | 0.61 (was 0.59) | 0.62 | 0.87 | 3 | 20.5 | 611k | $5.73 |
| web | 0.81 (was 0.82) | 0.76 | 0.89 | 0 | 15 | 426k | $5.05 |

- **This server now ties web search** (median 0.80 vs 0.81, mean 0.80 vs 0.76) at half
  the tool calls, and is 19 points ahead of ToolUniverse. Both other arms held within
  2 points of the first re-run, so the dam arm's 8 points are not run-to-run drift.
- **NGDC carried R2:** both runs found all 10 studies, including S5 and S10, which this
  server had never found and the earlier runs had found once between them (ToolUniverse,
  2026-10-05). R1 S14 (NGDC) was found here only; web found R1 S13 once.
- Found by this server in some run and by web in none: R1 S14, R4 S2 and S36, R5 S3,
  S4 and S5. The reverse: R1 S13, R3 S9, and R5 S7 and S8 (PRIDE).
- Web still leads on R3 (tardigrades, 0.83 vs 0.67 mean). Its R5 run 1 (0.25) is the
  widest swing of any arm on any task.

## R5 with unshown_terms

OmicsDI showed R5 S8 (PXD055071, "Chlamydomonas nitrogen", rank 22) in both 0.66.0 runs,
and neither listed it: its title and description say "nutrient stress", and the nitrogen
depletion is only in its sample protocol. A hit from a source that matches every query
word now names the words it does not show (`unshown_terms`), and an OmicsDI resolve shows
the protocols (`methods`). The dam arm alone, two runs on 2026-10-07; nothing listed was
left unjudged. Per-run data: `scores_2026-10-07_r5_unshown.json`.

| | 0.66.0 | with unshown_terms |
|---|---|---|
| recall | 0.75 / 0.75 | 0.88 / 0.88 |
| precision | 1.00 / 1.00 | 1.00 / 1.00 |
| tool calls | 6 / 7 | 10 / 17 |
| cost | $0.33 / $0.36 | $0.42 / $0.48 |

Both runs resolved PXD055071 after its hit named "nitrogen" as unshown, and listed it.
S7 (PXD036778) is still missed: its OmicsDI record says "N-starved", never "nitrogen".

## All three arms on 0.67.1

The same five tasks, two runs each, all three arms on 2026-10-07, with the dam arm on
0.67.1 (`unshown_terms`, #261; NCBI paced machine-wide at 2/s, #263). Five pooled entries
were judged (`adjudication_reasons.yaml`): one study the key missed was added (R4 A1, a
Zenodo snow leopard corridor deposit), one is optional (R4 B2) and three were rejected.
The 0.66.0 runs are rescored with the same rulings, so its R4 reads 0.83 / 0.56 here.
Per-run data: `scores_2026-10-07_0671.json` and `scores_2026-10-07_066_rescored.json`.

| task | dam | tu | web |
|---|---|---|---|
| R1 | 0.85 / 0.77 | 0.69 / 0.85 | 0.85 / 0.92 |
| R2 | 0.90 / 0.90 | 0.50 / 0.60 | 0.70 / 0.90 |
| R3 | 0.56 / 0.56 | 0.44 / 0.67 | 0.78 / 0.67 |
| R4 | 0.69 / 0.67 | 0.69 / 0.56 | 0.81 / 0.83 |
| R5 | 0.75 / 0.75 | 0.75 / 0.62 | 0.88 / 0.62 |

| arm | median recall (0.66.0) | mean recall (0.66.0) | precision | duplicates | median tool calls | median input tokens | median wall | cost (10 runs) |
|---|---|---|---|---|---|---|---|---|
| dam | 0.75 (0.79) | 0.74 (0.80) | 0.87 | 2 | 10 | 427k | 136 s | $5.32 |
| tu  | 0.65 (0.61) | 0.64 (0.62) | 0.88 | 1 | 23.5 | 679k | 109 s | $6.57 |
| web | 0.82 (0.80) | 0.80 (0.76) | 0.91 | 0 | 17 | 571k | 180 s | $5.39 |

- **NCBI 429s fell from 13 mentions in 6 dam runs to 2 in 1.** The dam arm's median wall
  rose from 96 s to 136 s: NCBI is now paced, not refused.
- **The dam arm's 5-point drop is not the rate limiter.** Of the 7 studies a 0.66.0 run
  found and no 0.67.1 run did, 6 are outside NCBI (R1 S14 in NGDC; R4 S28, S29, S30 and
  S32 on figshare or Zenodo) and each came up in one of the two 0.66.0 runs only, four
  of them in the same run; no 0.67.1 search returned any of the six, and none timed out. R3 S1 came
  back in a 0.67.1 search and was not listed.
- **R5 S8 held in one run of two.** Both runs saw PXD055071 with "nitrogen" unshown; run 2
  resolved and listed it (and S7), run 1 did not. The 0.88 / 0.88 above was two runs.
- Over both versions (four runs a task), the dam and web arms tie (mean recall 0.77 vs
  0.78) and both lead ToolUniverse (0.63). Run-to-run spread on one task reaches 0.37.

## Deposits first, one hit per deposit (#266)

Web search found the R4 figshare and Zenodo deposits in every run; this server ranked
them behind papers sharing their title words, and page 1, the only page an agent read,
spent 29 of its 50 hits on 7 runs of one SRA study, 6 extra copies of three deposits and
16 Europe PMC paper imports filed as BioStudies studies. #266 ranks deposits first among
equal hits, collapses copies, versions and runs of one study, and files S-EPMC records as
publications. The dam arm alone, two runs on 2026-10-07; two pooled entries, both near
misses the key named, were rejected. Per-run data: `scores_2026-10-07_266.json`.

| task | dam on #266 | dam before (4 runs) | web (4 runs) |
|---|---|---|---|
| R1 | 0.85 / 0.92 | 0.85 0.92 0.85 0.77 | 0.92 0.85 0.85 0.92 |
| R2 | 0.90 / 1.00 | 1.00 1.00 0.90 0.90 | 0.90 0.80 0.70 0.90 |
| R3 | 0.78 / 1.00 | 0.67 0.67 0.56 0.56 | 0.89 0.78 0.78 0.67 |
| R4 | 0.83 / 0.83 | 0.83 0.56 0.69 0.67 | 0.81 0.78 0.81 0.83 |
| R5 | 0.88 / 0.75 | 0.75 0.75 0.75 0.75 | 0.25 0.62 0.88 0.62 |

| | dam on #266 | dam before | web |
|---|---|---|---|
| mean recall | 0.87 | 0.77 | 0.78 |
| median recall | 0.86 | 0.75 | 0.81 |
| precision | 0.87 | 0.87 | 0.90 |
| duplicates (10 runs) | 5 | 2 to 3 | 0 |
| median tool calls | 7.5 | 7.5 to 10 | 15 to 17 |

- **R4:** S28, S29, S30 and S32 in both runs (one run of four before), and S37 for the
  first time. S31 (figshare) was missed in both (four of four before), S5 and S19 in one.
- **R3:** S8 in both runs (never before) and S1 in both (one of four): tardigrade SRA
  runs of one study no longer fill the page.
- The web arm was not re-run; its numbers are the two earlier sets. Two runs a task, so
  a confirming three-arm run is the test of the lead.

## Confirmation: all three arms with the dam arm on #266

The five tasks, two runs each, all three arms on 2026-10-07 ($15.83). Four pooled
entries, all from the web arm, were judged: two near misses rejected, and two paper DOIs
of keyed studies listed without a data accession (R2 S4, S9) made optional, not wrong
and not a find; counted as finds they would raise web's mean to 0.78. Per-run data:
`scores_2026-10-07_266_3arm.json`.

| task | dam | tu | web |
|---|---|---|---|
| R1 | 0.92 / 0.85 | 0.69 / 0.77 | 0.85 / 0.77 |
| R2 | 0.90 / 0.90 | 0.60 / 0.70 | 0.50 / 0.80 |
| R3 | 0.44 / 0.56 | 0.56 / 0.33 | 0.78 / 0.78 |
| R4 | 0.86 / 0.83 | 0.56 / 0.56 | 0.83 / 0.78 |
| R5 | 0.88 / 0.75 | 0.75 / 0.88 | 0.62 / 0.88 |

| arm | mean recall | median recall | precision | median tool calls | median input tokens | median wall | cost (10 runs) |
|---|---|---|---|---|---|---|---|
| dam | 0.79 | 0.85 | 0.87 | 9 | 370k | 87 s | $4.66 |
| tu  | 0.64 | 0.65 | 0.87 | 20 | 589k | 85 s | $5.50 |
| web | 0.76 | 0.78 | 0.93 | 13 | 473k | 159 s | $5.67 |

- **Over every run on each version:** the dam arm on #266 averages 0.83 (20 runs; 0.77
  before), web 0.77 (30) and ToolUniverse 0.63 (30). By task, dam on #266 against web:
  R1 0.88 vs 0.86, R2 0.93 vs 0.77, R3 0.69 vs 0.78, R4 0.84 vs 0.81, R5 0.81 vs 0.65.
- **R3 is still web's.** The dam arm's R3 went 0.78 / 1.00 in the first run on #266 and
  0.44 / 0.56 here: the widest swing in the round, so R3 depends on which searches an
  agent happens to make.
- Web keeps the best precision (0.93 to 0.87).

## R3: NCBI asked for what the plurals add (#269)

R3's study S1 (*Ramazzottius varieornatus*, PRJDB2359) never names "tardigrade", and NCBI
maps only the plural "tardigrades" to the taxon Tardigrada, so no "tardigrade ..." search
reached it: the dam arm found it in 5 of 16 earlier runs, each through a species-name
search. The dam arm alone, R3, two runs per version, 2026-10-07 ($1.88):

| version | recall | S1 | S7 | per-run data |
|---|---|---|---|---|
| plural form as one query | 0.78 / 0.56 | 2 of 2 | 1 of 2 | `scores_2026-10-07_269_one_query.json` |
| plurals as their own stream | 0.56 / 0.67 | 2 of 2 | 2 of 2 | `scores_2026-10-07_269.json` |

- In one query, the 33 runs the plural adds came first (NCBI lists newest first), and
  pushed S7's runs, which name both words, from 1st to 34th: one run lost S7. As a
  separate stream both reach the page; every run found S1 through "tardigrade
  anhydrobiosis".
- Recall did not rise: S3 was missed in both runs (8 of 16 before). No page lost it:
  with the plural stream on and off, S3 sits on the same pages within four places. The
  agents searched *Hypsibius exemplaris*, while S3 is filed under its older name
  *H. dujardini*. Two runs cannot separate this from the agents' choice of searches.

## Limits

- Five tasks and two runs each: per-task differences of one run are noise; the
  overall order is what holds.
- Keys are complete only as far as the archive searches and the pooled lists reach.
- The bench ran without an NCBI API key, which a real user may set.
- ToolUniverse ran without its optional skills.
