# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Improved PELE harness: plan -> work -> deterministic check -> verify/fix.

Built from the official `minimal` and `plan-verify` samples (same
`taskbridge.py`, same OpenAI Chat Completions loop, same retry / cache /
wrap-up handling) with four additions:

1. Three roles in separate conversations (planner / worker / verifier),
   as in `plan-verify`, with per-step time budgets taken from
   `session.time_left()` (tasks allow up to 6 hours).
2. Task MCP tools: servers listed in the task message are discovered via
   `taskbridge.MCPClient` and offered to the worker (directly as function
   tools when the catalog is small, otherwise through `mcp_find` /
   `mcp_call`). `minimal` / `plan-verify` ignore MCP servers entirely.
3. A deterministic (no-LLM) deliverable check between the worker and the
   verifier: paths mentioned in the plan / worker summary are tested for
   existence and non-zero size inside the task container, and missing or
   empty files go straight into a fix round.
4. Proactive context management: old tool results are shortened before the
   context is full, and a very long conversation is compacted into model
   written notes (the reactive cut on a context-length 400 is kept too).

Only function tools that run inside the task container are used; no
provider-hosted tools are ever requested. `tool_choice` is only ever
"auto" or "none" (Claude rejects a forced tool choice with a 400).
"""

import asyncio
import json
import os
import re
import shlex

import openai
import taskbridge

MODEL = os.environ["MODEL_NAME"]
# The model's highest reasoning level: max for Claude, GLM, GPT-6 and
# DeepSeek-V4, high for the others (Gemini refuses max). REASONING_EFFORT
# overrides it. No temperature / top_p / thinking budget is ever sent:
# Claude Opus rejects those with a 400 (docs/models.md).
MAX_EFFORT_MODELS = ("claude", "glm", "gpt-6", "deepseek-v4")
EFFORT = os.environ.get("REASONING_EFFORT") or (
    "max" if any(m in MODEL.lower() for m in MAX_EFFORT_MODELS) else "high"
)
WRAP_UP_SEC = 240  # a step wraps up this long before its own deadline
WRAP_UP = {
    "role": "user",
    "content": "Time is up for this step. Stop and reply with your answer.",
}
RESULT_CHARS = 20_000  # a longer tool result loses its middle
RETRY_WAITS = (5, 15, 30, 60, 120)  # after the SDK's own quick retries
CONTEXT_FULL = re.compile(
    r"context.?(length|window)|prompt is too long|maximum prompt length"
    r"|exceed[^.]{0,40}(token|context)",
    re.I,
)
CUT_KEEP = 1000  # characters a tool result keeps once shortened
MAX_NUDGES = 3  # consecutive empty / cut-off replies tolerated
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "32000"))
CACHE_MARK = "claude" in MODEL.lower()

# Phase budgets (shares of the task's total time, as in plan-verify).
PLAN_SHARE = float(os.environ.get("PLAN_SHARE", "0.1"))
CHECK_SHARE = float(os.environ.get("CHECK_SHARE", "0.25"))
MIN_STEP_SEC = 120  # a verify / fix step starts only with this much room
MAX_FIX_ROUNDS = int(os.environ.get("MAX_FIX_ROUNDS", "2"))

# Context management: shorten old tool results once the conversation passes
# COMPACT_AT_CHARS characters (~4 chars per token), and compact into notes
# once it passes COMPACT_HARD_CHARS even after shortening.
COMPACT_AT_CHARS = int(os.environ.get("COMPACT_AT_CHARS", "400000"))
COMPACT_HARD_CHARS = int(COMPACT_AT_CHARS * 1.5)
KEEP_RECENT = 8  # trailing messages kept verbatim across a compaction

# MCP: offer tools directly while their definitions stay under this many
# JSON characters and the provider's 128-function cap is respected.
DIRECT_MCP_CHARS = 40_000
MAX_TOOLS = 120

# The evaluator sets OPENAI_BASE_URL and OPENAI_API_KEY for every model.
# Non-streaming calls can take >15 min at max effort before any bytes
# arrive, so the timeout matches the egress proxy's 30-minute idle limit.
client = openai.AsyncOpenAI(max_retries=5, timeout=1800)

PLANNER = """You plan an engineering task that another engineer will carry out in a Linux container with no internet \
access. Inspect the task's files with your tools (bash starts in {workdir}), but do not create or change any file. \
Then reply with the plan: each deliverable with its exact path and format, what it must contain, the data and method \
to use, the constraints that must hold (including any hard / safety constraints and how the task says the result is \
checked), and how to verify each deliverable."""

WORKER = """You are an expert engineer working on a task in a Linux container with no internet access. Your tools \
act inside it, and bash starts in {workdir}.{mcp} A plan for the task follows it; use it, and correct it where the \
files show it is wrong. Work in small, verified steps: compute with code, not by hand, and check units. Write a \
first version of each deliverable early, then improve it. Write each deliverable exactly where and in the format \
the task asks; produce office / binary formats with code so they are real files of that format. Before you finish, \
open every deliverable again and check that it exists, is non-empty, readable, and complete. When you are done, \
reply without calling a tool: a short summary of each deliverable, its exact path, and the key results."""

VERIFIER = """You check an engineer's work on a task in a Linux container. You get the task, the plan, and the \
engineer's summary of what was delivered. Open every deliverable with your tools (bash starts in {workdir}) and \
check it against the task: the path, the format, every part the task asks for, and the key numbers, recomputed. \
Do not create or change any file. If everything holds, reply with the single word PASS and nothing else. Otherwise \
reply with a numbered list of the gaps, each specific enough to fix."""

SUMMARY_SYSTEM = """You compress a working conversation into notes. Reply with the notes only."""


def tool(name, description, required, **properties):
  """Builds an OpenAI function-tool definition dictionary."""
  return {
      "type": "function",
      "function": {
          "name": name,
          "description": description,
          "parameters": {
              "type": "object",
              "properties": properties,
              "required": required,
          },
      },
  }


_STR = {"type": "string"}
TOOLS = [
    tool(
        "bash",
        "Run a bash command in the task container and return its exit code "
        "and output. Each call starts a fresh shell in the working "
        "directory: only files persist between calls.",
        ["command"],
        command=_STR,
        timeout_sec={
            "type": "integer",
            "description": "Stop after this many seconds (default 120, "
            "max 600).",
        },
    ),
    tool("read_file", "Read a text file.", ["path"], path=_STR),
    tool(
        "write_file",
        "Create or overwrite a file; parent folders are created. Use "
        "append=true to write a large file in parts.",
        ["path", "content"],
        path=_STR,
        content=_STR,
        append={"type": "boolean"},
    ),
    tool(
        "edit_file",
        "Replace exact text in a file. old_text must occur exactly once "
        "unless replace_all is true.",
        ["path", "old_text", "new_text"],
        path=_STR,
        old_text=_STR,
        new_text=_STR,
        replace_all={"type": "boolean"},
    ),
]
READ_ONLY = TOOLS[:2]  # planner and verifier: bash + read_file only
MCP_TOOLS = [
    tool(
        "mcp_find",
        "Show the description and parameters of the task's own tools "
        "matching a query (a tool name, or words describing the need).",
        ["query"],
        query=_STR,
    ),
    tool(
        "mcp_call",
        "Run one of the task's own tools, named as listed in the system "
        "prompt (for example apps__list_files).",
        ["tool", "arguments"],
        tool=_STR,
        arguments={"type": "object"},
    ),
]


def clip(text, limit=RESULT_CHARS):
  """Middle-truncates `text` to at most `limit` characters."""
  if len(text) <= limit:
    return text
  half = limit // 2
  return (
      f"{text[:half]}\n[... {len(text) - limit} characters cut ...]\n"
      f"{text[-half:]}"
  )


def marked(messages):
  """Returns `messages` with Claude cache_control marks, or unchanged.

  Claude caches a prompt prefix only at marked breakpoints; the system
  prompt and the newest message are marked (2 of the 4 allowed). The
  stored conversation is never modified.
  """
  if not CACHE_MARK:
    return messages
  out = list(messages)
  for i in {0, len(out) - 1}:
    m, content = out[i], out[i].get("content")
    if isinstance(content, str) and content:
      out[i] = {
          **m,
          "content": [{
              "type": "text",
              "text": content,
              "cache_control": {"type": "ephemeral"},
          }],
      }
    else:
      out[i] = {**m, "cache_control": {"type": "ephemeral"}}
  return out


# ---------------------------------------------------------------- MCP layer


class MCPRegistry:
  """Discovers the task's MCP tools and dispatches calls to them.

  Direct mode (small catalog): each MCP tool becomes one function named
  `<server>__<tool>`. Catalog mode (large catalog): only `mcp_find` /
  `mcp_call` are exposed and the tool names are listed in the worker's
  system prompt, so a ~500 KB catalog never enters every request.
  """

  def __init__(self, session):
    self.session = session
    self.clients = {}  # server name -> taskbridge.MCPClient
    self.by_func = {}  # function name -> (client, original tool name, tool)
    self.direct = False
    self.catalog_text = ""

  @staticmethod
  def _func_name(server, tool_name, taken):
    base = re.sub(r"[^A-Za-z0-9_-]", "_", f"{server}__{tool_name}")[:64]
    name, n = base, 1
    while name in taken:
      n += 1
      name = f"{base[:63 - len(str(n))]}_{n}"
    return name

  async def discover(self):
    """Initializes every MCP server; failures are logged, never fatal."""
    for spec in self.session.mcp_servers:
      name, url = spec.get("name"), spec.get("url")
      if not name or not url:
        continue
      mcp = taskbridge.MCPClient(
          self.session, url, client="pele-harness", timeout_sec=120
      )
      try:
        await mcp.initialize()
        tools = await mcp.list_tools()
      except (taskbridge.TaskError, taskbridge.MCPError, ValueError) as exc:
        await self.session.log(f"MCP server {name} unavailable: {exc}")
        continue
      self.clients[name] = mcp
      for t in tools:
        func = self._func_name(name, t["name"], self.by_func)
        self.by_func[func] = (mcp, t["name"], t)
      await self.session.log(f"MCP server {name}: {len(tools)} tools")
    if not self.by_func:
      return
    functions = [self._definition(f) for f in self.by_func]
    if (
        len(json.dumps(functions)) <= DIRECT_MCP_CHARS
        and len(TOOLS) + len(functions) <= MAX_TOOLS
    ):
      self.direct = True
    else:
      servers = {}
      for func, (_, _, t) in self.by_func.items():
        server = func.split("__", 1)[0]
        servers.setdefault(server, []).append(func)
      self.catalog_text = "\n".join(
          f"{s}: {', '.join(names)}" for s, names in servers.items()
      )

  def _definition(self, func):
    _, _, t = self.by_func[func]
    schema = t.get("inputSchema") or {}
    params = {k: v for k, v in schema.items() if k != "$schema"}
    if params.get("type") != "object" or "properties" not in params:
      params = {"type": "object", "properties": params.get("properties", {})}
    return {
        "type": "function",
        "function": {
            "name": func,
            "description": (t.get("description") or func)[:1024],
            "parameters": params,
        },
    }

  def worker_tools(self):
    """Returns the extra function tools the worker may call."""
    if not self.by_func:
      return []
    if self.direct:
      return [self._definition(f) for f in self.by_func]
    return list(MCP_TOOLS)

  def system_note(self):
    """Returns the system-prompt paragraph describing MCP availability."""
    if not self.by_func:
      return ""
    if self.direct:
      return (
          " The task also offers its own tools (the functions named"
          " <server>__<tool>); use them where the task points to the"
          " project applications."
      )
    return (
        " The task also offers its own tools, listed below by server."
        " Call mcp_find to see a tool's parameters, then mcp_call to run"
        f" it.\n{self.catalog_text}"
    )

  def find(self, query):
    """Formats the schemas of the MCP tools best matching `query`."""
    words = [w for w in re.split(r"[^a-z0-9]+", query.lower()) if w]

    def score(item):
      func, (_, _, t) = item
      text = f"{func} {t.get('description') or ''}".lower()
      return (query == func) * 100 + sum(w in func.lower() for w in words) * 3 + sum(
          w in text for w in words
      )

    best = [
        item
        for item in sorted(self.by_func.items(), key=score, reverse=True)[:8]
        if score(item) > 0
    ]
    if not best:
      return "no tool matches; the names are listed in the system prompt"
    found = [
        f"{func}: {(t.get('description') or '')[:400]}\nparameters:"
        f" {json.dumps(t.get('inputSchema') or {})}"
        for func, (_, _, t) in best
    ]
    while len(found) > 1 and len("\n\n".join(found)) > RESULT_CHARS:
      found.pop()
    return "\n\n".join(found)

  async def call(self, func, arguments):
    """Runs MCP tool `func` and formats its result as text."""
    entry = self.by_func.get(func)
    if entry is None:
      return f"error: there is no MCP tool named {func}"
    mcp, original, _ = entry
    try:
      result = await mcp.call_tool(original, arguments or {})
    except (taskbridge.TaskError, taskbridge.MCPError, ValueError) as exc:
      return f"error: {type(exc).__name__}: {exc}"
    parts = []
    content = result.get("content")
    if isinstance(content, list):
      for item in content:
        if not isinstance(item, dict):
          continue
        kind = item.get("type")
        if kind == "text":
          parts.append(str(item.get("text", "")))
        elif kind in ("resource", "resource_link"):
          parts.append(json.dumps(item)[:RESULT_CHARS])
        else:
          parts.append(f"[{kind or 'unknown'} content not shown]")
    if not parts and result.get("structuredContent") is not None:
      parts.append(json.dumps(result["structuredContent"]))
    text = "\n".join(parts) or "(no content)"
    return clip(f"error: {text}" if result.get("isError") else text)


# ------------------------------------------------------- tool execution


async def run_tool(session, registry, fn):
  """Executes one tool call in the task container; returns its output.

  `fn["arguments"]` is rewritten as canonical JSON (or "{}" if invalid)
  so the stored conversation stays valid for strict endpoints. Every
  failure is returned as an error string for the model, never raised.
  """
  name = fn["name"]
  try:
    args = json.loads(fn["arguments"] or "{}")
    if not isinstance(args, dict):
      raise TypeError("not a JSON object")
    fn["arguments"] = json.dumps(args, allow_nan=False)
  except (TypeError, ValueError) as exc:
    fn["arguments"] = "{}"
    return f"error: the arguments are not a JSON object ({exc}); send the call again"
  try:
    if name == "bash":
      timeout = taskbridge.positive_int(args.get("timeout_sec"), 120, cap=600)
      result = await session.exec(args["command"], timeout_sec=timeout)
      return clip(f"exit code {result['exit_code']}\n{result['output']}")
    if name == "read_file":
      return clip((await session.read_file(args["path"])).decode("utf-8", "replace"))
    if name == "write_file":
      size = await session.write_file(
          args["path"], args["content"], append=bool(args.get("append"))
      )
      return f"wrote {size} bytes to {args['path']}"
    if name == "edit_file":
      return await edit_file(session, args)
    if name == "mcp_find":
      return registry.find(str(args.get("query", "")))
    if name == "mcp_call":
      return await registry.call(str(args.get("tool", "")), args.get("arguments"))
    if registry is not None and name in registry.by_func:
      return await registry.call(name, args)
    return f"error: there is no tool named {name}"
  except (taskbridge.TaskError, KeyError, ValueError) as exc:
    return f"error: {type(exc).__name__}: {exc}"


async def edit_file(session, args):
  """Implements edit_file via read_file + write_file (exact match only)."""
  path, old, new = args["path"], args["old_text"], args["new_text"]
  text = (await session.read_file(path)).decode("utf-8")
  count = text.count(old)
  if count == 0:
    return f"error: old_text not found in {path}"
  if count > 1 and not args.get("replace_all"):
    return f"error: old_text occurs {count} times in {path}; set replace_all or give more context"
  text = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
  size = await session.write_file(path, text)
  return f"edited {path} ({count if args.get('replace_all') else 1} replacement(s), {size} bytes)"


# ------------------------------------------------- context management


def conversation_chars(messages):
  return sum(len(m.get("content") or "") if isinstance(m.get("content"), str) else 0 for m in messages)


async def shorten(session, messages):
  """Truncates the older half of long tool results to CUT_KEEP chars."""
  long = [
      n for n, m in enumerate(messages)
      if m["role"] == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > 2 * CUT_KEEP
  ]
  if not long:
    return False
  cut = long[: max(1, len(long) // 2)]
  for n in cut:
    messages[n]["content"] = (
        messages[n]["content"][:CUT_KEEP] + "\n[... cut to save context; run the tool again if needed]"
    )
  await session.log(f"context: cut {len(cut)} old tool results")
  return True


def sanitize_tail(tail):
  """Drops orphan tool results / unresolved tool calls from `tail`.

  After a compaction the kept tail must still be a valid conversation:
  a tool message is kept only when a preceding kept assistant message
  asked for its id, and an assistant message keeps its tool_calls only
  when every call is answered later in the tail.
  """
  answered = set()
  for m in tail:
    if m["role"] == "tool":
      answered.add(m.get("tool_call_id"))
  out, pending = [], set()
  for m in tail:
    if m["role"] == "tool":
      if m.get("tool_call_id") in pending:
        out.append(m)
      continue
    m = dict(m)
    calls = m.get("tool_calls") or []
    if calls:
      kept = [c for c in calls if c.get("id") in answered]
      if kept:
        m["tool_calls"] = kept
        pending = {c["id"] for c in kept}
      else:
        m.pop("tool_calls", None)
        pending = set()
        if not m.get("content"):
          continue
    else:
      pending = set()
    out.append(m)
  # A leading tool message can never be valid.
  while out and out[0]["role"] == "tool":
    out.pop(0)
  return out


async def compact(session, messages):
  """Replaces the middle of `messages` with model-written notes.

  Keeps the system prompt, the original first user message, and the last
  KEEP_RECENT messages (sanitized). Returns False when there is nothing
  worth compacting or the summary call fails; the caller then falls back
  to shorten().
  """
  if len(messages) <= KEEP_RECENT + 3:
    return False
  transcript = "\n\n".join(
      f"{m['role']}: {clip(str(m.get('content') or ''), 4000)}"
      + (f"\n[tool calls: {json.dumps(m['tool_calls'])[:1000]}]" if m.get("tool_calls") else "")
      for m in messages[2:-KEEP_RECENT]
  )
  prompt = (
      "The conversation is getting long and its earlier turns will be removed. "
      "Write notes that let the work continue without them: what the task "
      "requires, what was found in the files, every file created or changed "
      "(exact path and state), the numbers and decisions so far, errors "
      "already hit, and what remains to do. Be specific.\n\n" + clip(transcript, 120_000)
  )
  try:
    reply = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        tool_choice="none",
        reasoning_effort=EFFORT,
        max_completion_tokens=8000,
    )
  except openai.APIError as exc:
    await session.log(f"compaction failed: {str(exc)[:200]}")
    return False
  if reply.usage:
    details = getattr(reply.usage, "prompt_tokens_details", None)
    await session.usage(
        MODEL, reply.usage.prompt_tokens, reply.usage.completion_tokens,
        getattr(details, "cached_tokens", 0) or 0,
    )
  notes = (reply.choices[0].message.content or "").strip()
  if not notes:
    return False
  tail = sanitize_tail(messages[-KEEP_RECENT:])
  messages[:] = messages[:2] + [
      {"role": "user", "content": f"## Notes from earlier work (context was compacted)\n{notes}"}
  ] + tail
  await session.log("context: compacted conversation into notes")
  return True


async def maybe_compact(session, messages):
  """Proactive context care, run after each tool round."""
  if conversation_chars(messages) <= COMPACT_AT_CHARS:
    return
  await shorten(session, messages)
  if conversation_chars(messages) > COMPACT_HARD_CHARS:
    await compact(session, messages)


# ------------------------------------------------------------ recovery


async def recover(session, messages, exc, waits, until):
  """Decides whether a failed model call is retried (as in minimal).

  Context-length 400 -> shorten and retry. 408/409/429/5xx or a dropped
  connection -> sleep the next RETRY_WAITS interval (capped so the wait
  ends before the step's wrap-up). Anything else is re-raised.
  """
  status = getattr(exc, "status_code", None)
  if status == 400 and CONTEXT_FULL.search(str(exc)):
    return await shorten(session, messages) or await compact(session, messages)
  wait = next(waits, None)
  if wait is None or not (status is None or status in (408, 409, 429) or status >= 500):
    return False
  left = session.time_left()
  if left is not None and left > until:
    wait = min(wait, left - until)
  await session.log(f"model call failed ({str(exc)[:200]}); again in {wait:.0f} s")
  await asyncio.sleep(wait)
  return True


# -------------------------------------------------------------- agent


def conversation(system, session, prompt, mcp_note=""):
  return [
      {"role": "system", "content": system.format(workdir=session.workdir, mcp=mcp_note)},
      {"role": "user", "content": prompt},
  ]


async def agent(session, registry, messages, tools, until, final=False):
  """Runs the tool-calling loop until the model answers without tools.

  `until` is the seconds-left threshold at which this step wraps up: the
  in-flight call is cancelled, WRAP_UP is appended once, and the step's
  last call runs with tool_choice="none". When `final` is true, every
  non-empty text reply also updates session.final_message, so taskbridge
  can still deliver a summary if the evaluator cancels the task.
  """
  nudges, head, waits = 0, "", iter(RETRY_WAITS)
  while True:
    left = session.time_left()
    wrap_up = left is not None and left < until
    if wrap_up and messages[-1:] != [WRAP_UP]:
      messages.append(dict(WRAP_UP))
    if left is None:
      timeout = None
    elif wrap_up:
      timeout = min(WRAP_UP_SEC, left)
    else:
      timeout = left - until
    try:
      reply = await asyncio.wait_for(
          client.chat.completions.create(
              model=MODEL,
              messages=marked(messages),
              tools=tools,
              tool_choice="none" if wrap_up else "auto",
              reasoning_effort=EFFORT,
              max_completion_tokens=MAX_OUTPUT_TOKENS,
          ),
          timeout,
      )
    except asyncio.TimeoutError:
      if wrap_up:
        return head
      continue
    except openai.APIError as exc:
      if await recover(session, messages, exc, waits, until):
        continue
      raise
    waits = iter(RETRY_WAITS)
    if reply.usage:
      details = getattr(reply.usage, "prompt_tokens_details", None)
      await session.usage(
          MODEL, reply.usage.prompt_tokens, reply.usage.completion_tokens,
          getattr(details, "cached_tokens", 0) or 0,
      )
    message = reply.choices[0].message
    cut = reply.choices[0].finish_reason == "length"
    text = head + (message.content or "")
    if final and text:
      session.final_message = text
    # Sent back exactly as returned: Gemini refuses a tool call that
    # comes back without its thought signature (an endpoint field).
    turn = message.model_dump(exclude_none=True)
    if wrap_up or not message.tool_calls:
      turn.pop("tool_calls", None)  # calls made at wrap-up are not run
      if message.content:
        messages.append(turn)
      if wrap_up or (message.content and not cut) or nudges == MAX_NUDGES:
        return text
      nudges, head = nudges + 1, text
      messages.append({
          "role": "user",
          "content": (
              "Your reply was cut off; continue where it stopped."
              if cut else "Continue, and finish with your answer."
          ),
      })
      continue
    nudges, head = 0, ""
    messages.append(turn)
    for call in turn["tool_calls"]:
      await session.log(f"{call['function']['name']} {call['function']['arguments'][:200]}")
      content = await run_tool(session, registry, call["function"])
      messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
    await maybe_compact(session, messages)
    left = session.time_left()
    if left is not None and messages[-1]["role"] == "tool":
      remain = max(session.time_left() - until, 0)
      messages[-1]["content"] += f"\n[{int(remain // 60)} minutes left for this step]"


def is_pass(verdict):
  """Returns True when a verifier reply names no gaps.

  The verifier is told to answer the single word PASS, but models often
  add a sentence ("PASS. All deliverables check out."), which must not
  be mistaken for a gap list. PASS is accepted as the first word of the
  reply, unless the reply also contains a numbered list (the instructed
  format for gaps) or a "but" on the first line. An empty reply names
  no gaps either.
  """
  text = (verdict or "").strip()
  if not text:
    return True
  if re.search(r"(?m)^\s*\d+[.)]\s", text):
    return False
  first = text.splitlines()[0].strip("*_# \t")
  if " but " in f" {first.lower()} ":
    return False
  upper = first.upper()
  return upper == "PASS" or upper.startswith(
      ("PASS ", "PASS:", "PASS.", "PASS—", "PASS-")
  )


# ------------------------------------------- deterministic self-check

_PATH_RE = re.compile(r"(/app|/filesystem)/[^\s\"'\)\]\|<>]+")
_TRAILING = ".,;:!"


def candidate_paths(*texts):
  """Extracts absolute container paths mentioned in plan / summary."""
  found, seen = [], set()
  for text in texts:
    for raw in _PATH_RE.finditer(text or ""):
      path = raw.group(0).rstrip(_TRAILING)
      # A directory deliverable ends with '/'; files are what we can size.
      if path.endswith("/") or path in seen:
        continue
      seen.add(path)
      found.append(path)
  return found[:50]


async def deterministic_check(session, *texts):
  """Checks that mentioned deliverable files exist and are non-empty.

  One bash call inside the task container; no model involved. Returns a
  list of problem strings (empty when everything mentioned is present).
  Paths the task only reads (inputs) can false-positive here, so the
  caller phrases the result as files to confirm, not as proven gaps.
  """
  paths = candidate_paths(*texts)
  if not paths:
    return []
  script = "\n".join(
      f"if [ -f {shlex.quote(p)} ]; then "
      f"s=$(wc -c < {shlex.quote(p)}); "
      f"if [ \"$s\" -gt 0 ]; then echo \"OK {p} $s\"; else echo \"EMPTY {p}\"; fi; "
      f"else echo \"MISSING {p}\"; fi"
      for p in paths
  )
  try:
    result = await session.exec(script, timeout_sec=60)
  except taskbridge.TaskError as exc:
    await session.log(f"deterministic check failed: {exc}")
    return []
  problems = [
      line for line in result["output"].splitlines()
      if line.startswith(("MISSING ", "EMPTY "))
  ]
  await session.log(f"deterministic check: {len(paths)} paths, {len(problems)} problems")
  return problems


# ---------------------------------------------------------------- solve


async def solve(session):
  """Planner -> worker -> deterministic check -> verifier/fix rounds."""
  total = session.time_left()

  def until(share):
    return 0 if total is None else total * share

  def time_for(wrap_up):
    left = session.time_left()
    return left is None or left - wrap_up >= MIN_STEP_SEC

  task = session.instruction
  registry = MCPRegistry(session)
  await registry.discover()

  plan = await agent(
      session, registry, conversation(PLANNER, session, task),
      READ_ONLY, until(1 - PLAN_SHARE),
  )
  await session.log(f"plan: {plan[:1000]}")

  worker_tools = TOOLS + registry.worker_tools()
  work = conversation(WORKER, session, f"{task}\n\n## Plan\n{plan}", registry.system_note())
  summary = await agent(session, registry, work, worker_tools, until(CHECK_SHARE), final=True)

  fix_rounds = 0

  async def fix(instruction_text):
    nonlocal summary, fix_rounds
    fix_rounds += 1
    work.append({"role": "user", "content": instruction_text})
    summary = await agent(
        session, registry, work, worker_tools, WRAP_UP_SEC, final=True
    ) or summary

  # Deterministic check first: it is free and catches the most damaging
  # failure mode (a deliverable that was never actually written).
  problems = await deterministic_check(session, plan, summary)
  if problems and fix_rounds < MAX_FIX_ROUNDS and time_for(WRAP_UP_SEC):
    await fix(
        "A file check (not a reviewer) reports these problems with files "
        "named in the plan or your summary:\n"
        + "\n".join(problems)
        + "\n\nIf a path is an input file or was renamed on purpose, say so "
        "in your summary; otherwise create or repair the file, re-open it "
        "to confirm, then reply with your updated summary."
    )

  while True:
    if not time_for(until(CHECK_SHARE / 2)):
      break
    review = f"## Task\n{task}\n\n## Plan\n{plan}\n\n## The engineer's summary\n{summary}"
    verdict = await agent(
        session, registry, conversation(VERIFIER, session, review),
        READ_ONLY, until(CHECK_SHARE / 2),
    )
    await session.log(f"verifier: {verdict[:1000]}")
    if is_pass(verdict) or not time_for(WRAP_UP_SEC):
      break
    if fix_rounds >= MAX_FIX_ROUNDS:
      await session.log("verifier found gaps but the fix-round budget is used up")
      break
    await fix(
        "A reviewer checked your files and found these gaps:\n"
        f"{verdict}\n\nFix them, then reply with your updated summary."
    )
    problems = await deterministic_check(session, plan, summary)
    if problems:
      await session.log(f"after fix: deterministic problems remain: {problems}")

  return summary or session.final_message


if __name__ == "__main__":
  taskbridge.run(solve)
