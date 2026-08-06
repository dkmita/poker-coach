-- poker-coach corpus schema (SQLite)
--
-- This database is NOT the system of record for hands. A hand is a `.phh` file
-- on disk (the PHH open standard) and `pokerkit` replays it to produce game
-- state. What lives here is:
--
--   1. a thin index over that archive, so filtering the corpus is a query
--      instead of a full rescan, and
--   2. every piece of mutable pipeline state, which is the part PHH has no
--      opinion about.
--
-- The split follows mutability. A hand is write-once, which is what files are
-- good at. Findings get superseded when a prompt improves, leak status moves
-- open -> fixed, flagged decisions get re-queued after a detector changes, and
-- "pending work, most expensive first" is an ordered query. That is what a
-- database is for.
--
-- Correspondingly, the `hands` table indexes only what you FILTER on. Detail
-- stays in the `.phh` file. Add a column when a query proves slow, not by
-- default -- mirroring the action list into SQL would be duplicated state with a
-- drift failure mode, against a replay engine that already owns the problem.
--
-- Conventions, applied without exception:
--
--   money   INTEGER cents. Never REAL. SQLite has no decimal type, so REAL
--           silently accumulates error across a session of pot arithmetic. Big
--           blinds are derived at read time (see v_finding_ev) and never summed.
--   time    TEXT, ISO-8601, UTC: '2026-08-05T22:14:03Z'. Sorts
--           lexicographically, which is why it beats a numeric epoch here.
--   enums   TEXT with a CHECK constraint. This database is meant to be opened
--           in a SQLite browser and read by hand; 'river' beats 3.
--   cards   Canonical: uppercase rank, lowercase suit ('Ah', 'Td'), concatenated
--           in dealt order.
--
-- Callers must run `PRAGMA foreign_keys = ON` per connection -- SQLite defaults
-- it off, so the constraints below are advisory until you do.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Pipeline provenance
-- ---------------------------------------------------------------------------

-- One row per stage execution. Derived rows point back at the run that produced
-- them, so a bad detector or a regressed prompt can be found and its output
-- deleted without touching the archive.
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY,
    stage        TEXT NOT NULL CHECK (stage IN ('ingest', 'triage', 'analyze', 'synthesize')),
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'ok', 'failed', 'cancelled')),
    hands_in     INTEGER,
    hands_out    INTEGER,
    -- Agent stages only. Recording the model per run is what makes a quality
    -- change traceable to a model change.
    model        TEXT,
    cost_usd     REAL,
    error        TEXT,
    notes        TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_stage_started ON runs (stage, started_at DESC);

-- ---------------------------------------------------------------------------
-- Index over the PHH archive
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS hands (
    id             INTEGER PRIMARY KEY,

    -- Dedupe key. Re-reading a history file must not create a second row, so
    -- ingest is idempotent by construction. Neither field is native PHH: the
    -- spec has `hand` (a counter), not a site-scoped unique id, so these are
    -- carried as user-defined PHH fields (_pc_site, _pc_site_hand_id).
    site           TEXT NOT NULL,
    site_hand_id   TEXT NOT NULL,

    -- The system of record. Everything not indexed here is recoverable by
    -- replaying this file; the hash detects a site silently reformatting its
    -- histories, or an archive file being edited underneath us.
    phh_path       TEXT NOT NULL,
    phh_sha256     TEXT NOT NULL,

    played_at      TEXT NOT NULL,
    -- Minutes east of UTC at the table's local time, so sessions can be cut on
    -- the player's clock. A session crossing midnight UTC is still one session.
    tz_offset_min  INTEGER NOT NULL DEFAULT 0,

    -- bb is the divisor for every big-blind normalization in the system.
    bb             INTEGER NOT NULL CHECK (bb > 0),
    currency       TEXT NOT NULL DEFAULT 'USD',
    -- Number actually dealt in, which determines the position set -- not the
    -- table's capacity.
    players_dealt  INTEGER NOT NULL CHECK (players_dealt BETWEEN 2 AND 10),

    hero_position  TEXT NOT NULL
                   CHECK (hero_position IN ('UTG', 'UTG+1', 'HJ', 'CO', 'BTN', 'SB', 'BB')),
    -- Hero vs the deepest opponent still live preflop. Indexed because stack
    -- depth selects a strategy: 100bb and 40bb are different games, so this is a
    -- constant filter rather than a detail.
    eff_stack_bb   REAL NOT NULL CHECK (eff_stack_bb > 0),
    street_reached TEXT NOT NULL
                   CHECK (street_reached IN ('preflop', 'flop', 'turn', 'river')),

    -- Money result, net of rake. Deliberately not an EV judgement. Its job is
    -- reconciliation: PHH finishing_stacks is ground truth, so a mismatch here
    -- means the parser is wrong.
    hero_net       INTEGER NOT NULL,
    -- PHH has no rake field -- the spec's position is that rake is reconstructed
    -- from finishing stacks. Done at ingest and stored, because rake is what
    -- makes marginal opens unprofitable; a model that cannot express it rates
    -- the loosest opens as fine.
    rake           INTEGER NOT NULL DEFAULT 0 CHECK (rake >= 0),

    ingest_run_id  INTEGER REFERENCES runs (id) ON DELETE SET NULL,
    ingested_at    TEXT NOT NULL,

    UNIQUE (site, site_hand_id)
);

CREATE INDEX IF NOT EXISTS idx_hands_played_at ON hands (played_at DESC);
-- The shape most triage and leak queries take: a position at a stack depth.
CREATE INDEX IF NOT EXISTS idx_hands_spot
    ON hands (hero_position, eff_stack_bb, bb);
CREATE INDEX IF NOT EXISTS idx_hands_phh_path ON hands (phh_path);

-- ---------------------------------------------------------------------------
-- Stage 2: triage output
-- ---------------------------------------------------------------------------

-- A hero decision that cheap deterministic checks think is worth paying an agent
-- to judge. A suspicion, not a verdict.
--
-- The unit is the DECISION, not the detector. Two rules can fire on the same
-- loose river call; one row per detector would produce two findings for one
-- mistake and double-count its cost in leak totals -- inflating exactly the
-- leaks whose detectors overlap most.
--
-- Roughly 10% of hero decisions should land here. If that fraction climbs the
-- funnel is broken, and stage 3 cost scales with volume instead of with signal.
CREATE TABLE IF NOT EXISTS flagged_decisions (
    id            INTEGER PRIMARY KEY,
    hand_id       INTEGER NOT NULL REFERENCES hands (id) ON DELETE CASCADE,
    -- pokerkit's action ordering within the replayed hand. Stable across
    -- re-analysis because it is the engine's ordering, not ours.
    action_index  INTEGER NOT NULL,

    -- Canonical spot description, the chart and solver lookup key, e.g.
    -- 'BB_vs_BTN_open_2.5bb'.
    spot_key      TEXT NOT NULL,
    -- Queue ordering only, so the most expensive analysis reaches the most
    -- expensive-looking mistakes first.
    priority      REAL NOT NULL DEFAULT 0,

    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'analyzed', 'skipped', 'failed')),
    run_id        INTEGER REFERENCES runs (id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL,

    -- One row per decision. Re-running triage updates in place.
    UNIQUE (hand_id, action_index)
);

-- The stage-3 work queue.
CREATE INDEX IF NOT EXISTS idx_flagged_queue
    ON flagged_decisions (status, priority DESC) WHERE status = 'pending';

-- Which rules fired on a decision, and what each guessed it cost. Many-to-many
-- so per-detector precision stays measurable without splitting the decision.
-- "Two independent checks flagged this" is also a stronger prior for the agent
-- than one.
CREATE TABLE IF NOT EXISTS flagged_decision_detectors (
    flagged_decision_id INTEGER NOT NULL
                        REFERENCES flagged_decisions (id) ON DELETE CASCADE,
    -- Stable slug, e.g. 'bb_defend_underfold'. Precision metrics and reports
    -- group by this, so a rename splits a detector's history in two.
    detector            TEXT NOT NULL,
    est_ev_lost         INTEGER,
    note                TEXT NOT NULL DEFAULT '',

    PRIMARY KEY (flagged_decision_id, detector)
);

CREATE INDEX IF NOT EXISTS idx_fdd_detector ON flagged_decision_detectors (detector);

-- ---------------------------------------------------------------------------
-- Stage 3: agent findings
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS findings (
    id                  INTEGER PRIMARY KEY,
    flagged_decision_id INTEGER NOT NULL
                        REFERENCES flagged_decisions (id) ON DELETE CASCADE,
    -- Denormalized so leak queries reach the hand without a three-way join.
    hand_id             INTEGER NOT NULL REFERENCES hands (id) ON DELETE CASCADE,
    action_index        INTEGER NOT NULL,

    verdict             TEXT NOT NULL
                        CHECK (verdict IN ('mistake', 'marginal', 'fine', 'unclear')),
    hero_action         TEXT NOT NULL,
    recommended_action  TEXT,

    -- THE ranking currency. Reports rank by money, never by error count, which
    -- is what keeps the output actionable instead of a wall of nitpicks.
    -- 0 for a 'fine' verdict; NULL when genuinely not estimable.
    ev_lost             INTEGER,
    confidence          REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),

    rationale           TEXT NOT NULL DEFAULT '',
    -- JSON: equity numbers, chart frequencies, solver output the verdict rests
    -- on. Retained so a finding is auditable without re-running the agent.
    evidence            TEXT,
    solution_id         INTEGER REFERENCES solutions (id) ON DELETE SET NULL,

    model               TEXT,
    run_id              INTEGER REFERENCES runs (id) ON DELETE SET NULL,
    created_at          TEXT NOT NULL,

    -- One live judgement per decision; re-analysis replaces rather than appends.
    UNIQUE (flagged_decision_id)
);

CREATE INDEX IF NOT EXISTS idx_findings_ev ON findings (ev_lost DESC)
    WHERE verdict IN ('mistake', 'marginal');
CREATE INDEX IF NOT EXISTS idx_findings_hand ON findings (hand_id);

-- ---------------------------------------------------------------------------
-- Stage 4: synthesized leaks
-- ---------------------------------------------------------------------------

-- A named, recurring pattern -- what the report is actually about. Findings are
-- per-hand evidence; a leak is the claim they support.
CREATE TABLE IF NOT EXISTS leaks (
    id              INTEGER PRIMARY KEY,
    -- Stable identifier, e.g. 'bb-overfold-vs-btn-open'. Cumulative history
    -- hangs off this, so renaming a slug orphans the trend.
    slug            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',

    -- Recomputed by each synthesis run rather than incremented, so a re-run over
    -- corrected findings self-heals.
    hand_count      INTEGER NOT NULL DEFAULT 0,
    total_ev_lost   INTEGER NOT NULL DEFAULT 0,
    -- Rate, not total: the honest way to compare a leak seen 500 times against
    -- one seen 12 times.
    ev_lost_per_100 REAL,

    first_seen_at   TEXT,
    last_seen_at    TEXT,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'monitoring', 'fixed', 'dismissed')),

    run_id          INTEGER REFERENCES runs (id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leaks_ranking ON leaks (status, total_ev_lost DESC);

-- Many-to-many on purpose: one loose call can be evidence for both a
-- preflop-range leak and a pot-odds-discipline leak, and forcing a single parent
-- would make the report's totals depend on an arbitrary tie-break.
CREATE TABLE IF NOT EXISTS finding_leaks (
    finding_id  INTEGER NOT NULL REFERENCES findings (id) ON DELETE CASCADE,
    leak_id     INTEGER NOT NULL REFERENCES leaks (id) ON DELETE CASCADE,
    -- Share of this finding's ev_lost attributed to this leak. Weights across
    -- one finding should sum to 1.0 so leak totals don't double-count money.
    weight      REAL NOT NULL DEFAULT 1.0 CHECK (weight > 0 AND weight <= 1.0),

    PRIMARY KEY (finding_id, leak_id)
);

CREATE INDEX IF NOT EXISTS idx_finding_leaks_leak ON finding_leaks (leak_id);

-- ---------------------------------------------------------------------------
-- Solver cache
-- ---------------------------------------------------------------------------

-- Keyed on the abstract spot, never on provider internals, so a solution fetched
-- from one provider is reusable by another. This is what lets the GTO Wizard
-- provider stay optional: with a warm cache nothing in the pipeline needs it.
CREATE TABLE IF NOT EXISTS solutions (
    id               INTEGER PRIMARY KEY,
    spot_key         TEXT NOT NULL,
    provider         TEXT NOT NULL,
    provider_version TEXT,

    request          TEXT NOT NULL,  -- JSON: the abstract spot as queried
    response         TEXT NOT NULL,  -- JSON: frequencies / EVs as returned

    retrieved_at     TEXT NOT NULL,
    -- Providers revise solutions; a stale entry should be refetchable without
    -- invalidating the whole cache.
    expires_at       TEXT,

    UNIQUE (spot_key, provider)
);

CREATE INDEX IF NOT EXISTS idx_solutions_spot ON solutions (spot_key);

-- ---------------------------------------------------------------------------
-- Views
-- ---------------------------------------------------------------------------

-- Findings with big-blind normalization applied. Aggregating raw cents across
-- stakes is the easy mistake: $5 lost at 200NL and $5 lost at 25NL are not the
-- same error. Query this instead of `findings` when comparing across stakes.
CREATE VIEW IF NOT EXISTS v_finding_ev AS
SELECT
    f.id                           AS finding_id,
    f.hand_id,
    f.action_index,
    f.verdict,
    f.ev_lost                      AS ev_lost_cents,
    CAST(f.ev_lost AS REAL) / h.bb AS ev_lost_bb,
    h.bb,
    h.played_at,
    h.site,
    h.hero_position,
    h.eff_stack_bb,
    h.phh_path,
    fd.spot_key
FROM findings f
JOIN hands h ON h.id = f.hand_id
JOIN flagged_decisions fd ON fd.id = f.flagged_decision_id;

-- Per-detector precision: of the decisions this rule flagged, how many turned
-- out to be real mistakes? A detector sitting at 5% is burning agent budget and
-- should be fixed or dropped. This number is the main defense against the funnel
-- silently degrading, which is why it gets a view rather than a one-off query.
CREATE VIEW IF NOT EXISTS v_detector_precision AS
SELECT
    d.detector,
    COUNT(*)                                        AS flagged,
    SUM(f.verdict = 'mistake')                      AS mistakes,
    SUM(f.verdict = 'marginal')                     AS marginal,
    SUM(f.verdict = 'fine')                         AS fine,
    SUM(f.id IS NULL)                               AS unanalyzed,
    CAST(SUM(f.verdict = 'mistake') AS REAL)
        / NULLIF(SUM(f.id IS NOT NULL), 0)          AS precision,
    SUM(COALESCE(f.ev_lost, 0))                     AS ev_lost_found
FROM flagged_decision_detectors d
JOIN flagged_decisions fd ON fd.id = d.flagged_decision_id
LEFT JOIN findings f ON f.flagged_decision_id = fd.id
GROUP BY d.detector
ORDER BY ev_lost_found DESC;

-- The report's front page: open leaks, most expensive first.
CREATE VIEW IF NOT EXISTS v_leak_ranking AS
SELECT
    l.slug,
    l.title,
    l.status,
    l.hand_count,
    l.total_ev_lost,
    l.ev_lost_per_100,
    l.last_seen_at,
    COUNT(fl.finding_id) AS finding_count
FROM leaks l
LEFT JOIN finding_leaks fl ON fl.leak_id = l.id
WHERE l.status IN ('open', 'monitoring')
GROUP BY l.id
ORDER BY l.total_ev_lost DESC;

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
