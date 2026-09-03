# PageRank

Query-independent authority from the link graph. A per-document prior available at every
ranking stage, and a factor in [canonical election](CONTENT-DE-DUPLICATION.md) and
[crawl prioritisation](URL-FRONTIER.md).

Related: [RANKING](RANKING.md) · [BM25](BM25.md) · [DISTRIBUTED-INDEXING](DISTRIBUTED-INDEXING.md)

---

## The formula

```
              1 − d              PR(v)
PR(u) = ──────── + d · Σ  ────────────
                N        v→u    L(v)
```

| Symbol | Meaning |
| --- | --- |
| `d` | Damping factor, **0.85** — probability the random surfer follows a link |
| `N` | Number of documents |
| `L(v)` | Out-degree of `v` |
| `v→u` | All pages linking to `u` |

The random-surfer interpretation: a surfer clicks links with probability `d`, and with
probability `1−d` teleports to a random page. PageRank is the stationary distribution — the
long-run fraction of time spent on each page.

### Dangling nodes

Pages with no outbound links (PDFs, images, leaves) leak rank out of the system. Redistribute
their mass uniformly each iteration, or the values do not sum to 1 and comparisons across
iterations become meaningless:

```
dangling_mass = Σ PR(v) for v with L(v) = 0
PR(u) += d · dangling_mass / N
```

---

## Scale

| Quantity | Value |
| --- | --- |
| Nodes | 10¹⁰ documents |
| Edges | ~5 × 10¹¹ (≈50 outlinks/page) |
| Edge storage | ~4 TB (8 bytes/edge, compressed adjacency) |
| Iterations to converge | 30–60 |
| Cadence | Weekly |
| Cost | A few hundred machines, a few hours |

This does not fit on one machine and cannot be done incrementally in any exact way — it is a
global fixed-point computation over the whole graph.

---

## Distributed computation

Vertex-centric (Pregel / Giraph / GraphX):

```
partition vertices by hash(host) — keeps intra-site links local,
                                    and most links are intra-site

for iteration in 1..K:
    each vertex sends PR(v)/L(v) along each outgoing edge
    each vertex sums incoming messages
    PR(u) = (1−d)/N + d·(sum + dangling_mass/N)
    if Σ|ΔPR| < ε: stop
```

**Partition by host, not by document.** Roughly 80% of links are intra-site, so host
partitioning keeps most messages node-local and cuts shuffle volume by ~5×. This is the
single biggest performance decision in the implementation.

### Practical notes

- Work in **log space** for storage. Raw values at 10¹⁰ nodes are ~10⁻¹⁰ and lose float
  precision; `log(1 + PR·N)` is well-conditioned and is the form ranking consumes anyway.
- **Warm-start** from the previous week's values. Convergence drops from ~50 iterations to
  ~10, because the graph changes slowly.
- Compute at **host level first**, then distribute within-host. Host-level PageRank over
  ~10⁸ hosts is 100× cheaper, converges faster, and is more stable — many production systems
  weight host authority more heavily than page authority for exactly this reason.

---

## Spam resistance

PageRank is gameable, and the mitigations are as important as the algorithm.

| Attack | Defence |
| --- | --- |
| Link farms (dense mutual linking) | Detect near-cliques; the eigenvector of a clique is a distinctive spectral signature |
| Purchased links | Domain-diversity requirement — 100 links from 3 hosts ≪ 100 links from 100 hosts |
| Comment/forum spam | Honour `rel="nofollow"`, `ugc`, `sponsored` — no authority flows |
| Expired-domain hijacking | Decay authority on ownership change and on content discontinuity |
| Link pyramids | **TrustRank**: seed from a manually vetted trusted set and propagate; sites far from every seed are suspect |
| Sitewide footer links | Cap contribution from any single source domain |

**TrustRank** is the most valuable addition: run the same propagation from a hand-curated
seed set of trustworthy sites. A page with high PageRank but low TrustRank is very likely
spam, and the *ratio* is a stronger signal than either value alone.

---

## Why this decayed as a signal, and what to do about it

Honest assessment: PageRank is not what it was in 1998.

- **The modern web links less.** Content moved into platforms, apps, and walled gardens that
  do not emit outbound links.
- **The links that exist are heavily gamed.** An entire industry exists to manufacture them.
- **It entrenches incumbents.** Authority accrues to sites that are already old and large,
  which is exactly the bias a search engine should be careful about.

Weight it high and you entrench incumbents and reward link brokers. Weight it low and content
farms flood in. **There is no correct setting** — it is a continuously retuned policy, not an
architecture decision.

### And retuning it is expensive

[INVERTED-INDEX](INVERTED-INDEX.md) explains that static rank is baked into posting order to
make block-max pruning work. So changing the authority formula requires re-sorting every
posting list — **a full rebuild**.

The structure that makes retrieval cheap makes the most-frequently-retuned signal the most
expensive to change. The demotion overlay ([FAILURE-HANDLING](FAILURE-HANDLING.md)) is a
workaround for this, not a solution.

**Practical consequence:** keep two authority values.

```
baked_authority     — in posting order, changes only on full rebuild
overlay_adjustment  — small per-host multiplier, shipped in minutes, applied at L1
```

The overlay cannot change *which* documents are retrieved (posting order is fixed), only how
they are scored once retrieved. That is a real limitation and it is why spam that gets into
tier 0 is much harder to remove than spam that never got in.

---

## Alternatives worth knowing

| Algorithm | Difference | Use |
| --- | --- | --- |
| **TrustRank** | Seeded propagation from vetted sites | Spam detection — use it |
| **HITS** | Query-dependent hubs and authorities | Too slow at query time; conceptually useful |
| **SALSA** | Random walk on the bipartite hub-authority graph | More topic-drift-resistant than HITS |
| **Host-level rank** | Aggregate to registrable domain | Cheaper, more stable, harder to game — often better |

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Engine | Pregel-style, host-partitioned | NetworkX / SciPy sparse for <10⁷ nodes; Spark GraphX above |
| Edges | 5 × 10¹¹ | 10⁸ |
| Cadence | Weekly, warm-started | Per index build |
| Storage | Compressed adjacency, 4 TB | Parquet edge list |
| Consumption | Baked into posting order + overlay | `rank_feature` field in OpenSearch |

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| Dangling nodes not handled | Rank mass leaks; values incomparable across iterations | Redistribute dangling mass every iteration |
| Not converged | Unstable ranks between builds; result churn | Fixed iteration floor **and** an `ε` threshold; alert on rank churn |
| Link farm undetected | Spam dominates authority | TrustRank ratio + near-clique detection |
| Float underflow | Ranks collapse to zero for most of the graph | Log space |
| Partition by document, not host | 5× shuffle volume; job takes all night | Partition by `hash(host)` |
| Authority baked in and now wrong | Cannot fix without a full rebuild | Maintain the per-host overlay adjustment |
| `nofollow` ignored | Comment spam becomes profitable | Honour `nofollow`, `ugc`, `sponsored` |
