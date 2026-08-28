# io-benchmark

Answers: **does this pod's disk keep up when DuckDB spills?**

A query whose working set exceeds DuckDB's memory limit writes intermediate data
to disk. That disk is network-attached and **shared by every pod on the node** —
unlike the CPU quota, which is per-pod and unaffected by neighbours.

That difference matters for diagnosing slowness that appears only under
concurrency:

| Resource | Scope | Degrades with co-tenants? |
|---|---|---|
| CPU quota | per pod | No |
| Disk bandwidth | per node | **Yes** |

## Running

```bash
tower deploy -f
tower run
```

Defaults force a real spill (~1.9 GB). To vary:

```bash
tower run -p ROWS=120000000              # more spill
tower run -p SPILL_MEMORY_LIMIT=200MB    # spill sooner
```

## What it does

Runs the same query twice — once with enough memory to stay in RAM, once
constrained so it must spill — and compares them. Both phases report CPU time,
wall clock, bytes moved, IOPS, and `io.pressure` stall time from the cgroup.

## Example output

From a Tower run on `us-east-1` (2-core, 16 GiB pod):

```
  in-memory (no spill expected)
    wall clock         : 2.9 s
    cpu time           : 5.6 s
    average cpu        : 1.98 cores
    disk read/written  : 0.0 MB / 0.0 MB
    stalled on io      : 0.0 % of wall

  spilling (memory_limit=400MB)
    wall clock         : 15.0 s
    cpu time           : 10.2 s
    average cpu        : 0.69 cores
    disk read/written  : 0.0 MB / 1,863.4 MB
    disk iops (r/w)    : 0 / 510
    stalled on io      : 23.2 % of wall
```

Spilling made the same query **5.2× slower** while using only 1.8× the CPU.
Average CPU *dropped* from 1.98 to 0.69 cores — the pod stopped computing and
started waiting. Sustained write throughput was **124.6 MB/s**.

## Reading the verdict

| Verdict | Meaning |
|---|---|
| **DISK-BOUND** | >20% of wall clock stalled on I/O. Storage dominates, and this is what degrades under concurrency. |
| **LIKELY DISK-BOUND** | Wall clock grew faster than CPU time, but PSI didn't confirm it. |
| **NOT DISK-BOUND** | The slowdown tracked CPU. Disk kept up. |
| **NO SPILL OBSERVED** | Query fit in memory. Raise `ROWS` or lower `SPILL_MEMORY_LIMIT`. |

## The concurrency test

This is the one that isolates shared-resource contention:

```bash
tower run                                    # baseline, alone
for i in $(seq 1 12); do tower run -d; done  # under load
```

If the **spilling** phase degrades with concurrency while the **in-memory**
phase stays constant, the shared disk is the bottleneck — not the per-pod CPU
limit.

## Notes

- `stalled on io` comes from cgroup v2 PSI (`io.pressure`). The report states
  whether PSI is available rather than silently showing zeros.
- Spill goes to the pod's ephemeral volume by default. Override with `TEMP_DIR`.
- Both phases build their own dataset, so setup cost is excluded from the timed
  section.
