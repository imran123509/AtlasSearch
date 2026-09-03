# Kubernetes

Production deployment. The workloads here have genuinely different shapes, and the main design
work is not writing YAML — it is matching workload type to controller and scaling signal.

Related: [DOCKER](DOCKER.md) · [MONITORING](MONITORING.md) · [FAILURE-HANDLING](FAILURE-HANDLING.md)

---

## Workload types

| Component | Controller | Scaling signal | Why |
| --- | --- | --- | --- |
| `search-api` | Deployment | QPS / latency | Stateless, horizontally scalable |
| `crawler` | **StatefulSet** | Manual / crawl budget | Needs **stable identity** for host affinity |
| `renderer` | Deployment | Queue depth | Memory-bound, separate pool |
| `indexer` | Deployment | **Kafka consumer lag** | Batch, lag-driven |
| `opensearch-data` | StatefulSet | Manual | Stateful, ordered, persistent volumes |
| `pagerank` | CronJob | — | Weekly batch |
| `index-build` | Job | — | Triggered |

### The crawler is a StatefulSet, and that is the interesting one

Not for storage — for **stable network identity**. Host affinity
([DISTRIBUTED-CRAWLER](DISTRIBUTED-CRAWLER.md)) depends on a worker keeping the same identity
across restarts, so its Kafka partition assignment is stable. A Deployment gives random pod
names and every restart triggers a rebalance, which means a politeness overlap window every
time a pod cycles.

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata: { name: crawler }
spec:
  serviceName: crawler
  replicas: 8
  podManagementPolicy: Parallel        # crawlers are independent; don't start serially
  template:
    spec:
      terminationGracePeriodSeconds: 90   # must exceed the drain-on-revoke window
      containers:
        - name: crawler
          image: atlas/crawler:v0.4.1
          env:
            - name: WORKER_ID
              valueFrom: { fieldRef: { fieldPath: metadata.name } }
          lifecycle:
            preStop:
              exec: { command: ["/bin/sh","-c","kill -TERM 1; sleep 60"] }
```

`terminationGracePeriodSeconds: 90` matters: the pod must finish in-flight fetches and release
its Kafka partitions cleanly. Killing it at the default 30 s leaves leases outstanding and
creates a dual-ownership window.

---

## search-api Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: search-api }
spec:
  replicas: 6
  strategy:
    rollingUpdate: { maxSurge: 2, maxUnavailable: 0 }   # never lose capacity mid-roll
  template:
    spec:
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: topology.kubernetes.io/zone
          whenUnsatisfiable: DoNotSchedule
          labelSelector: { matchLabels: { app: search-api } }
      containers:
        - name: api
          image: atlas/search-api:v0.4.1
          resources:
            requests: { cpu: "1",   memory: "2Gi" }
            limits:   {             memory: "2Gi" }   # NOTE: no CPU limit
          startupProbe:
            httpGet: { path: /v1/readyz, port: 8000 }
            failureThreshold: 30
            periodSeconds: 5
          readinessProbe:
            httpGet: { path: /v1/readyz, port: 8000 }
            periodSeconds: 5
          livenessProbe:
            httpGet: { path: /v1/healthz, port: 8000 }
            periodSeconds: 20
            failureThreshold: 3
```

### Three probes, three different jobs

| Probe | Question | Failure action |
| --- | --- | --- |
| `startupProbe` | Has it finished warming? | Hold off the other probes |
| `readinessProbe` | Can it serve **now**? | Remove from Service endpoints |
| `livenessProbe` | Is it wedged? | **Restart the pod** |

**`/healthz` must not check dependencies.** If it checks OpenSearch, an OpenSearch blip
restarts every API pod simultaneously — turning a degraded dependency into a total outage.
Liveness answers "is this process wedged", nothing more. Dependency health belongs in
readiness.

### No CPU limit

CPU limits cause throttling that shows up as unexplained p99 latency — the container is
throttled at the quota boundary even when the node is idle. Set **requests** for scheduling;
leave limits off for latency-sensitive services. Memory limits stay (OOM is better than a
node-wide memory crisis).

---

## Autoscaling on the right signal

```yaml
# search-api: scale on QPS, not CPU
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: { name: search-api }
spec:
  scaleTargetRef: { apiVersion: apps/v1, kind: Deployment, name: search-api }
  minReplicas: 4
  maxReplicas: 40
  metrics:
    - type: Pods
      pods:
        metric: { name: http_requests_per_second }
        target: { type: AverageValue, averageValue: "120" }
  behavior:
    scaleUp:   { stabilizationWindowSeconds: 30 }
    scaleDown: { stabilizationWindowSeconds: 300 }   # scale down slowly
```

```yaml
# indexer: scale on Kafka consumer lag (KEDA)
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata: { name: indexer }
spec:
  scaleTargetRef: { name: indexer }
  minReplicaCount: 2
  maxReplicaCount: 20
  triggers:
    - type: kafka
      metadata:
        topic: pages.parsed
        consumerGroup: indexer
        lagThreshold: "5000"
```

CPU-based autoscaling is wrong for both. The API is latency-bound, not CPU-bound; the indexer
is throughput-bound and its backlog is the thing that matters
([KAFKA](KAFKA.md) backpressure).

Asymmetric stabilisation windows — fast up, slow down — prevent flapping on bursty traffic.

---

## PodDisruptionBudgets

Without these, a node drain can take down a whole tier during routine maintenance.

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: { name: search-api }
spec:
  minAvailable: 75%
  selector: { matchLabels: { app: search-api } }
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: { name: opensearch-data }
spec:
  maxUnavailable: 1          # never take two data nodes at once
  selector: { matchLabels: { app: opensearch-data } }
```

---

## Security baseline

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 10001                  # must match the Dockerfile UID
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  capabilities: { drop: ["ALL"] }
  seccompProfile: { type: RuntimeDefault }
```

The **renderer needs special attention**: it executes untrusted web content. Run it in a
separate namespace, with a NetworkPolicy denying access to internal services, and consider a
sandboxed runtime (gVisor, Kata). Treat every rendered page as hostile
([HTML-PARSER](HTML-PARSER.md)).

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: renderer-egress, namespace: atlas-render }
spec:
  podSelector: { matchLabels: { app: renderer } }
  policyTypes: [Egress]
  egress:
    - to: [ { ipBlock: { cidr: "0.0.0.0/0", except: ["10.0.0.0/8","172.16.0.0/12","192.168.0.0/16","169.254.0.0/16"] } } ]
```

That `except` list is the important part — it blocks the renderer from reaching internal
services and the cloud metadata endpoint (`169.254.169.254`), which is the standard SSRF
target.

---

## Anti-patterns

| Don't | Why |
| --- | --- |
| Liveness probe checks dependencies | Dependency blip → mass restart → outage |
| CPU limits on latency-sensitive services | Throttling shows up as mystery p99 |
| Crawler as a Deployment | Unstable identity → rebalance on every restart |
| No PDB | Node drain takes out a tier |
| Autoscale the indexer on CPU | Backlog grows while CPU looks fine |
| Renderer in the main namespace | Untrusted code with cluster network access |
| `maxUnavailable > 0` on rolling API updates | Capacity dips mid-deploy, right when load is normal |
| Grace period shorter than drain time | Leases leak, dual ownership, politeness violated |
