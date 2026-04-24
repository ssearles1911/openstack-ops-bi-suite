"""Formatting + row-annotation helpers shared across reports."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional


def humanize(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def annotate_ages(rows: Iterable[Dict[str, Any]]) -> None:
    """Mutates each row in-place, adding `age_seconds` and `age` from
    `effective_time`. Also rewrites `last_action=None` as a readable marker.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for r in rows:
        eff = r.get("effective_time")
        if eff is None:
            r["age_seconds"] = None
            r["age"] = "-"
        else:
            r["age_seconds"] = (now - eff).total_seconds()
            r["age"] = humanize(r["age_seconds"])
        if r.get("last_action") is None:
            r["last_action"] = "(none recorded)"
