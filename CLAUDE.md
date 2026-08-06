# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Early. What exists: `models.py` (shared types) and `corpus/schema.sql` (verified against SQLite
3.51 — 9 tables, 3 views). Everything else below is the agreed design, not shipped code. Update
this file as code lands so it describes reality rather than intent.

No parser, no pipeline stages, no `pyproject.toml` yet. `pokerkit` is a planned dependency
(requires Python ≥ 3.11) and is not installed.

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

1. **Ingest** — read site hand histories, emit one `.phh` file per hand, index it in SQLite.
   Idempotent: re-reading a file must not create a second row (dedupe on `(site, site_hand_id)`).
2. **Triage** — replay each hand with pokerkit, run chart lookups and equity/EV math over hero's
   decisions, write `flagged_decisions`. **No LLM calls in this stage, ever** — it must stay fast
   and free, because it is the thing that protects stage 3's budget.
3. **Analyze** — Agent SDK agent per flagged decision, with custom tools for equity, ranges, solver
   lookup, and corpus search. Emits `findings` carrying an $EV-lost estimate.
4. **Synthesize** — cluster findings into named leaks across *all* sessions, rank by cumulative
   $EV lost, write the report. Reads `findings` from SQL; never opens a `.phh` file.

### Where data lives

Three layers, split on **mutability**. This is the decision most likely to be misread, so it is
worth stating flatly:

| Layer | Owns | Why |
|---|---|---|
| `.phh` files | hands | Write-once and immutable, which is what files are good at. Open standard (PHH, TOML-based), human-readable, diffable, testable against the public PHH dataset. |
| `pokerkit` | replay | Pot sizes, side pots, legal actions, stack tracking, hand evaluation. **Do not reimplement any of this.** |
| SQLite | index + pipeline state | Findings get superseded, leak status moves `open → fixed`, flagged decisions get re-queued, "pending work, most expensive first" is an ordered query. Files are bad at that. |

The `hands` table indexes only what you **filter** on and points at `phh_path`. Detail stays in the
`.phh` file. Add a column when a query proves slow — do not mirror by default, because a SQL mirror
of the action list is duplicated state that can drift from the archive it copies.

Replay cost is once-per-hand-ever (ingest and triage), so it is not a factor in design decisions.
The one exception is backfilling after a detector change, which rescans history; that is an
annoyance, not a constraint.

### Invariants

- **$EV lost is the ranking currency.** Every finding carries one. Reports rank by money, never by
  error count.
- **A flagged decision is keyed on the decision, not the detector.** Two rules firing on one call
  must produce one judgement; per-detector rows would double-count that money in leak totals and
  inflate exactly the leaks whose detectors overlap most. Detectors live in a child table.
- **Parsers emit PHH.** The `SiteParser` contract is "produces a valid `.phh` file", not "produces
  our dataclasses". Nothing outside `ingest/parsers/` knows which site a hand came from.
- **Solver access sits behind a `SolutionProvider` interface**, cache-first, keyed on the abstract
  spot rather than provider internals. A broken scraper cannot break the nightly run.
- **Watch `v_detector_precision`.** It is the main defense against the funnel silently degrading. A
  detector whose flags rarely come back `mistake` is burning agent budget.

### PHH gaps to work around

The spec does not cover everything a cash-game study tool needs. These are known, not surprises:

- **No rake field.** The paper's position is that rake is reconstructed from `finishing_stacks`. Do
  that at ingest and store it — rake is what makes marginal opens unprofitable, so a model that
  can't express it will rate the loosest opens as fine.
- **No site hand ID.** PHH has `hand` (a counter), not a site-scoped unique string. Carried as
  user-defined fields `_pc_site` / `_pc_site_hand_id` (see `models.py`).
- **`cc` collapses check/call and `cbr` collapses bet/raise.** Recoverable from `to_call` during
  replay, and resolved into `ActionType`. Never treat them as interchangeable — checking and
  calling are different decisions and a detector keyed on the wrong one measures nothing.
- **Positions are implicit** in player index + `blinds_or_straddles`. Derived once by
  `models.position_of()`. Heads up is the trap: the button posts the small blind, so index 0 is BTN.

### Intended layout

```
archive/                 # the .phh corpus: system of record, one file per hand
src/poker_coach/
├── models.py            # DONE: shared types; no Hand type -- that's the .phh file
├── config.py
├── ingest/
│   ├── watcher.py
│   ├── parsers/         # base.py defines SiteParser (emits PHH); one per site
│   └── indexer.py       # replay a .phh, project a HandIndex row
├── corpus/
│   ├── schema.sql       # DONE
│   └── store.py         # SQLite access
├── replay.py            # pokerkit wrapper: .phh -> Decision stream
├── triage/
│   ├── ranges.py        # solver-derived charts as data, not code
│   ├── equity.py        # equity/EV primitives
│   └── detectors.py     # emit flagged_decisions
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
