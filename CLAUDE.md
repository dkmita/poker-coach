# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Early. What exists and works:

- `models.py` — shared types
- `corpus/schema.sql` — verified on SQLite 3.51 (7 tables, 2 views)
- `replay.py` — the pokerkit boundary
- `handview.py` — one hand as a JSON contract for a UI (`tools/show_hand.py` renders it)
- `solvers/base.py` — `SolutionProvider` protocol + `NullProvider`; no real provider yet
- `tools/generate_corpus.py` — synthetic PHH corpus with planted, labelled mistakes
- `pyproject.toml` — `pokerkit>=0.7,<0.8`, Python ≥ 3.11; 33 tests passing

Not written yet: the ACR/WPN parser, the three pipeline stages, the range charts, and the CLI.
Everything below describing those is agreed design, not shipped code — update this file as they
land so it describes reality rather than intent.

## Commands

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'        # see the index caveat below
.venv/bin/python -m pytest -q            # whole suite
.venv/bin/python -m pytest tests/test_replay.py::test_cc_resolves_to_check_or_call   # one test
```

### Synthetic corpus

```bash
.venv/bin/python tools/generate_corpus.py --count 2000 --out archive/synthetic --seed 1
```

Not committed — `archive/` is gitignored and the corpus is byte-reproducible from its seed, so
the generator is the artifact, not its output. Regenerate rather than share files.

Hands are *played* with pokerkit, so every one is legal by construction. Hero rotates through all
six seats, and specific mistakes are planted on purpose and recorded in `manifest.json` by the
ordinal of hero's decision in the hand. **That gives triage a recall measure**, which
`v_detector_precision` cannot provide: precision can't see mistakes a detector never flagged. A
detector at 95% precision that misses four of five planted `bb_overfold`s is broken, and only the
manifest reveals it.

At `--count 2000 --seed 1`: 202 planted across `bb_overfold` (86), `utg_open_too_wide` (102),
`flop_float_with_air` (14); streets reached 952 / 177 / 100 / 771.

Three caveats before trusting it for anything beyond pipeline development:

- **`river_overcall` plants zero.** Hero floats the flop but folds the turn, so it never arrives at
  a river bet holding air. That detector has no ground truth until hero's policy changes.
- **Win rate is meaningless.** All six seats run the same crude policy and there is no skill model,
  so hero's bb/100 is noise. Don't compute strategy conclusions from this corpus.
- **Planted leaks must stay genuinely wrong.** An early version labelled folding 72o in the big
  blind a `bb_overfold` because the trigger had no lower bound. Bad ground truth is worse than
  none: it quietly makes every recall number wrong. Thresholds are frequency-derived
  (`_threshold(0.45)` = top 45% of hands) rather than hand-picked, for the same reason.

This does **not** help write the ACR parser. These are already PHH; the parser converts ACR's
undocumented text format *into* PHH, and that needs real samples.

### Environment

Two gotchas on this machine, both already hit:

- **pip defaults to Indeed's internal artifact proxy** (`dl.artifacts.indeed.tech`), which does not
  serve `pytest` or `hatchling`. Append `--index-url https://pypi.org/simple` for this project's
  dev dependencies — it's a personal repo, not Indeed work.
- **Build isolation can't reach the network**, so `pip install -e .` fails while resolving
  `hatchling`. Either preinstall `hatchling` and add `--no-build-isolation`, or skip the editable
  install entirely: `[tool.pytest.ini_options] pythonpath = ["src"]` means pytest imports the
  package straight from `src/` without it.

## What this is

A post-session poker study tool for NLHE 6-max cash. Hand histories in, mistakes ranked by $EV lost
out.
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

Three stages, each independently runnable so you can re-run one without redoing the others:

1. **Ingest** — read site hand histories, emit one `.phh` file per hand, index it in SQLite.
   Idempotent: re-reading a file must not create a second row (dedupe on `(site, site_hand_id)`).
2. **Triage** — replay each hand with pokerkit, run chart lookups and equity/EV math over hero's
   decisions, write `flagged_decisions`. **No LLM calls in this stage, ever** — it must stay fast
   and free, because it is the thing that protects stage 3's budget.
3. **Analyze** — Agent SDK agent per flagged decision, with custom tools for equity, ranges, solver
   lookup, and corpus search. Emits `findings` carrying an $EV-lost estimate.

**Out of scope for now: synthesis.** Clustering findings into named recurring leaks with cumulative
cost and an `open → fixed` lifecycle is deferred until these three stages work. There is no
`leaks` table, and `runs.stage` has no `synthesize` value — both were removed rather than left as
unused scaffolding. Re-adding them is a `schema_version = 2` migration; the original DDL is in git
at `5b4a8f4` if it's useful as a starting point. Don't build toward it in the meantime.

### Three tiers of output, split by cost

A hand view separates these deliberately, and the split is what lets a client render
something useful for free:

| Tier | Source | Cost | Where it lands |
|---|---|---|---|
| Hand facts | pokerkit replay | free | always present |
| Reference facts | charts / solver | expensive once, then cached | `gto` on each decision |
| Judgments | LLM | expensive every time | `analysis` slots |

**Solver output is a fact, not a judgment.** "The big blind defends this hand 100% vs a 2.5x
button open at 100bb" is a deterministic, cacheable lookup — the same answer regardless of who
asks. It belongs beside pot odds, not in the commentary. The consequence is that
`"you folded laying 27%; equilibrium calls 82%"` is a complete, actionable statement with no
model call. The LLM's job is explaining *why* and what it cost, not supplying the reference.

Rules that follow:

- `gto: null` means **not looked up**. It is not the same as "equilibrium is indifferent here",
  which is a `Solution` with mixed frequencies. A client that renders them the same implies
  knowledge it doesn't have.
- `Solution.is_mixed()` exists because deviating at a genuinely mixed node is not a mistake.
  Calling one a mistake is the fastest way to lose a user's trust in the tool.
- Providers are **cache-first and failure-tolerant**: unavailable returns `None`, never raises.
  `handview` swallows provider exceptions per decision, so a broken scraper costs you a `gto`
  block and not the hand.
- `spot_key` must never mention hero's cards or every lookup is a cache miss. Stack depth is
  bucketed to 25bb, the granularity charts are published at.

Numbers that are arithmetically defined but conventionally meaningless are suppressed, not
computed: no SPR preflop (the pot is just blinds), no `pct_pot` on a preflop raise (those are
read as multiples of the blind). "SPR 24.8" on a preflop fold is noise dressed as information,
and a poker player reading the UI would clock it as a bug.

### Where data lives

Three layers, split on **mutability**. This is the decision most likely to be misread, so it is
worth stating flatly:

| Layer | Owns | Why |
|---|---|---|
| `.phh` files | hands | Write-once and immutable, which is what files are good at. Open standard (PHH, TOML-based), human-readable, diffable, testable against the public PHH dataset. |
| `pokerkit` | replay | Pot sizes, side pots, legal actions, stack tracking, hand evaluation. **Do not reimplement any of this.** |
| SQLite | index + pipeline state | Findings get superseded when a prompt improves, flagged decisions get re-queued after a detector changes, and "pending work, most expensive first" is an ordered query. Files are bad at mutable, cross-referenced state. |

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
  must produce one judgement. Per-detector rows would put the same mistake in the report twice and
  count its cost twice in any total — and would do it worst to the spots with the most overlapping
  detectors. Detectors live in a child table.
- **Parsers emit PHH.** The `SiteParser` contract is "produces a valid `.phh` file", not "produces
  our dataclasses". Nothing outside `ingest/parsers/` knows which site a hand came from.
- **Solver access sits behind a `SolutionProvider` interface**, cache-first, keyed on the abstract
  spot rather than provider internals. A broken scraper cannot break the nightly run.
- **Watch `v_detector_precision`.** It is the main defense against the funnel silently degrading. A
  detector whose flags rarely come back `mistake` is burning agent budget.

### pokerkit: three undocumented behaviors `replay.py` depends on

All three were found empirically against 0.7.4, all three fail by returning
plausible-but-wrong numbers rather than raising, and all three are why the dependency is pinned
`>=0.7,<0.8` and confined to one module. **Nothing outside `replay.py` may import pokerkit.**

1. **One `State` object, mutated in place.** Every `state_actions` pair yields the same instance,
   so `[s for s, _ in hh.state_actions]` is N references to the *final* state. Read what you need
   during the pass.
2. **`state_actions` is off by one.** Pair `i` is `(state_i, action[i-1])`, where `state_i` is what
   `hh.actions[i]` faced. Index into `hh.actions` yourself; ignore the action in the tuple.
3. **`street_index` is positional, not board-derived.** At the first state of each street the card
   hasn't been dealt, so board length lags.

`iter_decisions` asserts `state.actor_index` matches the seat named in the action, which is what
turns a future regression into a `ReplayError` instead of silently wrong pot sizes. Two things
pokerkit gives us for free and we should not rebuild: it **validates** betting legality (an illegal
sequence raises, so a hand that replays is internally consistent), and `pokerkit.notation.rake`
implements percentage/cap/no-flop-no-drop.

pokerkit also ships site parsers (`HandHistory.from_pokerstars`, `from_partypoker`, …). There is no
WPN/ACR one, but `PokerStarsParser` is a useful reference when writing it.

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
├── replay.py            # DONE: pokerkit boundary, .phh -> Decision stream
├── triage/
│   ├── ranges.py        # solver-derived charts as data, not code
│   ├── equity.py        # equity/EV primitives
│   └── detectors.py     # emit flagged_decisions
├── agent/
│   ├── tools.py         # @tool defs + create_sdk_mcp_server
│   ├── analyst.py       # stage 3
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
