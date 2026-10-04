# Head-to-head, 2026-10-04: data-aggregator-mcp vs ToolUniverse

The same agent (Claude Sonnet 5.5, headless `claude -p`) was given eight "find this
dataset and download one file" tasks, run twice each, with one of three tool sets:

- **dam**: this server only (branch `fix/match-tiers`, which includes #238 and #239);
- **tu**: ToolUniverse 1.5.4 only, run with the stdio config from its README
  (compact mode, no skills);
- **web**: no MCP server, only web search, web fetch and a shell (the control arm).

The server arms had no built-in tools, so the server was their only way to search or
download. `run.py` scores each run from the session log and the files on disk:
- **found**: the reply names the dataset's ID;
- **bytes**: a saved file matches a sha256 computed in advance.

The answer key (`tasks.json`, provenance in `answer_key_provenance.yaml`) was built with
curl against each archive's own API, not through either server. Per-run data:
`scores_2026-10-04.json`.

| arm | found | bytes | median tool calls | median input tokens | cost (16 runs) |
|---|---|---|---|---|---|
| dam | 16/16 | 15/16 | 4.5 | 130k | $1.97 |
| tu  | 16/16 | 12/16 | 8   | 212k | $3.22 |
| web | 16/16 | 15/16 | 3   | 76k  | $1.84 |

Bytes by task (two runs each):

| task | archive | dam | tu | web |
|---|---|---|---|---|
| T1 | Zenodo | ✓✓ | ✓✓ | ✓✓ |
| T2 | Figshare | ✓✓ | ✗✗ | ✓✓ |
| T3 | GEO | ✓✗ | ✓✓ | ✓✓ |
| T4 | ArrayExpress | ✓✓ | ✓✓ | ✓✓ |
| T5 | OpenML | ✓✓ | ✓✓ | ✓✓ |
| T6 | Harvard Dataverse | ✓✓ | ✓✓ | ✓✓ |
| T7 | paper → Dryad | ✓✓ | ✗✗ | ✗✓ |
| T8 | RCSB PDB | ✓✓ | ✓✓ | ✓✓ |

## What it shows

- Against ToolUniverse, this server got more files (15 vs 12) with about half the tool
  calls and 40% fewer input tokens.
  - ToolUniverse saved a 0-byte file for the Figshare CSV in both runs.
  - It found the Dryad dataset but couldn't get the bytes, because Dryad refuses
    anonymous downloads.
  - This server got T7 both times; the same bytes are on Zenodo.
- **The control arm matched this server on bytes and beat it on calls and tokens.**
  These tasks describe one dataset precisely, and a web search finds that dataset by
  its description. Finding a dataset you can already describe is not where this server
  adds value.
- This server's one miss was a bug in it. The agent re-ran `fetch` with
  `max_bytes=1000` after a good download. The failed second fetch truncated the good
  file and then deleted it, because `fetch` writes directly over the target path.
- This server's six tool definitions add about 4k tokens per turn over the control
  arm's built-in tools (first turn 21.0k vs 17.1k). Most of the extra input comes from
  what the tools return.

## Limits

- Eight tasks and two runs each: differences of one or two runs are noise.
- Every task has one known answer that its description pins down. None asks for what
  a search across archives is for: every dataset matching a need, deduplicated and
  checked. A second round should.
- ToolUniverse ran without its optional skills, and its Claude Code plugin may do
  better than the bare server.
- The key's files can change: GEO and wwPDB re-release files (`answer_key_provenance.yaml`).
