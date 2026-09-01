# tower-benchmarks

Self-contained benchmarks that measure what a Tower run actually received from
the platform — CPU and disk — and report a plain verdict.

Each benchmark reads its **own** cgroup accounting from inside the pod
(`/sys/fs/cgroup`), so a single run is interpretable on its own. No cluster
access, no dashboards, no correlating timestamps afterwards.

## Why these exist

When a run is slow, two explanations look identical from the outside:

1. the workload needed more computing than usual, or
2. the platform made it wait

These benchmarks separate the two by running a **fixed** amount of work and
reporting CPU time and wall-clock time independently. If CPU time is unchanged
but wall clock grew, the difference is waiting — not computing.

## Benchmarks

| Directory | Question it answers |
|---|---|
| [`duckdb/cpu-benchmark`](duckdb/cpu-benchmark) | Did this pod get the CPU it was promised? |
| [`duckdb/io-benchmark`](duckdb/io-benchmark) | Does disk keep up when DuckDB spills? |
| [`duckdb/contention-benchmark`](duckdb/contention-benchmark) | Under concurrency, is the constraint CPU or disk? |
| [`duckdb/contention-driver`](duckdb/contention-driver) | Launches N of the above at once, for concurrency sweeps |

Reference measurements are in [RESULTS.md](RESULTS.md).

Both use DuckDB as the workload because it is cgroup-aware: it sizes its thread
pool and memory limit from the pod's actual limits rather than the host's, so it
measures the *platform*, not a misconfigured library.

## Running

```bash
pip install "tower[all]"
tower login

cd duckdb/cpu-benchmark
tower deploy -f
tower run
```

Same for `duckdb/io-benchmark`.

### Testing concurrency

Both benchmarks report independently per run, so concurrency is tested by
launching several at once:

```bash
for i in $(seq 1 12); do tower run -d; done
```

Then compare the reports. The distinction that matters:

- **CPU quota is per-pod.** It does not shrink when neighbours appear.
- **Disk bandwidth is per-node.** It is shared by every pod on the host.

So a workload that degrades with concurrency while per-pod CPU stays clean
points at storage, not CPU.

## Interpreting a report

Each run ends with a verdict. The useful comparison is across two runs — one
fast, one slow:

| Observation | Conclusion |
|---|---|
| Similar CPU time, longer wall clock | Time spent waiting. Platform-side. |
| More CPU time | The workload did more work. Application-side. |
| High `stalled on io` | Storage-bound. |
| High `cpu throttled` at full quota | CPU-limited by the pod's own limit. |

## Requirements

- A Tower account and the `tower` CLI
- Linux with cgroup v2 (Tower execution pods qualify)
- `tower run --local` will report that it has nothing to measure — there is no
  CPU or memory limit enforced outside a container
