# contention-driver

Launches N [`contention-benchmark`](../contention-benchmark) runs simultaneously
and waits for them, so per-run latency can be measured as a function of
concurrency.

Fan-out happens from inside a Tower run via `tower.run_app()`, so the children
are dispatched through the same path a real concurrent workload uses and start
close together — which a shell loop does not reproduce.

## Running a sweep

Run the same concurrency levels in both modes:

```bash
tower deploy -f

for c in 1 10 30 50; do
  tower run -p MODE=cpu  -p CONCURRENCY=$c
  tower run -p MODE=disk -p CONCURRENCY=$c
done
```

Then compare how mean per-run latency scales in each mode. See
[`../contention-benchmark`](../contention-benchmark) for how to interpret the
divergence.

## Parameters

| Name | Default | Meaning |
|---|---|---|
| `MODE` | `cpu` | Passed through to every child |
| `CONCURRENCY` | `10` | Number of children launched at once |
| `CHILD_APP` | `contention-benchmark` | App to fan out to |
| `UNITS` | `40` | Work units per cycle, per child |
| `CYCLES` | `5` | Cycles per child |
| `ROWS` | `4000000` | Rows per work unit |

## Output

```
  mode         : cpu
  concurrency  : 50
  succeeded    : 50
  failed       : 0
  wall (fanout): 466.0s

  child wall  min  288.1s   p50  401.1s   p95  465.3s   max  467.7s
              mean  396.3s   sd 57.13s   spread 1.62x
```

Per-child detail lives in each child's log — grep `RESULT_JSON`. Children are
tagged `<mode>-c<concurrency>` so a sweep's logs stay self-describing.

## Capacity

Each runner instance accepts a bounded number of concurrent executions
(`TOWER_MAX_CONCURRENT_APPS`, default 10). Requesting more concurrency than
`replicas × TOWER_MAX_CONCURRENT_APPS` queues the excess, which starts late and
runs against a partly-drained cluster — contaminating the measurement.

Check capacity before a large sweep:

```bash
kubectl get statefulset tower-runner-podular -n tower-system \
  -o jsonpath='{.spec.replicas}{"\n"}'
```

Node provisioning also takes time: a burst that needs new nodes will include
node startup in the first cycle. Cycle 1 is discarded as warm-up for this
reason, but allow for it when reading fan-out wall time.
