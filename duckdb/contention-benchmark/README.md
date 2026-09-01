# contention-benchmark

Determines whether latency under concurrent load is **CPU-bound** or **disk-bound**.

Runs one of two workloads of deliberately similar shape:

| `MODE` | Workload | Resources consumed |
|---|---|---|
| `cpu` | Aggregation sized to stay in memory | CPU only |
| `disk` | The same aggregation, forced to spill | CPU and disk |

Run each mode across a concurrency sweep and compare. The two resources fail
differently, which is what makes the comparison conclusive:

- **CPU quota is per pod.** A pod's allowance does not shrink when neighbours
  appear — but the kernel's CPU *shares* derive from the pod's **request**, not
  its limit. Under dense packing, pods receive their share rather than their
  limit, without ever hitting the quota that would register as throttling.
- **Disk bandwidth is a fixed per-host pool** (gp3 baseline: 125 MB/s,
  3,000 IOPS) divided among every pod on the node.

| Outcome | Conclusion |
|---|---|
| `cpu` degrades, `disk` flat | CPU is the constraint |
| `disk` degrades, `cpu` flat | Shared disk bandwidth |
| Both degrade equally | CPU — disk is not differentiating |
| Neither degrades | Contention is not the explanation |

## Running

```bash
tower deploy -f
tower run -p MODE=cpu
tower run -p MODE=disk
```

For a concurrency sweep, use [`../contention-driver`](../contention-driver),
which launches N of these at once.

## Parameters

| Name | Default | Meaning |
|---|---|---|
| `MODE` | `cpu` | `cpu` (in-memory) or `disk` (forced spill) |
| `ROWS` | `4000000` | Rows per work unit |
| `UNITS` | `40` | Work units per cycle |
| `CYCLES` | `5` | Cycles to run; cycle 1 is discarded as warm-up |
| `SPILL_MEMORY_LIMIT` | `400MB` | DuckDB `memory_limit` in disk mode |
| `TAG` | `` | Label echoed into output; useful when many runs are launched together |

## Output

One line per cycle, plus a machine-readable summary:

```
   cyc       utc   wall_s    cpu_s   cores    thr%    wr_MB  io_wait%
     1  08:31:52     28.4     54.1    1.91    2.1%      0.0      0.0%
     2  08:32:20     27.6     52.8    1.91    1.8%      0.0      0.0%

RESULT_JSON {"mode": "cpu", "mean_wall_s": 27.74, "mean_cpu_s": 53.08, ...}
```

The `RESULT_JSON` line lets a sweep of many runs be collated by grepping logs.

### Reading it

The decisive comparison is **`cpu_s` against `wall_s`** for a fixed work unit:

- `cpu_s` flat, `wall_s` growing → the pod is waiting, not computing. Check
  `cores` (how much CPU it actually received) and `io_wait`.
- `cpu_s` growing → the work itself got more expensive.

`thr%` is CFS throttling — the share of scheduling periods in which the pod
exhausted its own quota. Note that a pod can be starved of CPU **without**
throttling: if it receives less than its limit through fair-share scheduling,
it never reaches the quota that would throttle it. Watch `cores`, not just
`thr%`.

## Notes

- `tower run --local` reports nothing useful — there is no cgroup limit
  outside a container. It says so and exits non-zero.
- Cycle 1 builds the dataset and warms caches; it is excluded from the summary.
- `io_wait` comes from cgroup v2 PSI (`io.pressure`). The header states whether
  PSI is available rather than silently reporting zeros.
