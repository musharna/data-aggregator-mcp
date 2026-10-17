# Head-to-head round 4: a fresh held-out round (keys frozen 2026-10-16)

Round 3's tasks were held out when they ran, but the gap audit that followed diagnosed
and fixed this server on them (#277, #278, #281), so they are now tuning data. Round 4
asks the same kind of question ("find every public dataset that matches a need") on
five new topics, chosen to reach archives rounds 2 and 3 barely touched.

## Tasks

| task | need | scored studies | borderline | archives in the key |
|---|---|---|---|---|
| U1 | human intracranial EEG during natural sleep | 25 | 6 | 12: NEMAR, Zenodo, OpenNeuro, Figshare, Dryad, OSF, SPARC, CRCNS, DANDI and 3 institutional |
| U2 | *Batrachochytrium salamandrivorans* sequencing (backup topic: Tasmanian devil DFTD passed the 30-study cap) | 13 | 1 | NCBI SRA/BioProject/Assembly/TSA, ENA, DDBJ, GEO, Dryad, Figshare |
| U3 | rice drought MS proteomics | 19 | 5 | PRIDE, iProX, Zenodo, NGDC OMIX, Figshare |
| U4 | microplastics in polar and glacier snow and ice | 7 | 9 | 11: PANGAEA, AADC, UK PDC, USAP-DC, IMAS, Mendeley Data, Zenodo, Figshare and 3 more |
| U5 | eye tracking of children reading | 10 | 23 | OSF (22 of 33), DataverseNL, Figshare, Zenodo, Harvard Dataverse, ScienceDB and 5 more |

Each key (`keys/U*.yaml`) was built by one agent per task from each archive's own API,
DataCite and Europe PMC full text, with no access to this server, its code or
ToolUniverse, and committed before any run. `tasks.json` is built from the keys by
`../build_tasks.py --frozen 2026-10-16`; borderline studies are optional.

## Arms and setup

Same driver, prompt frame and scorer as rounds 2 and 3 (`../run.py`), with the
isolation fixes made for this round:

- model `claude-opus-5-5` (rounds 1 to 3 ran Claude Sonnet 5.5; their scores stay as
  they were), Claude Code version recorded in every score row;
- **dam**: `uvx --exclude-newer 2026-10-17T00:00:00Z data-aggregator-mcp==0.74.0`, the
  latest PyPI release on 2026-10-16;
- **tu**: `uvx --exclude-newer 2026-10-17T00:00:00Z tooluniverse==1.5.7`, the latest
  PyPI release on 2026-10-16;
- **web**: no MCP server; web search, web fetch, a shell, Read and Write;
- **bare**: no MCP server and no way to search (Write only): what the model lists from
  memory, the floor every other arm is read against;
- the server arms get Read as well as Write (rounds 1 to 3 gave them Write only, so
  ToolUniverse could not open its own oversized results, which Claude Code saves to a
  file: 24 of 30 ToolUniverse runs in round 2's final run and round 3); neither is a way
  to search;
- every run in a fresh `mktemp` directory under /tmp, with its own ToolUniverse home,
  cache and temp directories (deleted after it), `--setting-sources ""`, the same
  `--system-prompt` in every arm, a $6 budget cap and a 15-minute timeout.

Versions, as `uvx` resolved them on 2026-10-16 (`versions_2026-10-16.json`, every
distribution): dam 0.74.0 with mcp 2.3.0, httpx 0.28.1, pydantic 2.14.1; ToolUniverse
1.5.7 with mcp 1.30.0, fastmcp 3.4.9, httpx 0.28.1. Before any run, `run.py` checks
that each pin still resolves to the version its rows will claim and writes the full list
to the run directory (`versions.json`); it also checks that the scorer gives this key
100% and a wrong list 0%, and stops if either fails.

Plan: 5 tasks x 4 arms x 3 runs = 60 runs, one at a time.

## Pass criterion (stated 2026-10-16, before any run)

Primary endpoint: each task's mean recall over its 3 runs, after blind adjudication, an
invalid run (see `../README.md`, Controls) scoring 0. Primary comparison: dam vs web.

- **dam better**: dam's task mean is above web's on at least 4 of the 5 tasks AND the
  bootstrap 95% interval of the mean per-task difference (dam - web) is above 0.
- **dam worse**: the mirror image.
- **not separated**: anything else. The write-up says so in those words, and then
  reports what does separate the arms (tool calls, cost, wall time), with intervals.

The same verdict, reached on valid runs only, must agree; if it does not, the verdict is
"not separated". Secondary, reported with the same rule but not part of the verdict: dam
vs tu, and each arm vs bare. Also reported: precision, cost, wall time and tool calls,
with intervals, and the model-free baselines. This is a decision rule, not a
significance test: with 5 tasks, 4 of 5 has sign-test p = 0.38 and 5 of 5 p = 0.0625.

**These keys are public**: the repository is public and they were committed (5539039)
before any run, so the web arm can reach them. The leak scan (`run.leak_hits`) reads
every tool input and output for this benchmark's paths on GitHub and on disk; a hit makes
the run invalid. Future rounds commit the keys' hashes before the sweep and the keys
after it.

**OSF was down for scheduled maintenance while the keys were built** (2026-10-16 09:00 to
2026-10-19 09:00 EDT, per the builders; `osf.io` answered 503 at 20:50 EDT). No OSF
file list could be read, which is why most of U5's 23 borderline studies are borderline
(and U1's two OSF studies rest on DataCite metadata). Do not run U5, or the sweep, until
OSF answers again: a run during the outage measures the outage. Re-checking U5's OSF
studies then, before any run, may move some of them between firm and borderline; that
is a pre-run key revision, re-frozen with its date.

## Reproduce

From the repository root:

    python3 benchmarks/h2h/run.py --tasks benchmarks/h2h/round4/tasks.json \
        --out ~/data-aggregator-mcp-artifacts/h2h/r4_runs --reps 3 --workers 1 \
        --arms dam,tu,web,bare

A pilot (U2 only, one run per arm, 2026-10-16, before the Read, bare and cache changes;
its ToolUniverse run used the shared `~/.tooluniverse` cache) proved the harness: all
three arms exited 0 with their server connected (dam 0.74.0 confirmed from the running
interpreter's site-packages), every row records model, Claude Code 2.1.302 and server,
and the list scored (recall dam 0.85, tu 0.85, web 1.00, one web entry unjudged, so
pre-adjudication). The new arm setup has not had an agent run yet: its ToolUniverse
cache isolation was checked by starting ToolUniverse 1.5.7 with the run's variables (the
cache opened in the run's directory). Its runs are in `~/data-aggregator-mcp-artifacts/h2h/r4_pilot/`
and are not part of the results; do not diagnose the server from them before the sweep
is scored.

Raw transcripts stay in `~/data-aggregator-mcp-artifacts/h2h/r4_runs/` (rounds 2 and 3
are alongside it). Rerun with `--score-only` to rescore.

## TODO, after the sweep: adjudicate the pooled entries

`pool.json` (written next to `scores.json`) holds every listed entry the key does not
cover, with no arm attached and in a shuffled order. Each must be judged as an alias of
a keyed study, a new qualifying study (`add`), borderline (`optional`) or `reject`,
into `adjudication.json` and `adjudication_reasons.yaml`, then everything rescored with
`--adjudication`. **The judge for a task must be an agent that did NOT build that
task's key** (round 3's first adjudication was done by the key builders, which its
caveats flag). Use the round-3 rerun's brief as the template: blind to arm, no
data-aggregator or ToolUniverse tools, one verdict per entry with a reason.

Results: TODO, once the sweep has run and been adjudicated.
