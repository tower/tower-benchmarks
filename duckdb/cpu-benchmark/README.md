# cpu-benchmark

Answers one question about a Tower run: **did this pod get the CPU it was promised?**

It runs a fixed, deterministic amount of work and separates two things that look
identical from the outside when a run is slow:

| | what it tells you |
|---|---|
| **CPU time consumed** | how much computing the work actually needed — a property of *your code* |
| **Wall clock** | how long it took to deliver that — a property of the *platform* |

If CPU time is normal but wall clock is inflated, the difference is time the pod
spent **frozen by the kernel's CPU limiter**, not time spent computing.

Everything is measured from inside the pod via `/sys/fs/cgroup`. Nothing outside
the run is needed to interpret the result.

## Running it

```bash
tower deploy -f
tower run
```

Defaults produce a ~50s run, long enough for a trustworthy sample. To vary it:

```bash
tower run -p WORK_UNITS=1200        # longer run
tower run -p ROWS=8000000           # heavier unit
```

## Reading the output

```
  wall clock           :     51.3 s
  cpu time consumed    :    100.3 s   (0.17 s per unit)
  average cpu          :     1.96 cores  (limit 2.0)
  periods throttled    :      0.4 %
  wall time lost to throttling :    1.1 s
```

Then one of four verdicts:

| Verdict | Meaning |
|---|---|
| **NOT CPU-LIMITED** | Platform supplied the CPU asked for. Slowness is elsewhere — I/O, network, locks, single-threaded sections. |
| **AT CAPACITY, NOT THROTTLED** | Used essentially the whole allowance without being frozen. CPU-hungry, but the platform delivered. Needs a higher limit or less work. |
| **CPU-LIMITED** | Asked for more CPU than allowed and was frozen by the kernel. A capacity limit, not a code defect. |
| **THROTTLED BELOW QUOTA** | Frozen despite low average CPU. Unusual — worth escalating. |

## The comparison that settles an argument

Run it during a **fast** period and a **slow** period, then compare
`cpu time consumed`:

- **Similar CPU time, different wall clock** → the extra time was spent *waiting*,
  not computing. The work didn't change; the platform's delivery did.
- **Different CPU time** → the workload itself did more work. Not a platform issue.

That single number is the discriminator. Everything else in the report explains
*why*.

## Concurrency

To test whether latency degrades with concurrent runs:

```bash
for i in $(seq 1 12); do tower run -d; done
```

Each run reports independently. If `cpu time consumed` stays flat while wall
clock grows with concurrency, contention is the cause.

## Caveats

- `tower run --local` reports nothing useful — there's no CPU quota outside a
  container. It says so and exits non-zero.
- A run shorter than ~100 scheduling periods (~10s) is flagged as inconclusive.
- The workload is DuckDB-based, which is cgroup-aware and sizes its own thread
  pool correctly. It measures what the *platform* delivers, not whether a
  particular library is configured well.
