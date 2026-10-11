"""Usage statistics computed from the session log."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

CHARGED = ("completed", "ended_early", "kicked")
DAY = 86400


def _dedupe(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A reclaimed turn is logged twice under one id; keep the latest entry."""
    by_id: Dict[str, Dict[str, Any]] = {}
    for r in records:
        by_id[r.get("id", id(r))] = r
    return list(by_id.values())


def _wait_s(r: Dict[str, Any]) -> Optional[float]:
    joined, offered = r.get("joined_at"), r.get("offered_at")
    if joined is None or offered is None or offered < joined:
        return None
    return offered - joined


def _mean(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _tz(name: Optional[str]):
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


def user_stats(records: List[Dict[str, Any]], user_id: str) -> Dict[str, Any]:
    mine = [r for r in _dedupe(records) if r.get("user_id") == user_id]
    waits = [w for w in (_wait_s(r) for r in mine) if w is not None]
    avg = _mean(waits)
    return {
        "sessions": sum(1 for r in mine if r.get("outcome") in CHARGED),
        "no_shows": sum(1 for r in mine if r.get("outcome") == "no_show"),
        "avg_wait_min": int(round(avg / 60)) if avg is not None else 0,
    }


def global_stats(records: List[Dict[str, Any]], now: float, tz_name: Optional[str] = None,
                 days: int = 30, heatmap_days: int = 90) -> Dict[str, Any]:
    tz = _tz(tz_name)
    records = _dedupe(records)
    window = [r for r in records if (r.get("ended_at") or 0) >= now - days * DAY]
    charged = [r for r in window if r.get("outcome") in CHARGED and r.get("charge_start")]
    offers = [r for r in window if r.get("offered_at")]

    waits = [w for w in (_wait_s(r) for r in offers) if w is not None]
    durations = [r["ended_at"] - r["charge_start"] for r in charged if r["ended_at"] > r["charge_start"]]
    no_shows = sum(1 for r in offers if r.get("outcome") == "no_show")

    today = datetime.fromtimestamp(now, tz).date()
    per_day = {(today - timedelta(days=i)).isoformat(): 0 for i in range(days - 1, -1, -1)}
    for r in charged:
        d = datetime.fromtimestamp(r["charge_start"], tz).date().isoformat()
        if d in per_day:
            per_day[d] += 1

    # Minutes of charging per weekday (Mon=0) and hour.
    heat = [[0.0] * 24 for _ in range(7)]
    for r in records:
        start, end = r.get("charge_start"), r.get("ended_at")
        if r.get("outcome") not in CHARGED or not start or not end or end < now - heatmap_days * DAY:
            continue
        t = start
        while t < end:
            local = datetime.fromtimestamp(t, tz)
            next_hour = (local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).timestamp()
            seg_end = min(end, next_hour)
            heat[local.weekday()][local.hour] += (seg_end - t) / 60
            t = seg_end

    avg_wait = _mean(waits)
    avg_charge = _mean(durations)
    return {
        "days": days,
        "sessions": len(charged),
        "hours_charged": round(sum(durations) / 3600, 1),
        "avg_wait_min": int(round(avg_wait / 60)) if avg_wait is not None else None,
        "avg_charge_min": int(round(avg_charge / 60)) if avg_charge is not None else None,
        "no_show_rate": round(no_shows / len(offers), 3) if offers else None,
        "per_day": [{"date": d, "count": c} for d, c in per_day.items()],
        "heatmap": [[int(round(m)) for m in row] for row in heat],
        "heatmap_days": heatmap_days,
    }
