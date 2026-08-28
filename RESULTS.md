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
