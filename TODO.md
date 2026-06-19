# TODO / improvement backlog

## Formula correctness

### Recency `max(v)` per window is opaque
The formula takes the most recently-dated PR in each window as the base decay
value, then adds a burst bonus for additional PRs. Older-in-window PRs
contribute nothing to the base - they silently vanish. A more transparent
alternative: sum all decayed values in the window, then cap the sum at
`max(v) * burst_multiplier` to retain burst protection without the implicit
discard.

### `review_activity` only covers merged PRs
Reviews on open or ultimately-closed PRs are not counted. A contributor who
does most of their review on PRs that never merge (e.g. early feedback,
design PRs) is undercounted. Extending the GraphQL query to include all PRs
regardless of state would fix this, at the cost of a larger query.

---

## Missing signals

### PR revision count
PRs that merged cleanly after few review rounds are higher-quality than PRs
that required many back-and-forth cycles. `log1p(merged_prs / review_rounds)`
could reward clean, well-prepared work. Requires fetching review comment counts
from the GraphQL query.

---

## Transparency and auditability

### Per-contributor score explanation
The cron job logs caps and final team changes but does not explain *why* each
score landed where it did. A `--explain @login` flag that prints the normalized
value for each feature alongside the cap and weight would make the model
auditable without reading the source.

### Export training data for weight calibration
Add `--export-csv` to `trust_score.py` that writes a row per contributor with
their normalized feature vector. Operators can label a subset of contributors
as trusted/untrusted and fit `sklearn.linear_model.LogisticRegression` to derive
repo-specific importance weights.

### Score trend
Store the previous run's scores (e.g. as a GitHub Actions artifact or step
summary) and report delta next run. Contributors trending strongly upward are
near-trusted and worth human review; contributors trending down may warrant
removal from the team before the threshold triggers.

---

## Configuration

### `window_size_days` should be per-feature
Currently a single global value controlling both the burst-bucketing window for
`recency` and the temporal grid for `engagement` pair gaps. These could
reasonably differ - e.g. 14-day windows for burst protection, 30-day windows
for engagement granularity.

### `review_activity` shares `half_life_days` with `recency`
The decay rate for review activity is taken from `features.recency.half_life_days`.
If a repo wants review history to decay faster or slower than PR history, there
is no way to express that today. Adding a dedicated `half_life_days` key under
`features.review_activity` would allow independent tuning.

### `diff_size` measures volume not quality
Additions + deletions conflates a 500-line refactor (few additions, many
deletions) with a 500-line generated-file commit. Consider separate `additions`
and `deletions` features, or weight deletions differently as a proxy for
cleanup work.

---

## Robustness

### ~~GraphQL pagination is unbounded~~ *(resolved)*
Added `scoring.max_prs` (default `500`, `0` = unlimited). Query now orders
`DESC` so the limit takes the most recent PRs. Bot-type accounts are also
filtered via `__typename` in the query response rather than only by suffix.

### Missing test suite
The scoring logic (`compute_raw_features`, `normalize`, `compute_score`) is
pure Python with no external dependencies and is straightforward to unit-test.
A small `pytest` suite with synthetic PR histories would catch regressions when
formula parameters change.
