"""contention-driver — launch N contention-benchmark runs at once and wait.

This mirrors how a real workload arrives: many separate, independent runs
started simultaneously, rather than threads inside one run. Launching from
inside a run (rather than a shell loop) dispatches all children through the
same path a real fan-out uses, so they start close together.

Run the same CONCURRENCY sweep twice, once per MODE, and compare:

  MODE=cpu    each child does in-memory aggregation  — CPU only
  MODE=disk   the same shape forced to spill         — CPU and disk

CPU quota is per pod and does not shrink when neighbours appear; disk
bandwidth is a fixed per-host pool shared by every pod on the node. So the two
modes degrade differently, and which one degrades identifies the bottleneck:

  cpu degrades, disk flat  -> CPU oversubscription
  disk degrades, cpu flat  -> shared disk bandwidth
  both degrade             -> node saturation; compare magnitudes
  neither degrades         -> contention is not the explanation

Each child prints its own RESULT_JSON line; collect them from the child run
logs to build the sweep table.
"""

from __future__ import annotations

import os
import time

import tower


def _param(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def main() -> None:
    mode = _param("MODE", "cpu").lower()
    concurrency = int(_param("CONCURRENCY", "10"))
    child_app = _param("CHILD_APP", "contention-benchmark")
    units = _param("UNITS", "40")
    cycles = _param("CYCLES", "5")
    rows = _param("ROWS", "4000000")

    tag = f"{mode}-c{concurrency}"

    print("=" * 70)
    print(f"CONTENTION DRIVER  mode={mode}  concurrency={concurrency}")
    print("=" * 70)
    print(f"  child app : {child_app}")
    print(f"  per child : {units} units x {rows} rows, {cycles} cycles")
    print(f"  tag       : {tag}")
    print()

    params = {
        "MODE": mode,
        "UNITS": units,
        "CYCLES": cycles,
        "ROWS": rows,
        "TAG": tag,
    }

    print(f"  launching {concurrency} runs...")
    t0 = time.monotonic()
    runs = []
    for i in range(concurrency):
        runs.append(tower.run_app(child_app, parameters=params))
    launched = time.monotonic() - t0
    print(f"  launched in {launched:.1f}s")
    print()

    print("  waiting for completion...")
    t1 = time.monotonic()
    successful, failed = tower.wait_for_runs(runs)
    elapsed = time.monotonic() - t1

    print()
    print("-" * 70)
    print(f"  mode         : {mode}")
    print(f"  concurrency  : {concurrency}")
    print(f"  succeeded    : {len(successful)}")
    print(f"  failed       : {len(failed)}")
    print(f"  wall (fanout): {elapsed:.1f}s")
    print("-" * 70)
    # Per-child wall time straight off the Run records, so a sweep does not
    # need log-scraping to get its headline numbers. Node hostname comes
    # along too: co-location is what decides whether disk contention can
    # happen at all, so it belongs in the same table.
    import collections
    import statistics

    def secs(r):
        try:
            return (r.ended_at - r.started_at).total_seconds()
        except Exception:
            return None

    durations = [d for d in (secs(r) for r in successful) if d is not None]
    hosts = collections.Counter(
        getattr(r, "hostname", None) or "unknown" for r in successful
    )

    print()
    print("  child run numbers:", ", ".join(str(getattr(r, "number", "?")) for r in successful))
    print()

    if durations:
        durations.sort()
        pct = lambda q: durations[min(int(q * len(durations)), len(durations) - 1)]
        print(f"  child wall  min {min(durations):6.1f}s   p50 {pct(0.5):6.1f}s   "
              f"p95 {pct(0.95):6.1f}s   max {max(durations):6.1f}s")
        if len(durations) > 1:
            print(f"              mean {statistics.mean(durations):6.1f}s   "
                  f"sd {statistics.pstdev(durations):5.2f}s   "
                  f"spread {max(durations) / min(durations):.2f}x")

    print()
    print(f"  distinct hosts: {len(hosts)}")
    for h, n in hosts.most_common():
        print(f"    {n:>3} child(ren)  {h}")
    if len(hosts) > 1:
        print("  (disk bandwidth is per host — children sharing a host share it)")

    print()
    print(f"  Per-child detail is in each child log (grep RESULT_JSON), tagged '{tag}'.")

    if failed:
        print()
        print(f"  WARNING: {len(failed)} child run(s) failed; the sweep row for")
        print("  this concurrency level is incomplete.")


if __name__ == "__main__":
    main()
