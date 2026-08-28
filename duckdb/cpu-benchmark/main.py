"""cpu-benchmark — did this Tower run get the CPU its pod was promised?

Runs a fixed, deterministic amount of work and separates two questions that
look identical from the outside when a run is slow:

  1. How much CPU time did the work actually need?   (a property of the code)
  2. How long did the wall clock take to deliver it?  (a property of the platform)

If CPU time is normal but wall-clock is inflated, the difference is time the
pod spent frozen by the kernel's CPU limiter — not time the workload spent
computing. The report states which of the two it was.

Everything is measured from inside the pod via /sys/fs/cgroup, so the run is
self-contained: no cluster access is needed to interpret the result.
"""

from __future__ import annotations

import os
import platform
import sys
import threading
import time

import cgroup

# A work unit is a deterministic aggregation. The absolute cost doesn't matter;
# what matters is that it's identical between runs, so CPU-seconds per unit is
# a stable number that can be compared across fast runs and slow runs.
UNIT_QUERY = (
    "SELECT grp, count(*), avg(v), stddev(v), min(v), max(v) "
    "FROM t GROUP BY grp ORDER BY 2 DESC"
)


def _param(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


class Sampler:
    """Records CPU and throttle deltas while the work runs, so the report can
    show whether stalling was constant or concentrated in bursts."""

    def __init__(self, interval_ms: float) -> None:
        self.interval = interval_ms / 1000.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples: list[cgroup.Delta] = []

    def _loop(self) -> None:
        prev = cgroup.sample()
        while not self._stop.wait(self.interval):
            cur = cgroup.sample()
            d = cgroup.delta(prev, cur)
            if d.periods:
                self.samples.append(d)
            prev = cur

    def __enter__(self) -> "Sampler":
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def worst(self) -> float:
        return max((s.throttle_ratio for s in self.samples), default=0.0)


def run_work(units: int, rows: int, sampler_ms: float):
    import duckdb

    con = duckdb.connect()
    threads = con.execute("SELECT current_setting('threads')").fetchone()[0]

    con.execute(
        f"CREATE TABLE t AS SELECT i AS id, i % 1000 AS grp, random() AS v "
        f"FROM range({rows}) tbl(i)"
    )

    before = cgroup.sample()
    start = time.monotonic()

    with Sampler(sampler_ms) as sampler:
        for _ in range(units):
            con.execute(UNIT_QUERY).fetchall()

    wall = time.monotonic() - start
    after = cgroup.sample()
    con.close()

    return wall, cgroup.delta(before, after), sampler, threads


def main() -> int:
    units = int(_param("WORK_UNITS", "600"))
    rows = int(_param("ROWS", "4000000"))
    sample_ms = float(_param("SAMPLE_MS", "2000"))

    quota = cgroup.quota_cores()
    visible = cgroup.visible_cpus()

    print("=" * 74)
    print("TOWER CPU BENCHMARK")
    print("=" * 74)
    print(f"  python           : {platform.python_version()}")
    print(f"  cpus visible     : {visible}")
    print(f"  cpu limit (quota): {quota if quota else 'unlimited'} cores")
    print(f"  work             : {units} units x {rows:,} rows")
    print()

    if not cgroup.sample().valid:
        print("  No CPU quota is enforced in this environment (running outside a")
        print("  container?). Throttling cannot occur, so this benchmark has")
        print("  nothing to measure. Run it on Tower.")
        return 1

    print("  running...")
    wall, d, sampler, threads = run_work(units, rows, sample_ms)
    print()

    cpu_secs = d.cpu_used * d.elapsed
    per_unit = cpu_secs / units
    # Wall time this work would have taken with no throttling, given the quota.
    ideal_wall = cpu_secs / quota if quota else wall
    lost = max(wall - ideal_wall, 0.0)

    print("-" * 74)
    print("RESULT")
    print("-" * 74)
    print(f"  duckdb threads       : {threads}")
    print(f"  wall clock           : {wall:8.1f} s")
    print(f"  cpu time consumed    : {cpu_secs:8.1f} s   ({per_unit:.2f} s per unit)")
    print(f"  average cpu          : {d.cpu_used:8.2f} cores  (limit {quota:.1f})")
    print(f"  quota utilisation    : {d.cpu_used / quota * 100:7.1f} %")
    print()
    print(f"  cfs periods          : {d.periods}")
    print(f"  periods throttled    : {d.throttle_ratio * 100:7.1f} %   (worst sample {sampler.worst * 100:.1f} %)")
    print(f"  wall time frozen     : {d.throttled_frac * 100:7.1f} %")
    print()
    print(f"  wall time if never throttled : {ideal_wall:6.1f} s")
    print(f"  wall time lost to throttling : {lost:6.1f} s")
    print()

    print("=" * 74)
    print("VERDICT")
    print("=" * 74)

    if d.periods < 100:
        print(f"  (Only {d.periods} scheduling periods observed — too short to be")
        print("   conclusive. Raise WORK_UNITS so the run lasts at least ~30s.)")
        print()

    starved = d.throttle_ratio > 0.25
    saturated = quota and d.cpu_used >= quota * 0.9

    if starved and saturated:
        print("  CPU-LIMITED. The workload asked for more CPU than the pod allows.")
        print(f"  It ran at {d.cpu_used:.2f} of {quota:.1f} permitted cores and was frozen")
        print(f"  by the kernel in {d.throttle_ratio * 100:.0f}% of scheduling periods, adding roughly")
        print(f"  {lost:.0f}s to a {ideal_wall:.0f}s job.")
        print()
        print("  This is a capacity limit, not a defect in the workload: the same")
        print("  code with a higher CPU limit would finish sooner. Note the host")
        print(f"  has {visible} CPUs visible while this pod may use {quota:.0f}.")
    elif starved:
        print("  THROTTLED BELOW QUOTA. The pod was frozen in")
        print(f"  {d.throttle_ratio * 100:.0f}% of periods while averaging only {d.cpu_used:.2f} of")
        print(f"  {quota:.1f} cores. Demand arrived in bursts that exhausted the")
        print("  per-period budget. Worth investigating on the platform side.")
    elif saturated:
        print("  AT CAPACITY, NOT THROTTLED. The workload used essentially all of")
        print(f"  its {quota:.1f}-core allowance ({d.cpu_used:.2f}) without being frozen. It is")
        print("  CPU-hungry but the platform delivered the full quota. More speed")
        print("  requires a higher CPU limit or less work per run.")
    else:
        print("  NOT CPU-LIMITED. The workload used")
        print(f"  {d.cpu_used:.2f} of {quota:.1f} permitted cores and was throttled in only")
        print(f"  {d.throttle_ratio * 100:.1f}% of periods. The platform supplied the CPU that was")
        print("  asked for. Slowness in this run is not CPU starvation — look at")
        print("  I/O waits, network round trips, locks, or single-threaded sections.")

    print()
    print("  Compare 'cpu time consumed' across a fast run and a slow run:")
    print("  if it is similar but wall clock differs, the difference is time")
    print("  spent waiting, not computing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
