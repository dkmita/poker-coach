# poker-coach

Agentic system that analyzes hands while you play or sleep.

You play a session. Overnight, poker-coach reads the hand histories, works out which of your
decisions cost money, and hands you those mistakes ranked by what they cost.

**This is a study tool, not a real-time assistant.** Everything runs after a session ends, on
your own hand histories. Nothing reads the table, and nothing advises you mid-hand — real-time
assistance violates the terms of service of every major online site and is an account-ban
offense. Keeping the whole system post-session is a design constraint, not an implementation
gap.

## Scope

| | |
|---|---|
| **Game** | No-limit hold'em, 6-max cash |
| **Input** | Hand-history files written by an online client (site-agnostic parser; PokerStars-style format first) |
| **Timing** | Overnight batch, or on demand after a session |
| **Output** | Your mistakes, ranked by $EV lost, written to disk |

## How it works

A session is hundreds of hands, and most of them are folds and trivially-correct spots. Deep
analysis is slow and costs money, so the pipeline is a **funnel** — cheap deterministic checks
kill the boring majority, and only the survivors reach an agent.

```
  hand histories
        │
   1. INGEST ......... convert to .phh, index in SQLite                    (free)
        │
   2. TRIAGE ......... replay each hand; charts + equity/EV math flag
        │              hero decisions worth a look                         (cheap, deterministic)
        │              ~90% of decisions stop here
   3. ANALYZE ........ agent judges each flagged decision, with tools for
        │              equity, solver lookup, and corpus search            (expensive)
        ▼
   findings, ranked by $EV lost
```

**$EV lost is the ranking currency** — not error count. "You over-fold BB vs BTN 2.5x" only matters
if it's costing real money, and that's what makes the output actionable instead of a wall of
nitpicks.

Hands accumulate in a local corpus regardless, because that's where the raw material for
cross-session analysis will come from later. But nothing reads across sessions yet: today's output
is a ranked list of individual mistakes from the hands you just played.

### Deliberately not built yet

**Synthesis** — clustering findings into named, recurring leaks with cumulative cost and a
`open → fixed` lifecycle. That's the thing that turns "these 40 hands were mistakes" into "you
over-fold the big blind, and it has cost you $310 this month." It's the eventual point of the
project, and it's out of scope until the three stages above actually work.

## Stack

- **Python** (≥ 3.11) — where the poker ecosystem lives
- **[PHH](https://phh.readthedocs.io/)** as the hand archive: an open, TOML-based hand-history
  standard ([paper](https://arxiv.org/abs/2312.11753)). One `.phh` file per hand is the system of
  record — immutable, human-readable, diffable, and testable against the public PHH dataset.
  Parsers convert site histories *into* PHH, so the ingest boundary is a published standard rather
  than a shape only our tests know about.
- **[pokerkit](https://github.com/uoftcprg/pokerkit)** as the replay engine: pot sizes, side pots,
  legal actions, stack tracking, hand evaluation. We don't reimplement game logic.
- **SQLite** for an index over the archive plus all mutable pipeline state — flagged decisions,
  findings, the solver cache, run provenance. Hands stay in PHH; the database holds what changes.
- **[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk)** (`claude-agent-sdk`) for the
  analysis stage: built-in file/search tools plus custom in-process MCP tools for equity, ranges,
  solver lookups, and corpus queries

## Solver lookups

Postflop verdicts come from a `SolutionProvider` interface with an aggressive cache, so a cache
hit costs nothing and the provider is swappable:

- A **GTO Wizard** provider driven by browser automation against a personal account. Worth
  knowing before enabling it: automated access is the kind of thing their terms typically
  prohibit, and it's brittle when their UI changes. It sits behind a flag and is off by default.
- An **open-source local solver** (e.g. TexasSolver) as the substitute, so nothing else in the
  pipeline depends on the scraper working.

## Status

Early. The data model (`src/poker_coach/models.py`) and corpus schema
(`src/poker_coach/corpus/schema.sql`) are settled; the pipeline stages are not written yet. See
`CLAUDE.md` for the layout, conventions, and the reasoning behind each decision.
