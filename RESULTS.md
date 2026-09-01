# Benchmark results

Reference measurements from Tower execution pods. Reproduce any of these by
running the corresponding benchmark yourself — each reads its own cgroup
accounting from inside the pod, so results are self-contained.

---

## 2026-08-28 — us-east-1

**Pod under test** (a standard app run):

```
cpu limit    : 2 cores
memory limit : 16 GiB
node         : r5a.2xlarge (8 vCPU, 64 GiB, EBS-backed ephemeral storage)
image        : tower-runtime:python-3.12
```

### Runtime configuration

DuckDB 1.5.5 on this image is cgroup-aware — it sizes itself from the pod's
limits rather than the host's:

| Setting | Value | Note |
|---|---|---|
| `threads` | 2 | matches the 2-core limit |
| `memory_limit` | 12.7 GiB | ~79% of the 16 GiB limit |
| `os.cpu_count()` | 8 | reports the *host's* cores, not the limit |

No manual thread tuning is required for DuckDB. Libraries that are not
cgroup-aware will size pools from the 8 they see rather than the 2 available.

### cpu-benchmark

600 work units × 4M rows:

| Metric | Value |
|---|---|
| wall clock | 51.3 s |
| cpu time consumed | 100.3 s (0.17 s/unit) |
| average cpu | 1.96 cores |
| quota utilisation | 97.8% |
| cfs periods | 513 |
| periods throttled | 0.4% |
| wall time lost to throttling | 1.1 s |

**Verdict: AT CAPACITY, NOT THROTTLED.** A workload whose demand fits within the
pod's CPU limit receives the full quota.

For reference, a deliberately oversubscribed workload (8 CPU-bound processes in
a 2-core pod) produced 97.5% throttling at exactly 2.00 cores — throttling
occurs when demand exceeds the limit, not below it.

### io-benchmark

The same query run twice — once fitting in memory, once forced to spill:

| Metric | in-memory | spilling (400MB limit) |
|---|---|---|
| wall clock | 2.9 s | **15.0 s** |
| cpu time | 5.6 s | 10.2 s |
| average cpu | 1.98 cores | **0.69 cores** |
| cpu throttled | 10.3% | 8.4% |
| bytes written | 0 MB | **1,863 MB** |
| write IOPS | 0 | 510 |
| stalled on io | 0.0% | **23.2%** |

**Verdict: DISK-BOUND.** Spilling made the same query 5.2× slower while
consuming only 1.8× the CPU. Average CPU *dropped* from 1.98 to 0.69 cores — the
pod spent its time waiting rather than computing.

Sustained write throughput was **124.6 MB/s**, at the gp3 EBS baseline of
125 MB/s.

### Scope note

CPU quota is enforced **per pod** and is unaffected by other pods on the same
node. Disk bandwidth on EBS-backed nodes is a **per-node** resource shared by
all pods on the host. Workloads that spill to disk are therefore the ones whose
throughput depends on what else is running alongside them.

To measure this directly, run `io-benchmark` alone and then several instances
concurrently, and compare.

---

## 2026-09-01 — concurrency scaling, eu-west-1

First results from `duckdb/contention-benchmark`, measuring how per-run latency
scales with the number of simultaneous runs, and whether the constraint is CPU
or disk.

**Environment.** `r6a.2xlarge` nodes (8 vCPU, 7.91 allocatable), pods limited to
2 CPU / 16 GiB with requests of 500m / 1Gi, node root volume 500 GiB gp3 at the
125 MB/s baseline. At concurrency 50 the scheduler placed **15 pods per node**
across 4 nodes — pod density follows the 500m request, so per-node CPU limits
summed to 30 cores against 7.91 available.

Each run: 40 work units × 4M rows, 4 cycles, first cycle discarded as warm-up.

| Mode | Concurrency | n | wall_s | cpu_s | cores | throttled | written | io_wait |
|---|---|---|---|---|---|---|---|---|
| cpu | 10 | 10 | 27.7 | 53.1 | 1.91 | 2.78% | 0 MB | 0.00% |
| cpu | 50 | 50 | **91.1** | 55.8 | **0.63** | **0.00%** | 0 MB | 0.00% |
| disk | 10 | 10 | 37.7 | 70.7 | 1.88 | 7.18% | 530 MB | 0.79% |
| disk | 50 | 50 | **111.5** | 68.7 | **0.62** | 0.02% | 684 MB | 2.06% |

### Latency scales with concurrency, and the cause is CPU share

From 10 to 50 concurrent runs, wall-clock time grew **3.28×** in cpu mode and
**2.96×** in disk mode. Both modes degraded by essentially the same factor, so
disk is not what differentiates them: aggregate writes reached ~77 MB/s per node
against the 125 MB/s baseline, and I/O stall stayed at 2%.

CPU time per run stayed flat (53.1 → 55.8 s, a factor of 1.05). The work cost
the same; each run simply received less CPU. **Delivered CPU fell from 1.91 to
0.63 cores** — close to the 8 ÷ 15 ≈ 0.53 that fair-share scheduling implies at
that pod density.

### Starvation without throttling

The most useful detail for anyone instrumenting this: **throttling went to zero
as latency tripled.**

CFS throttling registers only when a pod exhausts *its own* quota. Linux
allocates CPU by weight derived from a pod's **request**, so at 15 pods × 500m
on 8 cores each pod receives ~0.5 cores — far below its 2-core limit. It is
starved, but never reaches the ceiling that would throttle it.

A monitoring setup that watches `container_cpu_cfs_throttled_periods_total`
alone will therefore report a healthy cluster while runs take three times
longer. The signal that does track the problem is **delivered cores**
(`container_cpu_usage_seconds_total` against the pod's limit).

### Implications

- Pod density on these nodes is set by the CPU **request** (500m), while
  behaviour under load is governed by how that request compares to the limit
  (2 CPU). At a 4:1 ratio, a fully packed node delivers roughly a quarter of
  each pod's nominal CPU.
- Raising requests toward limits reduces density and raises the per-pod floor
  proportionally, at a corresponding increase in node count.
- Disk was not the binding constraint at this concurrency, but the per-node
  125 MB/s baseline is shared and would become one at higher spill volumes.
  gp3 throughput is provisionable independently of volume size.
