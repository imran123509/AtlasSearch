# URL De-duplication

Two jobs: **canonicalise** URLs so that equivalent addresses collapse to one string, and
answer **"have we seen this URL?"** against 5 × 10¹¹ known URLs, fast enough to keep up with
link extraction.

Related: [URL-FRONTIER](URL-FRONTIER.md) · [CONTENT-DE-DUPLICATION](CONTENT-DE-DUPLICATION.md) · [REDIS](REDIS.md)

---

## Canonicalisation

Applied in a fixed order. Order matters — reordering these produces different outputs for
the same input, which defeats the whole purpose.

```
1.  lowercase scheme and host                 HTTP://Example.COM → http://example.com
2.  drop default port                         example.com:80 → example.com
3.  prefer https when the host redirects there
4.  percent-encoding: decode unreserved, uppercase remaining hex
                                              %7Euser → ~user,  %2f → %2F
5.  resolve dot segments                      /a/./b/../c → /a/c
6.  drop the fragment                         /page#section → /page
7.  strip known tracking params               utm_*, fbclid, gclid, ref, mc_cid, _ga …
8.  strip session ids                         PHPSESSID, jsessionid, sid, s= …
9.  sort remaining query params by key
10. drop trailing slash on non-directory paths
11. normalise Unicode host to punycode        münchen.de → xn--mnchen-3ya.de
12. apply rel=canonical if the page declared one and it is same-site
```

### Do not over-normalise

Two traps, both common:

- **Stripping unknown query params.** `?page=2` and `?id=447` are meaningful. Strip only from
  a maintained allow-list of known-junk parameters. A blocklist approach is safe; a
  "strip everything not in a small allow-list" approach silently destroys real content.
- **Trusting `rel=canonical` cross-site.** It is a hint from an untrusted party. Accept it
  same-site; treat cross-site declarations as a spam signal, not an instruction. Otherwise
  any site can declare itself canonical for your best pages.

---

## The seen-URL test at 5 × 10¹¹

Every extracted link needs a membership test. Three options, and the middle one is a trap.

### Option A — Bloom filter alone ❌

```
5 × 10¹¹ URLs × 10 bits = 625 GB, ~1% false-positive rate
```

A false positive means a URL is **permanently never crawled**, silently, with no way to
detect it from inside the system. For a corpus meant to be authoritative, that is an
unacceptable failure mode. This is the option most tutorials recommend and it is wrong.

### Option B — Exact sharded store ✅

```
64-bit fingerprints × 5 × 10¹¹ = 4 TB
sharded across 100 nodes = 40 GB each — comfortably in RAM
```

Correct and affordable. Fingerprint with a strong 64-bit hash of the canonical URL; collision
probability at 5 × 10¹¹ keys is ~2.7% for *at least one* collision across the whole corpus,
which is acceptable. Use 128-bit if you want that to vanish, at 8 TB.

### Option C — Hybrid ✅✅ (chosen)

```
in-memory Bloom  →  "definitely new"  →  skip the lookup, insert
                 →  "maybe seen"      →  consult the exact store
```

Absorbs ~85% of lookups with **no correctness loss** — the Bloom filter's false positives
now cost a lookup, not a lost document. Its false negatives do not exist.

---

## Batching is what makes it work

Point lookups against a 4 TB store are the bottleneck, not the storage. Amortise:

```
extracted links accumulate in a buffer (say 10⁶ URLs)
  → compute fingerprints
  → sort by fingerprint
  → single sequential sweep over the store shard
  → emit the new ones to the frontier
```

This turns random I/O into streaming I/O and is the difference between the frontier keeping
up and falling behind. The classic sort-merge URL-seen test. Buffer latency of a few minutes
is irrelevant — the frontier is scheduling hours ahead.

---

## Per-site URL budget

The single most effective defence against crawl traps, and it works without ever identifying
a trap as such:

```
site_budget = base × log(1 + site_authority)
```

- A site with no inbound links gets a few hundred URLs.
- A major news site gets millions.
- Infinite calendars, faceted-navigation explosions, and session-ID URL generators all hit
  the budget and stop, regardless of what generated them.

### Trap detection as a second layer

Track content-hash repetition per path prefix. A path pattern producing many URLs but few
distinct content hashes is a generator:

```
if urls_seen(prefix) > 1000 and distinct_content_hashes(prefix) / urls_seen(prefix) < 0.05:
    demote the whole pattern, not just the URL
```

Demoting the *pattern* is the important part. Demoting individual URLs is whack-a-mole
against a generator that produces them faster than you can demote.

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Membership | Bloom + sharded exact store, 4 TB | Redis Bloom / `SETBIT` + Postgres unique index |
| Capacity | 5 × 10¹¹ | 10⁸ |
| Lookup mode | Batched sort-merge sweep | Point lookups, fine at this rate |
| Fingerprint | 64-bit xxHash of canonical URL | Same |

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| Bloom false positive with no exact store | URL permanently never crawled, silently | Never use Bloom alone. Hybrid only. |
| Canonicalisation not deterministic | Same URL fingerprints differently; duplicate crawls | Pure function, no clocks, no map iteration order. Property-test that `canon(canon(u)) == canon(u)`. |
| Over-aggressive param stripping | Distinct pages collapse into one | Allow-list of junk params only. Review additions. |
| Cross-site `rel=canonical` trusted | Spammers hijack ranking of your best pages | Same-site only; cross-site is a spam signal. |
| Fingerprint collision | One page shadows another, permanently | 64-bit is acceptable; move to 128-bit if the corpus grows past 10¹². |
| Store shard lost | Those URLs look new; re-crawl storm | Shard is rebuildable from the frontier log. Rate-limit re-discovery after a rebuild. |
