# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Early. What exists and works:

- `models.py` — shared types
- `corpus/schema.sql` — verified on SQLite 3.51 (7 tables, 2 views)
- `replay.py` — the pokerkit boundary
- `handview.py` — one hand as a JSON contract for a UI (`tools/show_hand.py` renders it)
- `solvers/base.py` — `SolutionProvider` protocol + `NullProvider`
- `solvers/ranges.py` — `ChartProvider`: reads PioSolver-format range exports from `charts/`
- `tools/generate_corpus.py` — synthetic PHH corpus with planted, labelled mistakes
- `pyproject.toml` — `pokerkit>=0.7,<0.8`, Python ≥ 3.11; 43 tests passing

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

**Depth over coverage.** The funnel is not only the cheap shape, it is the one that matches how
players are told to study: *"better to dissect 3 hands in full than to skim 30 with half your
attention"*, with a target of roughly **10–15% of hands played** getting real review
([CheckReplay](https://blog.checkreplay.com/poker-hand-review/),
[SplitSuit](https://www.splitsuit.com/review-own-poker-hands-and-practice)). So the funnel's output
volume is a product decision, not just a budget one, and 10–15% is the number to tune the detectors
and the "interesting" filter against. A run that surfaces 60% of a session is broken even if every
flag is defensible — it is back to skimming 30.

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

### GTO Wizard: export, do not automate

Checked, with sources, rather than assumed:

- Their ToS prohibits "automated requests or any scripts within the Service"
  (<https://gtowizard.com/terms/>), with immediate suspension as the stated remedy.
- Their public API is a **benchmarking** API — connect an agent, play hands, get results. It
  deliberately excludes solver access and refuses requests for it
  (<https://gtowizard.com/benchmark/terms>).

The supported path is the product's own export: Ranges tab → copy button → standard
PioSolver/GTO+ text (<https://help.gtowizard.com/ranges-tab/>). Save one file per action under
`charts/<spot_key>/` and `ChartProvider` reads it.

This is also the better integration on the merits: no credentials in the tool, nothing to break
when they redesign a page, works offline and in CI, and the same format comes out of TexasSolver
and GTO+ — so one importer serves every source. Don't replace it with a scraper.

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
`>=0.7,<0.8` and confined to two modules. **Only `replay.py` and `equity.py` may import
pokerkit** — `replay.py` owns `pokerkit.notation` (replaying a hand), `equity.py` owns
`pokerkit.hands` (evaluating one). Two different surfaces; folding evaluation into `replay.py`
would make that module a grab bag, and the point of the rule is that an upgrade has a small,
named blast radius, not that the number of files is one.

1. **One `State` object, mutated in place.** Every `state_actions` pair yields the same instance,
   so `[s for s, _ in hh.state_actions]` is N references to the *final* state. Read what you need
   during the pass.
2. **`state_actions` does not line up with `hh.actions` positionally.** Pair `i` is
   `(state_after_action_i, action_i)`, so the state facing an action is the previous pair's — and
   pokerkit also emits states for its own automatic operations, whose action half is `None`, which
   makes the two sequences different lengths. **Use `iter_action_states`**, which pairs from the
   walk itself and never indexes `hh.actions`.
3. **`street_index` is positional, not board-derived.** At the first state of each street the card
   hasn't been dealt, so board length lags.

### pokerkit repairs what it disagrees with, and heads-up it disagrees

The one to internalise. pokerkit does not reject an action sequence that contradicts its own model
of the game — it **silently inserts the action it expected** and carries on.

Heads-up, pokerkit opens postflop betting at **index 0 regardless of who posted what**. So index 0
must be the big blind and index 1 the button — the reverse of every other table size, where index 0
is the small blind. `_phh_order` does this, `position_of` mirrors it, and the blind amounts are
written reversed on top (pokerkit posts entry 1 to index 0), so a heads-up array reads
`[small, big]` and produces `bets = [big, small]`.

Get it backwards and pokerkit thinks the button opens the flop. It reconciles the disagreement by
inventing a check for the button at the top of every postflop street. That costs no chips, so the
finishing stacks still reconcile against the site and nothing looks wrong — until a hand where the
button really did check behind, and the whole replay dies with `Unable to repair the hand history`.
Meanwhile the phantom states desynchronise `state_actions` from `hh.actions`, which is what made
postflop calls render as checks and raises as bets.

`iter_action_states` now raises if pokerkit inserts an operation before the last player action, and
`parse_player_action` asserts `state.actor_index` matches the seat named in the action. Both live in
`replay.py` so **every** consumer gets them: `handview._walk` once had its own copy of the verb
resolution without the check, and rendered a hand nobody played. Do not re-derive the pairing
anywhere else.

Two things pokerkit gives us for free and we should not rebuild: it **validates** betting legality
(an illegal sequence raises, so a hand that replays is internally consistent), and
`pokerkit.notation.rake` implements percentage/cap/no-flop-no-drop. Note the limits of that
validation: pokerkit applies each action to whoever *it* thinks is the actor, not to the seat named
in the PHH line, and repairs rather than rejects — so "it replayed" means far less than it sounds.

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
  `models.position_of()`. Heads up is the trap, and not in the direction it looks: index 0 is the
  **BB**, index 1 the button. That is forced by pokerkit's postflop ordering, not by PHH — see
  "pokerkit repairs what it disagrees with" above.
- **No slot for an out-of-turn post.** A player buying in mid-orbit posts a live big blind from
  their own seat. `blinds_or_straddles` is the only place for it and pokerkit reads a third entry as
  a straddle, which moves where the action starts; an ante is dead money, so the poster pays twice.
  It is therefore *not posted*: the money enters at the poster's own turn, where their recorded `cc`
  resolves to a call of the same amount. Recorded as `_pc_live_post` because it is the one place the
  archive knowingly differs from the site — players acting earlier faced a bigger pot than the
  replay reports, and the poster's check is named a call. **Refused outright when hero is the
  poster**, since that would invent a hero decision for the chart layer to judge.

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

## Model access

This project uses the **Anthropic API SDK** (`pip install anthropic`), not the Claude Agent SDK.
That was reconsidered deliberately: the Agent SDK is Claude Code packaged as a library — built-in
file/bash tools, an agent loop, sessions, permissions — and every model call here is a single
request with a stable prefix and a short structured answer. The loop, the tools and the sandbox
would all be unused, and the model would be handed filesystem access it has no need for.

If a stage ever does want the model to call `hand_equity` or a chart lookup itself, the thing to
reach for is `client.beta.messages.tool_runner` — still the `anthropic` package, custom tools only,
no sandbox. The Agent SDK earns its weight only for an agent that needs to read and write files.

### The boundary

`llm.py` is the only module that knows a vendor exists. One method:

```python
class LLM(Protocol):
    name: str
    def complete(self, *, system: str, prompt: str, max_tokens: int) -> Reply | None: ...
```

- **`NullLLM` is the default.** No key configured means every estimate comes back `None` and the
  pipeline runs to completion for free. A test suite that needed credentials is a test suite nobody
  runs, so `StubLLM` answers from a canned list and records what it was asked.
- **Unavailable returns `None`, never raises** — same contract as `SolutionProvider`. No key, no
  network, rate limit, 500, unparseable answer: all one thing to the caller, which is one missing
  estimate rather than a dead run.
- Swapping vendors is one class. `AnthropicLLM` is about thirty lines of it.
- `anthropic` is imported inside the call, so it stays an optional dependency and importing
  `poker_coach` does not require it.

### Ask for a range, not a number

The model's job is to estimate **the opponent's range**, and nothing else. Equity, pot odds and EV
are computed from that range by `equity.py`, exactly.

This is the whole design, so it is worth being explicit about why:

- Arithmetic is the thing a model is worst at and the thing that is cheapest to do properly.
- A range is **checkable**. It renders as the same 13×13 grid a chart does, so a wrong one is
  visible at a glance; a wrong EV figure is not.
- A range is **cacheable on `spot_key`**, because it does not depend on hero's cards — the same
  rule the solver cache follows, and for the same reason.
- Model ranges and chart ranges become the same object downstream, both parsed by `parse_range`.

`RangeEstimator` caches on `(spot_key, board)` and does not retry a spot that failed: otherwise a
spot the model cannot answer is paid for on every hand that reaches it.

### Prompt caching

The system prompt is the task description followed by `heuristics/`, in that order, and must be
byte-identical across hands — it is sent as a cached block. Nothing about a specific hand may
appear in it. Editing a heuristic invalidates the cache for the rest of the run, which is the right
trade and still worth knowing before editing mid-run.

**Model:** `claude-opus-5` with `thinking={"type": "adaptive"}`. Do not pass `budget_tokens` — it is
rejected on this model family.

**Budget:** `Budget` caps a batch in requests and in dollars, and is checked before each call. An
unattended run over a session must not be able to cost an unbounded amount.

### Pointing at a gateway

`AnthropicLLM` takes `base_url` and `headers` (or reads `ANTHROPIC_BASE_URL`), which covers any
proxy speaking the Anthropic wire format, including ones authenticating by header rather than by
`x-api-key`.

`ProxyLLM` is Indeed's gateway, which is **OpenAI-shaped** — `POST /openai/v1/chat/completions`,
`Authorization: Bearer`, `gpt-4.1-mini` — so it is a separate class rather than a `base_url`. That
is the case the protocol exists for. Modelled on `hiring-criteria-mock-ux`, including the key
resolution order: the Vault-rendered properties file first, then `LLM_PROXY_API_KEY`, then
`VITE_INDEED_LLM_API_KEY`.

It talks stdlib HTTP rather than pulling in `openai` — one POST of JSON to one endpoint, the same
reasoning that keeps the web server on `http.server`.

To run against it, write the key into a gitignored file in the working directory:

```
# .llm.properties  -- matched by *.properties in .gitignore
dominik-personal-llm-proxy.api-key=...
```

Preferred over `export LLM_PROXY_API_KEY=...`, which both still work: an
environment variable is visible to every process the shell starts and turns up in a stray `env`,
where a file is read only by the thing that needs it. **Never paste a key into a chat transcript** —
those are written to disk and sent to a model provider, and a key that has been in one is burned.

**Keys.** `api_key` is `repr=False` on both clients, because a dataclass prints every field it has
and these end up in tracebacks. Gateway errors are swallowed rather than logged, since the response
body can echo the prompt back.

One caveat that is not technical: this is a personal repo for a hobby project, and the gateway is an
employer's. Whether that is an acceptable use is a question about policy, not about wiring, and the
wiring being easy is not an answer to it.

## Conventions

- Prompts live in `agent/prompts/` as files, not inline string literals.
- Ranges and charts are **data** (checked-in files), not Python literals.
- Every stage is independently invocable via `cli.py` and writes its output to the corpus, so a
  failed stage 3 never forces a re-parse.
- Cache anything a solver provider returns, keyed on the abstract spot, not on provider internals.
