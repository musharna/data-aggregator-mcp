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

## Limits

- Five tasks and two runs each: per-task differences of one run are noise; the
  overall order is what holds.
- Keys are complete only as far as the archive searches and the pooled lists reach.
- The bench ran without an NCBI API key, which a real user may set.
- ToolUniverse ran without its optional skills.
