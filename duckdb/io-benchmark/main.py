"""io-benchmark — does this pod's disk keep up when DuckDB spills?

A query whose working set exceeds DuckDB's memory limit spills intermediate
data to disk. On these pods that disk is network-attached (EBS), and its
throughput is shared by every pod on the node — unlike the CPU quota, which is
per-pod. That makes spill a plausible source of slowdown that gets *worse* with
concurrency while CPU metrics stay clean.

This benchmark runs the same query twice:

  in-memory : DuckDB given enough memory to finish without spilling
  spilling  : DuckDB constrained so the same query must go to disk

Comparing the two separates "my query is heavy" from "this pod's disk is slow".
Run it alone, then run several concurrently: if the spilling case degrades with
concurrency and the in-memory case doesn't, the disk is the shared bottleneck.
"""

from __future__ import annotations

import os
import platform
import sys
import time

import cgroup
import iostat


def _param(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _fmt(v: float, unit: str = "") -> str:
    return f"{v:,.1f}{unit}"


def build_dataset(con, rows: int) -> None:
    con.execute(
        f"CREATE TABLE t AS "
        f"SELECT i AS id, (i * 2654435761) % 1000000 AS k, "
        f"       repeat('x', 64) AS pad, random() AS v "
        f"FROM range({rows}) tbl(i)"
    )


# A wide sort+aggregate over a high-cardinality key: the classic spiller.
SPILL_QUERY = (
    "SELECT k, count(*) AS n, avg(v) AS a, max(pad) AS m "
    "FROM t GROUP BY k ORDER BY n DESC, k LIMIT 100"
)


def run_phase(label: str, rows: int, mem_limit: str, temp_dir: str) -> dict:
    import duckdb

    con = duckdb.connect()
    con.execute(f"SET memory_limit='{mem_limit}'")
    con.execute(f"SET temp_directory='{temp_dir}'")
    con.execute("SET preserve_insertion_order=false")

    build_dataset(con, rows)

    cpu_before, io_before = cgroup.sample(), iostat.sample()
    start = time.monotonic()
    con.execute(SPILL_QUERY).fetchall()
    wall = time.monotonic() - start
    cpu_after, io_after = cgroup.sample(), iostat.sample()

    con.close()

    c = cgroup.delta(cpu_before, cpu_after)
    i = iostat.delta(io_before, io_after)
    cpu_secs = c.cpu_used * c.elapsed

    print(f"  {label}")
    print(f"    memory_limit       : {mem_limit}")
    print(f"    wall clock         : {_fmt(wall, ' s')}")
    print(f"    cpu time           : {_fmt(cpu_secs, ' s')}")
    print(f"    average cpu        : {c.cpu_used:.2f} cores")
    print(f"    cpu throttled      : {c.throttle_ratio * 100:.1f} % of periods")
    print(f"    disk read/written  : {_fmt(i.read_mb, ' MB')} / {_fmt(i.write_mb, ' MB')}")
    print(f"    disk iops (r/w)    : {i.read_iops:,.0f} / {i.write_iops:,.0f}")
    if iostat.psi_available():
        print(f"    stalled on io      : {i.io_stall_frac * 100:.1f} % of wall")
        print(f"    stalled on memory  : {i.mem_stall_frac * 100:.1f} % of wall")
    print(f"    peak memory        : {i.mem_peak_gib:.2f} GiB")
    print()

    return {
        "label": label,
        "wall": wall,
        "cpu_secs": cpu_secs,
        "cpu_used": c.cpu_used,
        "throttle": c.throttle_ratio,
        "spill_mb": i.total_mb,
        "io_stall": i.io_stall_frac,
        "iops": i.read_iops + i.write_iops,
    }


def main() -> int:
    rows = int(_param("ROWS", "60000000"))
    tight = _param("SPILL_MEMORY_LIMIT", "400MB")
    temp_dir = _param("TEMP_DIR", "/app/spill")

    quota = cgroup.quota_cores()
    mem_limit = iostat.memory_limit_bytes()

    print("=" * 74)
    print("TOWER DISK / SPILL BENCHMARK")
    print("=" * 74)
    print(f"  python            : {platform.python_version()}")
    print(f"  cpu limit         : {quota if quota else 'unlimited'} cores")
    print(f"  memory limit      : {mem_limit / 2**30:.1f} GiB" if mem_limit else "  memory limit      : unlimited")
    print(f"  rows              : {rows:,}")
    print(f"  spill temp dir    : {temp_dir}")
    print(f"  PSI available     : {iostat.psi_available()}")
    print()

    if not cgroup.sample().valid:
        print("  No cgroup limits enforced here — run this on Tower.")
        return 1

    os.makedirs(temp_dir, exist_ok=True)

    print("-" * 74)
    print("PHASES")
    print("-" * 74)
    generous = run_phase("in-memory (no spill expected)", rows, "8GB", temp_dir)
    spilling = run_phase(f"spilling (memory_limit={tight})", rows, tight, temp_dir)

    print("=" * 74)
    print("VERDICT")
    print("=" * 74)

    if spilling["spill_mb"] < 50:
        print("  NO SPILL OBSERVED. The constrained phase did not write enough to")
        print("  disk to test anything — DuckDB completed it in memory anyway.")
        print("  Raise ROWS or lower SPILL_MEMORY_LIMIT to force a spill.")
        return 0

    slowdown = spilling["wall"] / generous["wall"] if generous["wall"] else 0
    cpu_ratio = spilling["cpu_secs"] / generous["cpu_secs"] if generous["cpu_secs"] else 0
    throughput = spilling["spill_mb"] / spilling["wall"] if spilling["wall"] else 0

    print(f"  Spilling made the same query {slowdown:.1f}x slower in wall clock,")
    print(f"  while consuming {cpu_ratio:.1f}x the CPU time.")
    print(f"  It moved {_fmt(spilling['spill_mb'], ' MB')} at {_fmt(throughput, ' MB/s')}")
    print(f"  ({spilling['iops']:,.0f} IOPS).")
    print()

    if spilling["io_stall"] > 0.20:
        print(f"  DISK-BOUND. The pod was stalled waiting on I/O for")
        print(f"  {spilling['io_stall'] * 100:.0f}% of the spilling phase. Wall-clock time here is")
        print("  dominated by storage, not computation. Because this pod's disk is")
        print("  shared with other pods on the same node, this is the component")
        print("  most likely to degrade as concurrency rises.")
    elif slowdown > 1.5 and cpu_ratio < slowdown * 0.7:
        print("  LIKELY DISK-BOUND. Wall clock grew faster than CPU time did, so")
        print("  the extra time was spent waiting rather than computing — even")
        print("  though I/O pressure stalling was not directly observed.")
    else:
        print("  NOT DISK-BOUND. The slowdown tracked CPU time, so spilling cost")
        print("  computation rather than I/O waiting. This pod's disk kept up.")

    print()
    print("  Next: run several of these concurrently. If the spilling phase")
    print("  degrades with concurrency while the in-memory phase does not, the")
    print("  shared disk is the bottleneck — not the per-pod CPU limit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
