# How a persona decides

Every memory is a noisy witness about a claim. The pipeline below turns a set of
witnesses of mixed provenance into one decision, and learns from what happens next.
Every constant is in `.rncrn8/config.yaml`.

## 1. Reliability is a Beta distribution

Each source class, and each origin once it has a track record, carries
`Beta(a, b)`: pseudo-counts of confirmations and contradictions.

    reliability mean = a / (a + b)
    strength         = a + b          (how sure we are of the mean)

Priors per source class are the persona's trust profile. Defaults:

| source    | a | b | mean | halflife | review after |
|-----------|---|---|------|----------|--------------|
| outcome   | 9 | 1 | 0.90 | none     | never        |
| human     | 8 | 2 | 0.80 | 3 years  | never        |
| project   | 7 | 3 | 0.70 | none     | never        |
| history   | 6 | 4 | 0.60 | 2 years  | never        |
| inference | 5 | 5 | 0.50 | 1 year   | never        |
| internet  | 4 | 3 | 0.57 | 180 days | 180 days     |

A recorder may state a confidence for one entry. That re-centres the mean and
keeps the strength, so a confident claim from a weak source is still easy to move.

## 2. Evidence combines in log-odds

    L0  = ln( p0 / (1 - p0) )                       prior
    w_i = ln( r_i / (1 - r_i) )                     weight of evidence, clipped to +/- max_witness_weight
    d_i = 2 ^ ( -age_i / halflife_source(i) )       time decay
    c_i = n_cluster(i) ^ ( -dependence_exponent )    dependence discount
    s_i = stale_factor if past review date else 1
    L   = L0 + sum_i  sign_i * w_i * d_i * c_i * s_i
    p   = 1 / (1 + e^-L)

Witnesses sharing a `root` form a cluster: ten posts repeating one study are one
witness with sqrt(10) the weight, not ten witnesses. A cited memory with
reliability at or below one half contributes nothing (`allow_negative_reliability:
false`), because a vendor blog saying X is weak evidence for X, not evidence
against it. A persona's own reading is the exception: its confidence is a
probability, and after calibration an observed hit rate, so below one half it
counts against the claim. A persona wrong two times in three at 0.6 is, when it
says 0.6, evidence of the opposite, and the report marks such a reading
`counts against`.

A human memory with no explicit root gets one automatically: `who|origin`, lower-cased
with whitespace collapsed, so one person in one conversation is one source however
many things they said. It is an exact-match key: 'planning meeting' and 'planning
meeting 2026-11-04' are two sources, and `--root` says otherwise when they are not.
The root is derived when a memory is read as well as when it is written, so memories
recorded before this rule cluster too.

Conflict (live evidence on both sides) is reported, never averaged away.

### The persona's own reading is a witness too

In a review, each finding the persona raises becomes a memory entry of source
`inference` (origin `inference:<persona>`), and that entry is one more witness
for the finding, at the confidence the persona stated. Cited memories are the
other witnesses. So an uncited finding stands on the persona's word alone, and a
finding backed by a recorded outcome is hard to dismiss. Outcomes later move
both the entry and the `inference:<persona>` origin, which is how a persona's
track record accumulates; calibration of stated confidence against observed
frequency arrives in deliverable 4.

### Other personas' readings in the debate

A persona's own earlier inferences, when it cites them, are not witnesses: the
same reader saying the same thing again adds nothing, so they are counted and
the persona's current reading carries its opinion once.

In the debate round another persona's `agree` or `disagree` on a finding is one
more witness, at the responder's stated confidence, with the sign of its stance.
Every reading of one finding, the owner's and the responders', shares the
dependence cluster `reading:<finding id>`, so n personas agreeing add sqrt(n)
times one reading's weight rather than n times: they looked at the same target
with the same kind of eyes. Memories a responder cites join as ordinary witnesses
with their own provenance. The decision is then re-run with the owner's loss
matrix and the decision record is re-written in place (revision bumped), so an
outcome recorded later attributes credit to every witness, including responders'
readings in their own memory stores.

## 3. Decide by expected cost

    raise finding  iff  p * C_miss  >  (1 - p) * C_false_alarm
    threshold      =    C_false_alarm / (C_false_alarm + C_miss)
    severity       =    p * C_miss

Each persona has its own loss matrix. A security persona fears misses; a skimmer
fears false alarms because noise kills adoption. Findings rank by severity.

## 4. Learn from outcomes

When a decision's claim is confirmed or refuted:

    share_i  = |contribution_i| / sum_j |contribution_j|
    agreed_i = (witness said supports) == (claim was true)
    a_i += eta * share_i   if agreed   else   b_i += eta * share_i

The entry's own counts move, and so do the counts of its origin in
`reliability.json`, so the next memory from that origin inherits what it earned.

An outcome a person records uses the full learning rate. An outcome a persona
inferred, by reporting one of its own earlier findings as addressed or wrong when
it reviews the same target again, uses `outcomes.implicit_learning_rate` and is
marked `implicit:<review id>` on the decision. An outcome inferred from a later
commit that changed the lines a code finding cited uses
`outcomes.commit_learning_rate` and is marked `implicit:commit:<sha>`; it is the
weakest signal of the three, since lines also change for unrelated reasons.

### Calibration of stated confidence

Every closed decision gives each reading on it a point: what the persona said
(stated confidence, sign of its stance) and whether it was right. Per persona,
pool adjacent violators fits a monotone map from stated confidence to observed
hit rate, and each block of the map is shrunk toward the identity by its own count:

    calibrated(x) = x + a_b * (m_b - x)    for x in block b,   a_b = n_b / (n_b + prior_strength)

where m_b is the block's hit rate and n_b the outcomes in it; neighbouring blocks
with the same hit rate count as one. A block built on one outcome barely moves the
value; one built on ten mostly replaces the word. Between
blocks the map runs straight from one block's end to the next block's start, and
the block edges are made monotone. Outside the range of confidences the persona has
actually stated, the correction fades to nothing over 0.2, so a persona that has
only ever said 0.6 to 0.75 is taken near its word when it says 0.5. Below
`calibration.min_points` outcomes the persona is taken at its word. Reviews use
the calibrated value for the reading witness and record both numbers, so a replay
can refit.

### Memory selection tags

Inference memories carry `kind:<target kind>` and `target:<stable identity>`
tags, where the identity is set by the adapter: the repository name for a diff,
the file name for a document. Selection prefers same-target, then same-kind
memories and penalises other kinds. Entries recorded before these tags existed
carry neither and are ranked by keyword overlap and recency alone.

## 5. Panel: multiplicative weights (Hedge)

    W_k <- W_k * exp( -eta_panel * (brier_k - 0.25) )   after each outcome, for personas that read it
    scale_k = ( W_k / uniform ) ^ influence              multiplier on persona k's reading witnesses

0.25 is the Brier loss of saying one half. Only personas whose readings took a
side on the decision are updated: one gains weight when its reading beat a coin
flip and loses when it did worse. A persona that did not read the outcome counts
as if it had said one half, so saying nothing earns nothing and costs nothing.
When every persona read the outcome, the shift cancels when the weights are
normalized, and this is plain Hedge. Without the shift, every outcome would lower
whoever was scored relative to everyone who was not, since a loss is never
negative: a persona right on a hundred findings no one else read would fall from
half the weight to 0.40 while a silent one rose to 0.60. Weights are relative, so
with one persona on record its weight is 1 whatever happens; the per-persona
Brier record that `rncrn8 panel` shows keeps its history. Weights live in `panel.json`. The scale enters the
evidence sum as one more factor on reading witnesses, never on cited memories,
and `panel.influence` (0 to 1) says how hard it bites. Calibration already fixes
systematic over- or under-confidence; the weights add a cheap global adjustment
while calibration data is thin, and the replay harness is the judge of whether
either earns its place.

## Tuning: the replay harness

Every decision stores its witnesses with raw fields (reliability, stance, age,
cluster, staleness, stated confidence for readings). `rncrn8 replay
--candidate other.yaml` orders closed decisions by time, fits calibrators and
panel weights on the older part, re-scores the newer part under the current and
the candidate config, and reports Brier, log loss, AUC and expected calibration
error. A change is accepted only if the candidate scores better on that held-out
part. Source-class priors cannot be replayed this way yet, because an entry's
reliability at decision time is data, not a function of the config.

## Extensions that do not require a rewrite

- per-origin reliabilities instead of per-class (already partly there via `reliability.json`)
- reliability conditioned on niche or target kind
- a small Bayesian network when correlated sources become a measured problem
