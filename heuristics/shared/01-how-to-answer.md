# How to use these

You are estimating a **range**: the set of hands a player could hold given
everything they have done in this hand, weighted by how likely each one is.

You are not being asked for a verdict, an EV figure, or a recommendation. The
arithmetic is done deterministically from your range — pot odds, equity and EV
are computed, not guessed — so the range is the only thing that has to be right,
and the only thing that is yours to produce.

Two consequences:

- **Prefer being roughly right and honest about width** over being precise and
  confident. A range that is too narrow produces a confidently wrong equity.
- **Say what you are unsure about.** A weight of 0.5 on a class means "half the
  time"; use it. Ranges with every hand at 1.0 are almost always wrong.

Everything below is a **prior**, not a rule. It describes how a competent
regular plays. The specific pool here is $0.01/$0.02 and $0.05/$0.10 six-max on
America's Cardroom, which deviates from every one of these — see the population
notes, and prefer a read from the actual hand over a heuristic whenever the two
disagree.
