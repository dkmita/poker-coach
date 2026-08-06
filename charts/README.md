# Exported solver ranges

One directory per `spot_key`, one `.txt` per action, in PioSolver/GTO+ range
notation. Read by `poker_coach.solvers.ranges.ChartProvider`.

## Getting them out of GTO Wizard

Use the product's own export — Ranges tab, the **copy** button above a player's
range ([docs](https://help.gtowizard.com/ranges-tab/)). It emits standard
PioSolver text. Paste one file per action.

Do **not** automate the site. GTO Wizard's terms prohibit automated requests and
scripts within the Service, and their public API is a *benchmarking* API that
deliberately excludes solver access. Exported text is also the more durable
integration: it needs no credentials, survives their UI changing, and works
offline and in CI.

The same format comes out of TexasSolver and GTO+, so anything here is
source-agnostic.

## Format

    AA:1.0, KK:1.0, AKs:0.75, AJo:0.5, 77+, A2s+

A bare hand means weight 1.0. `+` expands upward: `77+` is sevens through aces,
`A2s+` every suited ace. `fold.txt` is optional — if absent it's inferred as the
remainder, since exports usually cover only the continuing actions.

## Naming a spot directory

Match `spot_key` exactly as `handview` builds it, or the lookup misses:

    {position}_{street}_{unopened|vs_{POS}_{action}_{size}bb}_{depth}bb
    BB_preflop_vs_UTG_raise_2.5bb_100bb

Depth is bucketed to the nearest 25bb.
