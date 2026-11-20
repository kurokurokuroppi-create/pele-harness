# pele-improved: plan → work → deterministic check → verify/fix, with MCP

An improved harness built from the official `minimal` and `plan-verify`
samples, keeping their `taskbridge.py` protocol layer, retry handling,
Claude cache marking, and wrap-up behaviour. It calls the Google-sponsored
`google-cloud/gemini-3.8-flash` model (unchanged from the starting
skeleton), so it needs no API key.

## How it works

`solve(session)` in `serve.py` runs four phases per task (tasks allow up
to 6 hours; budgets are shares of `session.time_left()`):

1. **Planner** (`bash`, `read_file` only; wraps up after 10% of the task
   time): inspects the files and writes a plan — each deliverable with its
   exact path and format, contents, method, hard constraints, and how to
   verify it.
2. **Worker** (full tools; wraps up when 25% of the time remains):
   executes the plan. Tools are `bash` (with a per-call `timeout_sec`),
   `read_file`, `write_file` (with `append`), `edit_file` (exact-match
   replace), plus the task's MCP tools (below). It is instructed to write
   a first version of each deliverable early and to re-open every
   deliverable before finishing.
3. **Deterministic check** (no model call): absolute paths mentioned in
   the plan / worker summary are tested inside the task container for
   existence and non-zero size. Missing or empty files go straight into
   a fix round (phrased as "confirm or repair", since a mentioned input
   path can false-positive).
4. **Verifier** (fresh conversation, `bash` + `read_file` only): checks
   the deliverables against the task and replies `PASS` or a numbered
   list of gaps. Gaps go back to the worker for a fix round. Verifier +
   deterministic fixes share a budget of `MAX_FIX_ROUNDS` (default 2);
   a step only starts with at least 2 minutes before its wrap-up.

### MCP tools

Servers from the task message are discovered with
`taskbridge.MCPClient` (`initialize` → `tools/list`). A small catalog is
offered directly as `<server>__<tool>` functions; a large one (over
40,000 JSON characters, or over the function cap) is listed by name in
the system prompt and reached through `mcp_find` / `mcp_call`, so a
~500 KB catalog never enters every request. Discovery failures are
logged and the task continues without that server. The planner and
verifier do not get MCP tools (their read-only guarantee would be
unverifiable, since an MCP tool's side effects are unknown).

### Context and failure handling (inherited + extended)

- Transient model failures (408/409/429/5xx, dropped connections) retry
  with backoff (5–120 s) without polluting the conversation; a
  context-length 400 first shortens old tool results, then compacts.
- Proactive context care: past ~400k conversation characters, old tool
  results are shortened; past ~600k, the middle of the conversation is
  replaced by model-written notes (system prompt, original task, and the
  last 8 messages are kept, with orphan tool calls/results sanitized).
- Empty or length-truncated replies are nudged up to 3 times; every
  non-empty worker reply updates `session.final_message`, so a cancel
  still delivers the latest summary. Assistant turns are sent back via
  `model_dump()` so provider fields (Gemini thought signatures) survive.
- Only `tool_choice: "auto"` / `"none"` is used, and no temperature,
  thinking budget, or provider-hosted tools are ever sent (Claude and
  the model gateway reject those with a 400).

## Files

| File | Purpose |
|---|---|
| `serve.py` | Agent phases, tools, MCP layer, context management |
| `taskbridge.py` | Serve protocol implementation (official, unmodified) |
| `requirements.txt` | Pinned `openai` and `websockets` dependencies |
| `submission.yaml` | Model declaration (`google-cloud/gemini-3.8-flash`), build and serve commands |

## Configuration (`env:` in `submission.yaml` or the environment)

`REASONING_EFFORT`, `MAX_OUTPUT_TOKENS` (default 32000), `PLAN_SHARE`
(0.1), `CHECK_SHARE` (0.25), `MAX_FIX_ROUNDS` (2), `COMPACT_AT_CHARS`
(400000).

## Status

`kit check` passed on 2026-11-17 (validate / files / build / model /
listen / smoke / mcp) in a local Docker environment, using a locally
hosted substitute model routed under the declared model's name; the
declared model itself (`google-cloud/gemini-3.8-flash`) has not been
called yet. `artifacts/` holds the dev-set runs from the local
substitute model: 23 of 29 tasks usable, 3 empty, 3 missing.
See `../kit-check-20261117.md` for the check record and
`../harness-design.md` for the design record and unverified items.
