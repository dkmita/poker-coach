# poker-coach

Agentic system that analyzes hands while you play or sleep.

You play a session. Overnight, poker-coach reads the hand histories, works out where your
decisions cost money, and hands you a prioritized leak report plus a drill set for your next
study block.

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
| **Output** | Ranked leak report + drill hands, written to disk |

## How it works

A session is hundreds of hands, and most of them are folds and trivially-correct spots. Deep
analysis is slow and costs money, so the pipeline is a **funnel** — cheap deterministic checks
kill the boring majority, and only the survivors reach an agent.

```
  hand histories
        │
   1. INGEST ......... parse to one canonical hand model, store in SQLite      (free)
        │
   2. TRIAGE ......... preflop charts + equity/EV math flag candidate errors   (cheap, deterministic)
        │              ~90% of hands stop here
   3. ANALYZE ........ agent reasons over each candidate, with tools for
        │              equity, solver lookup, and corpus search               (expensive)
        │
   4. SYNTHESIZE ..... cluster findings into named leaks across sessions,
        │              rank by $EV lost                                        (one pass)
        ▼
     report + drills
```

Two things are load-bearing:

- **$EV lost is the ranking currency.** Not error count. "You over-fold BB vs BTN 2.5x" only
  matters if it's costing real money, and that's what makes the report actionable instead of a
  wall of nitpicks.
- **Hands accumulate in a local corpus.** One session can't reveal a leak; a leak is a pattern
  across sessions. The SQLite database is the product — the nightly run just adds to it.

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
  findings, leak history, the solver cache, run provenance. Hands stay in PHH; the database holds
  what changes.
- **[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk)** (`claude-agent-sdk`) for the
  analysis and synthesis stages: built-in file/search tools plus custom in-process MCP tools for
  equity, ranges, solver lookups, and corpus queries

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
