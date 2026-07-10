"""
stats.py
Statistical helpers for the TLOPO Loot Tracker's observation data --
currently just the Wilson score interval used to put a confidence bound
on small-sample observed rates (see enrichment.py compute_session_statistics).

Pure `math`-only, no new dependency, no I/O -- same "reusable outside the
GUI/detector" contract as loot_parser.py.
"""

import math
from typing import Optional, Tuple

# 95% two-sided Wilson interval by default -- the collaborator's own
# worked examples use "95% Wilson CI".
DEFAULT_Z = 1.959963985  # z-score for 95% confidence


def wilson_interval(successes: int, total: int, z: float = DEFAULT_Z) -> Optional[Tuple[float, float]]:
    """
    Closed-form Wilson score interval for a binomial proportion
    successes/total. Preferred over a naive normal-approximation interval
    for small samples (exactly the regime this tracker starts in every
    session) since it doesn't produce nonsensical bounds outside [0, 1]
    or collapse to a zero-width interval at 0%/100% observed rates.

    Returns None when total == 0 -- there's nothing to estimate a rate
    from yet, and (0.0, 0.0) or (0.0, 1.0) would both misrepresent "no
    data" as if it were a real (if wide) estimate.

    successes is clamped to [0, total] -- a rate can never exceed 100%
    conceptually, but the raw counts feeding this (e.g. containers vs.
    kills) can genuinely disagree in a real session (a manual kill-count
    correction, or containers logged for kills that predate auto-
    tracking being turned on), which would otherwise feed the sqrt below
    a negative value and crash.
    """
    if total <= 0:
        return None
    successes = max(0, min(successes, total))
    phat = successes / total
    denom = 1.0 + (z * z) / total
    center = phat + (z * z) / (2 * total)
    margin = z * math.sqrt((phat * (1 - phat) / total) + (z * z) / (4 * total * total))
    low = max(0.0, (center - margin) / denom)
    high = min(1.0, (center + margin) / denom)
    return (low, high)


def rate_with_ci(successes: int, total: int, z: float = DEFAULT_Z) -> dict:
    """
    {"rate": float, "ci_low": float, "ci_high": float} (0-1 scale, not
    percent -- callers format for display), or all three None if
    total == 0. The one helper every "X rate + 95% CI" line in the
    exporters calls.
    """
    if total <= 0:
        return {"rate": None, "ci_low": None, "ci_high": None}
    interval = wilson_interval(successes, total, z)
    return {"rate": successes / total, "ci_low": interval[0], "ci_high": interval[1]}
