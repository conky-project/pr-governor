# pr-governor

Reusable GitHub Actions for PR governance based on contributor trust scores.
Designed to prevent compromised-credential attacks where a malicious PR is
approved by an account with stolen credentials.

## How it works

Three actions work together:

**`trust-cron`** runs on a schedule (e.g. weekly). It fetches the full merged
PR history of your repo, computes a trust score for every contributor, and
syncs a GitHub Team with those who exceed the threshold. Nothing is committed
to your repository - team membership is the only persistent state.

**`approval-check`** runs on every PR event and review. It enforces three
rules and posts a required commit status (`pr-governance/approval-check`):

1. The repo owner's approval always passes outright.
2. No one who authored a commit in the PR may also approve it (no
   self-approval).
3. At least one approval must come from a member of the trusted-contributors
   team.

**`pr-guard`** runs when a PR is pushed to or when a governance command is posted.

- Force pushes that rewrite history trigger an unconditional dismiss.
- If the new commit range introduces an author who was not present before the existing approvals were given, all approvals are dismissed.
- If the PR author's own commits are GPG-verified, `pr-guard` automatically enables `required_signatures` on the head branch. GitHub then enforces this server-side: an attacker with stolen credentials but no signing key cannot push to the branch at all, even outside the PR review process.

**PR governance commands** (post as a PR comment):

| Command | Who can use it | Effect |
|---------|---------------|--------|
| `/allow-unsigned` | PR author, repo owner | Removes the signature requirement; existing approvals are kept |
| `/approve persistent` | Any reviewer | Marks your approval as persistent - it will not be auto-dismissed by force pushes or new authors |
| `/approve discard` | Any reviewer | Removes your persistent approval; your approval may now be auto-dismissed |

**Persistent approvals** let a reviewer signal that they approve the overall direction of a PR and do not need to re-review after routine rebases or minor history changes. A persistent approval survives governance events that would otherwise dismiss all reviews. The last `/approve` command per user wins. `/approve persistent` (shorthand: `/approve p`) enables it; `/approve discard` or `/approve` alone removes it.

Commands are recognized on any line of a comment body, so you can write rationale before or after the command. Any line starting with `/` is parsed as a command.

**The signature lock** is tied to the PR author - if a reviewer or maintainer pushes a signed commit onto someone else's branch, the lock is **not** activated.

### Security model

All three workflows use `pull_request_target`, which always runs workflow files
from the **base branch**, never from the PR head. The implementation lives in
this repo (`conky-project/pr-governor`) - no PR to your repo can reach or
modify the governance logic. Changes to the thin caller workflows and config in
your repo require owner review via CODEOWNERS, breaking the circularity where
the governance system could approve its own weakening.

## Setup

### 1. GitHub organization

The trusted-contributors team lives in a GitHub **organization** (personal
accounts do not support Teams). Create a free org if you do not already have
one - the team is created automatically on the first `trust-cron` run.

### 2. Secrets

| Secret | Scope | Used by |
|--------|-------|---------|
| `ORG_TOKEN` | `write:org` | `trust-cron` (manages team membership) |
| `ORG_TOKEN` | `read:org` | `approval-check` (queries team membership) |
| `ORG_TOKEN` | `read:org` | `pr-guard` (`/allow-unsigned` auth for org owners) |

A single PAT with `write:org` covers all three. Add it to your repo secrets as `ORG_TOKEN`.

The `pr-guard` workflow also requires `administration: write` on the Actions `GITHUB_TOKEN` (declared in the caller workflow `permissions` block) so it can set branch-level signature requirements. This permission is automatically granted - no secret needed.

### 3. Caller workflows

Add these three files to `.github/workflows/` in your repo:

**`.github/workflows/approval-check.yml`**
```yaml
name: Approval Check
on:
  pull_request_target:
    types: [opened, synchronize, reopened]
  pull_request_review:
    types: [submitted, dismissed]
permissions:
  pull-requests: read
  statuses: write
  contents: read
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: conky-project/pr-governor/approval-check@main
        with:
          org-token: ${{ secrets.ORG_TOKEN }}
          pr-number: ${{ github.event.pull_request.number }}
          head-sha: ${{ github.event.pull_request.head.sha }}
```

**`.github/workflows/pr-guard.yml`**
```yaml
name: PR Guard
on:
  pull_request_target:
    types: [synchronize]
  issue_comment:
    types: [created]
permissions:
  pull-requests: write
  administration: write
  contents: read
jobs:
  guard:
    runs-on: ubuntu-latest
    # For issue_comment events, skip plain issue comments (no pull_request key).
    if: >
      github.event_name == 'pull_request_target' ||
      (github.event_name == 'issue_comment' &&
       github.event.issue.pull_request != null)
    steps:
      - uses: conky-project/pr-governor/pr-guard@main
        with:
          org-token: ${{ secrets.ORG_TOKEN }}
          pr-number: ${{ github.event.pull_request.number || github.event.issue.number }}
          before-sha: ${{ github.event.before }}
          after-sha: ${{ github.event.pull_request.head.sha }}
```

**`.github/workflows/trust-cron.yml`**
```yaml
name: Update Trusted Contributors
on:
  schedule:
    - cron: "0 2 * * 0"  # weekly
  workflow_dispatch:
permissions:
  contents: read
jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: conky-project/pr-governor/trust-cron@main
        with:
          org-token: ${{ secrets.ORG_TOKEN }}
```

### 4. Branch protection

On your default branch, enable:

- **Require status checks to pass** → add `pr-governance/approval-check`
- **Require review from Code Owners**

### 5. CODEOWNERS

Protect the governance files so they require owner review:

```
/CODEOWNERS                               @your-username
/.github/trust-config.yml                 @your-username
/.github/workflows/approval-check.yml     @your-username
/.github/workflows/pr-guard.yml           @your-username
/.github/workflows/trust-cron.yml         @your-username
```

### 6. Configuration

Create `.github/trust-config.yml` in your repo with at minimum:

```yaml
github:
  org: "your-org-name"
```

All other values inherit from the [bundled defaults](trust-config.yml). Run
`trust-cron` manually once (`workflow_dispatch`) to seed the initial team.

## Configuration reference

All settings are optional except `github.org`. The full set of defaults with
documentation is in [`trust-config.yml`](trust-config.yml).

### `scoring`

| Key | Default | Description |
|-----|---------|-------------|
| `window_size_days` | `30` | Bucket size for burst-prevention and consistency counting |

### `features`

Each feature has two keys:

- **`full_credit_at`** - what value earns a normalized score of 1.0. Accepts
  a literal number in the feature's natural unit, a percentile string like
  `p99` (data-driven cap), or a duration string like `2y` / `20mo` / `30d`
  for time-based features. Values above the cap are clamped to 1.0.
- **`importance`** - relative weight in the final score. Values are
  auto-normalized by their sum, so any scale works.

| Feature | Natural unit | Default `full_credit_at` | Default `importance` | What it measures |
|---------|-------------|--------------------------|----------------------|-----------------|
| `recency` | decay sum | `p99` | `2` | Burst-protected exponential recency sum |
| `review_activity` | decay sum | `p99` | `2` | Recency-decayed reviews given on merged PRs |
| `engagement` | pairwise sum | `p99` | `3` | Breadth × depth of contribution history |
| `volume` | PRs | `20` | `1` | Total merged PR count |
| `diff_size` | lines | `300` | `1` | Median lines changed per PR |

The `recency` feature also accepts:

- **`half_life_days`** (default `2y`): contributions older than the half-life lose ~50% of their weight. Also controls the decay rate for `review_activity`.
- **`burst_bonus`** (default `0.1`): extra-credit coefficient when multiple PRs land in the same 30-day window.
- **`inactivity_cutoff`** (default `2y`): contributors with no activity (PR **or** review) beyond this window have their final score overridden to -1, removing them from the trusted team regardless of historical record. Root-of-trust accounts are exempt.

The `engagement` feature also accepts `full_credit_window_prs` (default `5`):
the number of PRs per 30-day window that counts as "fully active". The formula
sums `min(c_i, c_j) × log1p(gap_months)` over every pair of active windows,
rewarding both long contribution history and sustained activity within each window.

### `thresholds`

| Key | Default | Description |
|-----|---------|-------------|
| `trusted_approver` | `0.0` | Minimum score on the [-1, 1] scale for team membership |

A score of `0.0` means "above the midpoint between zero and the p99
contributor". Raise it to be stricter; lower it to be more permissive.

### Root of trust

No configuration needed. The root of trust is derived automatically from
`GITHUB_REPOSITORY_OWNER` at runtime:

- **Personal account** - that account is the root of trust.
- **Organization** - all members with the org `owner` role are roots of trust.

Root-of-trust approvals always pass the check outright, and those accounts are
always included in the trusted-contributors team regardless of score.

### `github`

```yaml
github: "your-org"                      # uses default team slug "trusted-contributors"
github: "your-org/custom-team-slug"     # explicit team slug
```

Required. Must be a GitHub organization account (not a personal account) to
support Teams. The team is created automatically if it does not exist.

## Scoring model

Each contributor's merged PR history is reduced to five independent features,
each normalized to [0, 1] against its `full_credit_at` cap. A weighted mean
is then mapped to [-1, 1]:

```
score = 2 * weighted_mean(normalized_features) - 1
```

Because the combination is a linear weighted sum of fixed-transform features,
the importance weights can be tuned by fitting a `LogisticRegression` model on
a labeled set of contributors:

1. Export the normalized feature matrix (add `--export-csv` to a local run).
2. Label contributors as trusted/untrusted.
3. Fit `sklearn.linear_model.LogisticRegression` and read off the coefficients.
4. Set those coefficients as `importance` values in `trust-config.yml`.

The deployed scoring remains a transparent, auditable formula in the config —
ML only informs the weight values.
