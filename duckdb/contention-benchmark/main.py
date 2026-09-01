"""contention-benchmark — is concurrent-load latency CPU-bound or disk-bound?

Runs one of two workloads of deliberately similar shape:

  MODE=cpu   an aggregation sized to stay in memory   — consumes CPU, no disk
  MODE=disk  the same aggregation forced to spill     — consumes CPU and disk

Launch each mode across a concurrency sweep (1, 10, 30, 60, 90 simultaneous
runs) and compare how per-run latency changes. The two resources fail
differently, which is what makes the comparison conclusive:

  CPU quota is enforced per pod. A pod's 2-core allowance does not shrink
  when neighbours appear. It degrades only if pods are packed densely enough
  that their *limits* oversubscribe the node's real cores.

  Disk bandwidth is a fixed per-host pool (gp3 baseline: 125 MB/s, 3000 IOPS)
  divided among every pod on that node, so it degrades as soon as co-located
  pods contend for it.

So:

  cpu mode degrades, disk mode flat   -> CPU oversubscription
  disk mode degrades, cpu mode flat   -> shared disk bandwidth
  both degrade                        -> node saturation; compare magnitudes
  neither degrades                    -> contention is not the explanation

Each run also reports its own cgroup accounting, so a single run is
interpretable on its own: if wall clock grows while cpu_s stays flat, the
extra time was spent waiting rather than computing.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import sys
import time

import cgroup
import iostat

QUERY = (
    "SELECT k, count(*) AS n, avg(v) AS a, max(pad) AS m "
    "FROM t GROUP BY k ORDER BY n DESC, k LIMIT 100"
)


def _param(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def build(con, rows: int) -> None:
    # High-cardinality key + a padding column: large intermediates, so the
    # same query either fits in memory or spills purely on the memory limit.
    con.execute(
        f"CREATE TABLE t AS "
        f"SELECT i AS id, (i * 2654435761) % 1000000 AS k, "
        f"       repeat('x', 64) AS pad, random() AS v "
        f"FROM range({rows}) tbl(i)"
    )


def main() -> int:
    mode = _param("MODE", "cpu").lower()
    rows = int(_param("ROWS", "4000000"))
    units = int(_param("UNITS", "40"))
    cycles = int(_param("CYCLES", "5"))
    spill_limit = _param("SPILL_MEMORY_LIMIT", "400MB")
    tag = _param("TAG", "")

    if mode not in ("cpu", "disk"):
        print(f"MODE must be 'cpu' or 'disk', got {mode!r}")
        return 2

    quota = cgroup.quota_cores()

    print("=" * 78)
    print(f"CONTENTION BENCHMARK — mode={mode}" + (f"  tag={tag}" if tag else ""))
    print("=" * 78)
    print(f"  host            : {socket.gethostname()}")
    print(f"  python          : {platform.python_version()}")
    print(f"  cpu limit       : {quota if quota else 'unlimited'} cores")
    print(f"  rows/unit       : {rows:,}   units/cycle: {units}   cycles: {cycles}")
    if mode == "disk":
        print(f"  memory_limit    : {spill_limit}  (forcing spill)")
    print(f"  PSI available   : {iostat.psi_available()}")
    print()

    if not cgroup.sample().valid:
        print("  No cgroup limits enforced here — run this on Tower.")
        return 1

    import duckdb

    con = duckdb.connect()
    if mode == "disk":
        temp = "/app/spill"
        os.makedirs(temp, exist_ok=True)
        con.execute(f"SET temp_directory='{temp}'")
        con.execute(f"SET memory_limit='{spill_limit}'")
    else:
        # Comfortably above the working set so nothing reaches disk.
        con.execute("SET memory_limit='8GB'")
    con.execute("SET preserve_insertion_order=false")

    build(con, rows)

    print(f"  {'cyc':>4} {'utc':>9} {'wall_s':>8} {'cpu_s':>8} {'cores':>7} "
          f"{'thr%':>7} {'wr_MB':>8} {'io_wait%':>9}")

    results = []
    for n in range(1, cycles + 1):
        c0, i0 = cgroup.sample(), iostat.sample()
        t0 = time.monotonic()
        for _ in range(units):
            con.execute(QUERY).fetchall()
        wall = time.monotonic() - t0
        c1, i1 = cgroup.sample(), iostat.sample()

        c, i = cgroup.delta(c0, c1), iostat.delta(i0, i1)
        cpu_s = c.cpu_used * c.elapsed
        stamp = time.strftime("%H:%M:%S", time.gmtime())
        print(
            f"  {n:>4} {stamp:>9} {wall:>8.1f} {cpu_s:>8.1f} {c.cpu_used:>7.2f} "
            f"{c.throttle_ratio * 100:>6.1f}% {i.write_mb:>8.1f} "
            f"{i.io_stall_frac * 100:>8.1f}%",
            flush=True,
        )
        results.append({
            "cycle": n, "wall_s": round(wall, 2), "cpu_s": round(cpu_s, 2),
            "cores": round(c.cpu_used, 3), "throttle": round(c.throttle_ratio, 4),
            "write_mb": round(i.write_mb, 1), "io_wait": round(i.io_stall_frac, 4),
        })

    con.close()

    # Drop cycle 1: first pass warms caches and is not comparable.
    steady = results[1:] or results
    mean = lambda k: sum(r[k] for r in steady) / len(steady)

    print()
    print("-" * 78)
    print("SUMMARY (cycle 1 excluded as warm-up)" if len(results) > 1 else "SUMMARY")
    print("-" * 78)
    print(f"  mean wall     : {mean('wall_s'):8.1f} s")
    print(f"  mean cpu      : {mean('cpu_s'):8.1f} s  ({mean('cores'):.2f} cores)")
    print(f"  mean throttle : {mean('throttle') * 100:8.1f} %")
    print(f"  mean written  : {mean('write_mb'):8.1f} MB")
    print(f"  mean io wait  : {mean('io_wait') * 100:8.1f} % of wall")
    print()

    # One machine-readable line so a sweep of many runs can be collated by
    # grepping the logs, without re-parsing the human table above.
    print("RESULT_JSON " + json.dumps({
        "mode": mode, "tag": tag, "rows": rows, "units": units,
        "cycles": cycles, "quota_cores": quota,
        "mean_wall_s": round(mean("wall_s"), 2),
        "mean_cpu_s": round(mean("cpu_s"), 2),
        "mean_cores": round(mean("cores"), 3),
        "mean_throttle": round(mean("throttle"), 4),
        "mean_write_mb": round(mean("write_mb"), 1),
        "mean_io_wait": round(mean("io_wait"), 4),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
