# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

**Nothing is implemented yet.** The repo holds `README.md` and this file. Everything below is the
agreed design — treat it as the spec to build against, and update this file as code lands so it
describes reality rather than intent.

## What this is

A post-session poker study tool for NLHE 6-max cash. Hand histories in, ranked leak report out.
See `README.md` for the pipeline diagram.

**Hard constraint: no real-time assistance.** Nothing in this codebase may read a live table or
produce advice while a hand is in progress. That is an account-ban offense on every major online
site, and the post-session-only boundary is why the project is safe to run at all. If a feature
request implies live play, flag it rather than building it.

## Architecture

The core design problem is economics, not accuracy. A session is hundreds of hands and ~90% are
trivially correct, so the pipeline is a **funnel**: deterministic filters first, agents only on
survivors. Preserve that shape — a change that sends every hand to an agent is a regression even
if its output looks better.

Four stages, each independently runnable so you can re-run one without redoing the others:

1. **Ingest** — watch the hand-history folder, parse to one canonical hand model, persist. Idempotent:
   re-ingesting a file must not duplicate hands (dedupe on the site's hand ID).
2. **Triage** — preflop chart lookup + equity/EV math. Emits `Candidate` records for hands worth a
   closer look. No LLM calls in this stage, ever; it must stay fast and free.
3. **Analyze** — Agent SDK agent per candidate, with custom tools for equity, ranges, solver
   lookup, and corpus search. Emits `Finding` records carrying an $EV-lost estimate.
4. **Synthesize** — cluster findings into named leaks across *all* sessions, rank by cumulative
   $EV lost, write the report.

### Invariants

- **$EV lost is the ranking currency.** Every `Finding` carries one. Reports rank by money, never
  by error count.
- **The corpus is the product.** A leak is a cross-session pattern, so findings and hands persist
  in SQLite and synthesis reads the whole history, not just tonight's session.
- **Parsers sit behind a `SiteParser` interface.** The target site isn't settled. Nothing outside
  `ingest/parsers/` may know which site a hand came from.
- **Solver access sits behind a `SolutionProvider` interface**, cache-first. The rest of the
  pipeline must degrade gracefully when a provider is unavailable — a broken scraper cannot break
  the nightly run.

### Intended layout

```
src/poker_coach/
├── models.py            # canonical Hand / Street / Action / Candidate / Finding
├── config.py
├── ingest/
│   ├── watcher.py
│   └── parsers/         # base.py defines SiteParser; one module per site format
├── corpus/
│   ├── schema.sql
│   └── store.py         # SQLite: hands, actions, candidates, findings, leaks, runs
├── triage/
│   ├── ranges.py        # solver-derived charts as data, not code
│   ├── equity.py        # equity/EV primitives
│   └── rules.py         # candidate detection
├── agent/
│   ├── tools.py         # @tool defs + create_sdk_mcp_server
│   ├── analyst.py       # stage 3
│   ├── synthesist.py    # stage 4
│   └── prompts/         # system prompts as files, not inline strings
├── solvers/
│   ├── base.py          # SolutionProvider protocol
│   ├── cache.py
│   └── gtowizard.py     # Playwright; opt-in, off by default
└── cli.py               # ingest / triage / analyze / report / run
```

## Claude Agent SDK notes

This project uses the **Claude Agent SDK** (`pip install claude-agent-sdk`) — the Claude Code
harness as a library. It is *not* the Anthropic API SDK's tool runner; the two are easy to
conflate and have different imports and different capabilities. Reference:
https://code.claude.com/docs/en/agent-sdk/python

Shape of an analysis stage:

```python
from claude_agent_sdk import tool, create_sdk_mcp_server, query, ClaudeAgentOptions

@tool("hand_equity", "Equity of a holding vs a range on a given board", {"hero": str, "villain_range": str, "board": str})
async def hand_equity(args: dict) -> dict:
    return {"content": [{"type": "text", "text": ...}]}

poker = create_sdk_mcp_server(name="poker", version="1.0.0", tools=[hand_equity, ...])

options = ClaudeAgentOptions(
    model="claude-opus-5",
    thinking={"type": "adaptive"},
    mcp_servers={"poker": poker},
    allowed_tools=["mcp__poker__*"],
    max_budget_usd=...,        # hard cap on a nightly run
)
```

Non-obvious things that will bite:

- **Tool names are `mcp__{server_key}__{tool_name}`.** The server key is the key in `mcp_servers`,
  not the `name=` passed to `create_sdk_mcp_server`. Wildcards work: `mcp__poker__*`.
- **The Python dict schema makes every key required.** For an optional parameter, leave it out of
  the schema, document it in the description string, and read it with `args.get()`. Use a full
  JSON Schema dict instead when you need enums or ranges.
- **`structuredContent` is unavailable** from Python in-process tools — the `@tool` decorator
  forwards only `content` and `is_error`. Tool results come back as text, so serialize
  deliberately (JSON in a text block) rather than expecting typed fields.
- **`tools`, `allowed_tools`, and `disallowed_tools` are different layers.** `tools=[...]` controls
  which built-ins exist in context; `allowed_tools` only pre-approves permission. To keep a
  built-in out entirely, omit it from `tools` or list its bare name in `disallowed_tools`.
- **Return failures as `{"content": [...], "is_error": True}`** rather than raising. Handler
  exceptions don't stop the loop; they reach Claude as a bare exception string with no context.
- **Mark read-only tools** with `annotations=ToolAnnotations(readOnlyHint=True)` — that's what lets
  Claude batch equity and corpus lookups in parallel.
- **Use `max_budget_usd`** on every batch entry point. An unattended overnight run over hundreds of
  hands is exactly the workload that needs a ceiling.
- **Model:** `claude-opus-5`, with `thinking={"type": "adaptive"}`. Do not use `budget_tokens` — it
  is removed on this model and returns a 400.
- Prefer `query()` for per-hand analysis (fresh context per hand) and `ClaudeSDKClient` only where
  a stage genuinely needs multi-turn continuity.

### Prompt caching

Analysis runs the same system prompt and the same range/chart context across hundreds of hands, so
caching matters more than usual. Keep the stable prefix byte-identical: system prompt and chart
data first, per-hand content last, and no timestamps or run IDs anywhere in the prefix.

## Conventions

- Prompts live in `agent/prompts/` as files, not inline string literals.
- Ranges and charts are **data** (checked-in files), not Python literals.
- Every stage is independently invocable via `cli.py` and writes its output to the corpus, so a
  failed stage 3 never forces a re-parse.
- Cache anything a solver provider returns, keyed on the abstract spot, not on provider internals.
