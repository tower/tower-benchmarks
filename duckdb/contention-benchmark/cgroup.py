"""Read this process's own CPU cgroup: quota, usage, and throttle counters.

Everything here comes from /sys/fs/cgroup inside the container, so the app
measures its own throttling with no cluster access required.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

CG2 = "/sys/fs/cgroup"


def _read(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def quota_cores() -> float | None:
    """Effective CPU limit in cores, or None if unlimited/unreadable."""
    raw = _read(f"{CG2}/cpu.max")  # cgroup v2: "<quota|max> <period>"
    if raw:
        quota, period = raw.split()
        if quota == "max":
            return None
        return int(quota) / int(period)

    quota = _read(f"{CG2}/cpu/cpu.cfs_quota_us")  # cgroup v1 fallback
    period = _read(f"{CG2}/cpu/cpu.cfs_period_us")
    if quota and period and int(quota) > 0:
        return int(quota) / int(period)
    return None


def period_ms() -> float:
    raw = _read(f"{CG2}/cpu.max")
    if raw:
        return int(raw.split()[1]) / 1000
    raw = _read(f"{CG2}/cpu/cpu.cfs_period_us")
    return int(raw) / 1000 if raw else 100.0


@dataclass(frozen=True)
class CpuStat:
    """A point-in-time sample of the cgroup's CPU accounting."""

    wall: float
    usage_secs: float
    nr_periods: int
    nr_throttled: int
    throttled_secs: float

    @property
    def valid(self) -> bool:
        return self.nr_periods > 0


def sample() -> CpuStat:
    now = time.monotonic()
    raw = _read(f"{CG2}/cpu.stat")
    vals: dict[str, int] = {}
    if raw:
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 2:
                vals[parts[0]] = int(parts[1])

    if "usage_usec" in vals:  # cgroup v2
        return CpuStat(
            wall=now,
            usage_secs=vals.get("usage_usec", 0) / 1e6,
            nr_periods=vals.get("nr_periods", 0),
            nr_throttled=vals.get("nr_throttled", 0),
            throttled_secs=vals.get("throttled_usec", 0) / 1e6,
        )

    # cgroup v1: counters live in cpu.stat, usage in cpuacct.usage (nanoseconds)
    usage_ns = _read(f"{CG2}/cpuacct/cpuacct.usage") or "0"
    return CpuStat(
        wall=now,
        usage_secs=int(usage_ns) / 1e9,
        nr_periods=vals.get("nr_periods", 0),
        nr_throttled=vals.get("nr_throttled", 0),
        throttled_secs=vals.get("throttled_time", 0) / 1e9,
    )


@dataclass(frozen=True)
class Delta:
    """Derived rates between two samples — what we actually report."""

    elapsed: float
    cpu_used: float          # cores consumed on average
    throttle_ratio: float    # fraction of periods that hit the quota
    throttled_frac: float    # fraction of wall time frozen
    periods: int

    def line(self, label: str) -> str:
        return (
            f"{label:<22} cpu={self.cpu_used:5.2f} cores  "
            f"throttled={self.throttle_ratio * 100:6.2f}% of periods  "
            f"stalled={self.throttled_frac * 100:6.2f}% of wall  "
            f"({self.periods} periods)"
        )


def delta(before: CpuStat, after: CpuStat) -> Delta:
    elapsed = max(after.wall - before.wall, 1e-9)
    periods = after.nr_periods - before.nr_periods
    throttled = after.nr_throttled - before.nr_throttled
    return Delta(
        elapsed=elapsed,
        cpu_used=(after.usage_secs - before.usage_secs) / elapsed,
        throttle_ratio=(throttled / periods) if periods else 0.0,
        throttled_frac=(after.throttled_secs - before.throttled_secs) / elapsed,
        periods=periods,
    )


def visible_cpus() -> int:
    """What a naive library sees — deliberately not cgroup-aware."""
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1
