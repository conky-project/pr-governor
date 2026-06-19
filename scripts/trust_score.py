#!/usr/bin/env python3

from __future__ import annotations

import math
import sys
from collections import defaultdict
from datetime import datetime, timezone

import gh_api
import log
from settings import Config, FeatureConfig, parse_duration, require_env


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2) if s else 0.0


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = (p / 100.0) * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def compute_raw_features(
    entries: list[gh_api.PREntry],
    review_dates: list[datetime],
    now: datetime,
    half_life: float,
    window_size: int,
    burst_bonus: float,
    full_credit_window_prs: float,
) -> dict[str, float]:
    """Return raw (un-normalized) features for one contributor."""
    dates = [e[0] for e in entries]
    diffs = [e[1] + e[2] for e in entries]

    decay_rate = math.log(2) / half_life
    epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)

    buckets: dict[int, list[float]] = defaultdict(list)
    for d in dates:
        idx = (d - epoch).days // window_size
        age = max(0, (now - d).days)
        buckets[idx].append(math.exp(-decay_rate * age))

    recency = sum(
        max(v) * (1.0 + burst_bonus * math.log1p(len(v) - 1))
        for v in buckets.values()
    )

    review_activity = sum(
        math.exp(-decay_rate * max(0, (now - d).days))
        for d in review_dates
    )

    # Engagement: for each pair of active windows, reward the product of their
    # activity levels weighted by the log of the temporal gap between them.
    # c_i = min(prs_in_window / full_credit_window_prs, 1.0)
    sorted_windows = sorted(buckets.keys())
    c = {idx: min(len(v) / full_credit_window_prs, 1.0) for idx, v in buckets.items()}
    engagement = 0.0
    for k, wi in enumerate(sorted_windows):
        for wj in sorted_windows[k + 1:]:
            gap_months = (wj - wi) * window_size / 30.0
            engagement += min(c[wi], c[wj]) * math.log1p(gap_months)

    return {
        "recency":         recency,
        "review_activity": review_activity,
        "engagement":      engagement,
        "volume":          math.log1p(len(dates)),
        "diff_size":       math.log1p(_median(diffs)),
    }


# Features whose raw values are log1p-transformed in compute_raw_features.
# full_credit_at literals are in natural units (PRs, lines) and must be passed
# through log1p to reach feature space.
_LOG1P_FEATURES = {"volume", "diff_size"}


def _resolve_cap(
    feat: str,
    fc: FeatureConfig,
    all_raw: dict[str, dict[str, float]],
) -> float:
    """Return the normalization cap for one feature in feature space.

    Handles three forms of full_credit_at:
      - float  → natural unit (PRs / lines); log1p applied for log features
      - "p99"  → percentile over observed feature values (already in feature space)
      - "2y"   → duration string; treated as days (natural unit for time features)
    """
    fca = fc.full_credit_at
    if isinstance(fca, str):
        s = fca.strip()
        if s[0] in "pP" and s[1:].replace(".", "").isdigit():
            # Percentile string: work directly in feature space.
            return _percentile([v[feat] for v in all_raw.values()], float(s[1:])) or 1.0
        natural: float = parse_duration(s)
    else:
        natural = float(fca)

    cap = math.log1p(natural) if feat in _LOG1P_FEATURES else natural
    return cap or 1.0


def normalize(
    all_raw: dict[str, dict[str, float]],
    features: dict[str, FeatureConfig],
) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    """Normalize each feature to [0, 1]; values above the cap are clamped to 1.0."""
    feature_names = list(next(iter(all_raw.values())).keys())
    caps = {
        feat: _resolve_cap(feat, features[feat], all_raw)
        for feat in feature_names
    }
    normalized = {
        login: {feat: min(raw[feat] / caps[feat], 1.0) for feat in feature_names}
        for login, raw in all_raw.items()
    }
    return normalized, caps


def compute_score(normalized: dict[str, float], features: dict[str, FeatureConfig]) -> float:
    """Importance-weighted mean of normalized features mapped to [-1, 1].

    Importance values are auto-normalized by their sum, so any relative scale works.
    """
    total = sum(f.importance for f in features.values())
    if total == 0:
        return -1.0
    wsum = sum(features[feat].importance * val for feat, val in normalized.items() if feat in features)
    return 2.0 * (wsum / total) - 1.0


def main() -> int:
    env = require_env("ORG_TOKEN", "GITHUB_REPOSITORY", "GITHUB_REPOSITORY_OWNER")
    token = env["ORG_TOKEN"]
    repo_owner = env["GITHUB_REPOSITORY_OWNER"]
    owner, name = env["GITHUB_REPOSITORY"].split("/", 1)

    cfg = Config.load()

    if not gh_api.ensure_team(cfg.org, cfg.team_slug, token):
        log.error(
            f"Failed to ensure team '{cfg.team_slug}' exists in org '{cfg.org}'",
            title="Team setup failed",
        )
        return 1

    log.info(f"Fetching merged PRs for {owner}/{name} …")
    try:
        author_prs, reviewer_dates = gh_api.fetch_merged_prs(
            owner, name, token, cfg.ignore_users, cfg.max_prs
        )
    except Exception as exc:
        log.error(f"Failed to fetch PRs: {exc}", title="GitHub API error")
        return 1

    total_prs = sum(len(v) for v in author_prs.values())
    log.info(
        f"Found {total_prs} merged PRs from {len(author_prs)} contributors; "
        f"{len(reviewer_dates)} reviewers"
    )

    now = datetime.now(timezone.utc)
    recency_cfg = cfg.features["recency"]
    half_life = recency_cfg.half_life_days
    burst_bonus = recency_cfg.burst_bonus
    inactivity_cutoff = recency_cfg.inactivity_cutoff_days
    full_credit_window_prs = cfg.features["engagement"].full_credit_window_prs

    all_logins = set(author_prs) | set(reviewer_dates)
    all_raw = {
        login: compute_raw_features(
            author_prs.get(login, []),
            reviewer_dates.get(login, []),
            now, half_life, cfg.window_size_days, burst_bonus, full_credit_window_prs,
        )
        for login in all_logins
    }

    normalized, caps = normalize(all_raw, cfg.features)

    log.group("feature caps")
    for feat, cap in caps.items():
        fc = cfg.features[feat]
        src = fc.full_credit_at if isinstance(fc.full_credit_at, str) else f"{cap:.3f}"
        log.info(f"  {feat}: {cap:.3f}  (full_credit_at={src})")
    log.endgroup()

    root = gh_api.get_root_of_trust(repo_owner, token)

    scores = {
        login: compute_score(normalized[login], cfg.features)
        for login in normalized
    }
    for login in root:
        scores[login] = max(scores.get(login, -1.0), 1.0)

    # Hard inactivity cutoff: contributors with no activity (PR or review) in
    # the last inactivity_cutoff_days score -1 regardless of history.
    # Root-of-trust accounts are exempt.
    for login in list(scores):
        if login in root:
            continue
        pr_dates = [e[0] for e in author_prs.get(login, [])]
        rev_dates = reviewer_dates.get(login, [])
        all_dates = pr_dates + rev_dates
        if not all_dates:
            continue
        days_inactive = (now - max(all_dates)).days
        if days_inactive > inactivity_cutoff:
            scores[login] = -1.0
            log.info(
                f"  @{login} inactive for {days_inactive}d (cutoff {int(inactivity_cutoff)}d)"
                " - score overridden to -1.0"
            )

    desired: set[str] = {
        login for login, s in scores.items() if s >= cfg.threshold
    } | root

    current = gh_api.get_team_members(cfg.org, cfg.team_slug, token)
    if current is None:
        log.error(
            f"Team '{cfg.team_slug}' not found after creation attempt",
            title="Team not found",
        )
        return 1

    for login in sorted(desired - current):
        ok = gh_api.add_member(cfg.org, cfg.team_slug, login, token)
        log.info(f"  {'+ added  ' if ok else '! FAILED '} @{login:<24} score={scores[login]:.3f}")

    for login in sorted(current - desired):
        ok = gh_api.remove_member(cfg.org, cfg.team_slug, login, token)
        log.info(f"  {'- removed' if ok else '! FAILED '} @{login:<24} score={scores.get(login, -1):.3f}")

    if not (desired - current) and not (current - desired):
        log.info("Team membership is already up to date.")

    log.info(f"\nTeam '{cfg.team_slug}': {len(desired)} member(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
