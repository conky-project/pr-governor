"""Load trust-config.yml and required environment variables."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


import yaml

_DURATION_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(y(?:ears?)?|mo(?:nths?)?|w(?:eeks?)?|d(?:ays?)?)$",
    re.IGNORECASE,
)

# Bundled defaults - shipped alongside the action scripts.
_TEMPLATE = Path(__file__).parent.parent / "trust-config.yml"


def parse_duration(value: int | float | str) -> float:
    """Convert a duration value to days.

    Accepts a plain number (already days) or a string like '2y', '20mo',
    '4w', '30d'. Years are 365 days; months are 30 days; weeks are 7 days.
    """
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    m = _DURATION_RE.match(s)
    if not m:
        try:
            return float(s)
        except ValueError:
            raise ValueError(f"Cannot parse duration: {value!r}")
    n, unit = float(m.group(1)), m.group(2).lower()
    if unit.startswith("y"):
        return n * 365.0
    if unit.startswith("mo"):
        return n * 30.0
    if unit.startswith("w"):
        return n * 7.0
    return n  # days


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


@dataclass(frozen=True)
class FeatureConfig:
    importance: float
    # Literal number (in the feature's natural unit), a percentile string like
    # "p99", or a duration string like "2y" / "20mo" (for time-based features).
    # Values above the resolved cap are clamped to 1.0 during normalization.
    full_credit_at: float | str
    # Recency-specific exponential decay half-life (days). Ignored for all
    # other features. Accepts a plain number or a duration string.
    # Also controls the decay rate for review_activity.
    half_life_days: float = 730.0
    # Recency-specific: coefficient for the burst bonus applied when multiple
    # PRs land in the same window. Formula: max(v) * (1 + burst_bonus * log1p(n-1)).
    burst_bonus: float = 0.1
    # Recency-specific: contributors with no activity (PR or review) in the
    # last inactivity_cutoff_days have their final score overridden to -1.
    # Accepts a plain number or a duration string.
    inactivity_cutoff_days: float = 730.0
    # Engagement-specific: PRs per 30-day window that counts as "fully active".
    # c_i = min(n_prs_in_window / full_credit_window_prs, 1.0). Ignored for
    # all other features.
    full_credit_window_prs: float = 5.0


@dataclass(frozen=True)
class Config:
    window_size_days: int
    max_prs: int
    features: dict[str, FeatureConfig]
    threshold: float
    org: str
    team_slug: str
    ignore_users: frozenset[str]

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = path or os.environ.get("TRUST_CONFIG", ".github/trust-config.yml")

        with open(_TEMPLATE) as fh:
            raw = yaml.safe_load(fh)
        try:
            with open(path) as fh:
                raw = _deep_merge(raw, yaml.safe_load(fh) or {})
        except FileNotFoundError:
            pass  # repo has no overrides; defaults apply

        features: dict[str, FeatureConfig] = {}
        for name, fc in raw["features"].items():
            fca = fc["full_credit_at"]
            features[name] = FeatureConfig(
                importance=float(fc["importance"]),
                full_credit_at=float(fca) if isinstance(fca, (int, float)) else str(fca),
                half_life_days=parse_duration(fc.get("half_life_days", 730.0)),
                burst_bonus=float(fc.get("burst_bonus", 0.1)),
                inactivity_cutoff_days=parse_duration(fc.get("inactivity_cutoff", 730.0)),
                full_credit_window_prs=float(fc.get("full_credit_window_prs", 5.0)),
            )

        github_val = str(raw.get("github", "")).strip()
        if "/" in github_val:
            org, team_slug = github_val.split("/", 1)
        else:
            org, team_slug = github_val, "trusted-contributors"

        if not org:
            print(
                f"::error title=Configuration error::github must be set in {path}"
                " (format: \"org\" or \"org/team-slug\")",
                flush=True,
            )
            sys.exit(1)

        return cls(
            window_size_days=int(raw["scoring"]["window_size_days"]),
            max_prs=int(raw["scoring"].get("max_prs", 500)),
            features=features,
            threshold=float(raw["thresholds"]["trusted_approver"]),
            org=org,
            team_slug=team_slug,
            ignore_users=frozenset(raw.get("ignore_users") or []),
        )


def require_env(*names: str) -> dict[str, str]:
    """Read named env vars; print a workflow error annotation and exit if any are missing."""
    vals = {k: os.environ.get(k, "") for k in names}
    missing = [k for k, v in vals.items() if not v]
    if missing:
        print(
            f"::error title=Configuration error::Missing required environment"
            f" variables: {', '.join(missing)}",
            flush=True,
        )
        sys.exit(1)
    return vals
