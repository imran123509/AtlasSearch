from __future__ import annotations

import pytest

from atlas_crawler.dedup import (
    InMemoryExactStore,
    RedisBloom,
    RedisExactStore,
    SeenUrlFilter,
    SiteBudget,
    TrapDetector,
    bloom_params,
    fingerprint,
    path_prefixes,
)


class AlwaysMaybeBloom:
    """A Bloom filter whose false-positive rate is 100%.

    Used to prove the hybrid stays correct under the worst possible Bloom: every
    lookup falls through to the exact store and nothing is lost.
    """

    async def test_and_add(self, fp: int) -> bool:
        return True


class TestFingerprint:
    def test_deterministic(self):
        assert fingerprint("https://example.com/a") == fingerprint("https://example.com/a")

    def test_fits_in_64_bits(self):
        assert 0 <= fingerprint("https://example.com/a") < (1 << 64)

    def test_distinct_urls_differ(self):
        fps = {fingerprint(f"https://example.com/{i}") for i in range(1000)}
        assert len(fps) == 1000


class TestBloomParams:
    def test_ten_bits_per_item_gives_about_one_percent(self):
        bits, k = bloom_params(1_000_000, 0.01)
        assert 9 <= bits / 1_000_000 <= 11
        assert k == 7

    def test_tighter_error_rate_costs_more_bits(self):
        assert bloom_params(1000, 0.001)[0] > bloom_params(1000, 0.01)[0]

    def test_zero_capacity_rejected(self):
        with pytest.raises(ValueError):
            bloom_params(0)


class TestRedisBloom:
    async def test_no_false_negatives(self, redis):
        """THE property that makes the hybrid safe.

        "Definitely new" must really be definite. If a Bloom filter could return
        "not present" for something it holds, the exact store would never be
        consulted and the URL would be crawled twice — or worse, the design's
        correctness argument collapses.
        """
        bloom = RedisBloom(redis, capacity=10_000, error_rate=0.01)
        fps = [fingerprint(f"https://example.com/{i}") for i in range(500)]
        for fp in fps:
            await bloom.test_and_add(fp)
        for fp in fps:
            assert await bloom.contains(fp) is True

    async def test_first_insert_reports_new(self, redis):
        bloom = RedisBloom(redis, capacity=10_000)
        assert await bloom.test_and_add(fingerprint("https://example.com/x")) is False

    async def test_second_insert_reports_maybe_seen(self, redis):
        bloom = RedisBloom(redis, capacity=10_000)
        fp = fingerprint("https://example.com/x")
        await bloom.test_and_add(fp)
        assert await bloom.test_and_add(fp) is True

    async def test_false_positive_rate_is_roughly_as_configured(self, redis):
        bloom = RedisBloom(redis, capacity=2_000, error_rate=0.01, shards=1)
        for i in range(2_000):
            await bloom.test_and_add(fingerprint(f"https://in.test/{i}"))
        hits = 0
        for i in range(2_000):
            if await bloom.contains(fingerprint(f"https://out.test/{i}")):
                hits += 1
        assert hits / 2_000 < 0.05  # generous bound; the point is "small"


class TestExactStore:
    async def test_roundtrip(self, redis):
        store = RedisExactStore(redis, shards=4)
        fps = [fingerprint(f"https://example.com/{i}") for i in range(50)]
        await store.add_many(fps)
        assert await store.contains_many(fps) == set(fps)

    async def test_absent_fingerprints_not_reported(self, redis):
        store = RedisExactStore(redis, shards=4)
        await store.add_many([fingerprint("https://example.com/a")])
        absent = fingerprint("https://example.com/b")
        assert absent not in await store.contains_many([absent])

    async def test_empty_input(self, redis):
        store = RedisExactStore(redis)
        assert await store.contains_many([]) == set()
        await store.add_many([])

    async def test_sharded_by_fingerprint_not_host(self, redis):
        """Host distribution is Zipfian; sharding on it would put a tenth of the
        web in one shard."""
        store = RedisExactStore(redis, shards=16)
        keys = {store._key(fingerprint(f"https://example.com/{i}")) for i in range(500)}
        assert len(keys) > 8, "one host's URLs concentrated into too few shards"


class TestSeenUrlFilter:
    @pytest.fixture
    def seen(self, redis):
        return SeenUrlFilter(RedisBloom(redis, capacity=100_000), InMemoryExactStore())

    async def test_new_urls_returned(self, seen):
        urls = ["https://example.com/a", "https://example.com/b"]
        assert set(await seen.filter_new(urls)) == set(urls)

    async def test_repeat_urls_filtered(self, seen):
        await seen.filter_new(["https://example.com/a"])
        assert await seen.filter_new(["https://example.com/a"]) == []

    async def test_canonicalisation_collapses_variants(self, seen):
        """The two halves of this feature meet here: tracking-parameter variants
        of one page must not each consume a crawl."""
        await seen.filter_new(["https://example.com/p?utm_source=nl&id=4"])
        assert await seen.filter_new(["HTTPS://Example.COM/p?id=4#frag"]) == []

    async def test_duplicates_within_one_batch_collapsed(self, seen):
        got = await seen.filter_new(["https://example.com/a"] * 5)
        assert len(got) == 1

    async def test_malformed_urls_skipped_not_raised(self, seen):
        got = await seen.filter_new(["http://[bad", "https://example.com/ok"])
        assert got == ["https://example.com/ok"]

    async def test_bloom_absorbs_most_lookups(self, seen):
        """The doc expects ~85% absorption: on a fresh corpus almost everything
        is 'definitely new' and never reaches the exact store."""
        await seen.filter_new([f"https://example.com/{i}" for i in range(1000)])
        assert seen.stats.bloom_absorption > 0.9
        assert seen.stats.exact_consulted < 100

    async def test_a_pathological_bloom_loses_nothing(self, redis):
        """Option A is the trap; Option C survives it.

        With a Bloom that returns 'maybe seen' for everything, correctness is
        unchanged — every lookup just costs an exact-store consultation.
        """
        seen = SeenUrlFilter(AlwaysMaybeBloom(), InMemoryExactStore())
        urls = [f"https://example.com/{i}" for i in range(200)]
        assert len(await seen.filter_new(urls)) == 200, "a false positive lost a URL"
        assert await seen.filter_new(urls) == []

    async def test_exact_store_has_the_final_say(self, redis):
        exact = InMemoryExactStore()
        seen = SeenUrlFilter(AlwaysMaybeBloom(), exact)
        await seen.filter_new(["https://example.com/a"])
        assert len(exact) == 1

    async def test_is_new_single_url(self, seen):
        assert await seen.is_new("https://example.com/z") is True
        assert await seen.is_new("https://example.com/z") is False


class TestSiteBudget:
    async def test_default_authority_gives_a_small_budget(self, redis):
        budget = SiteBudget(redis)
        assert 0 < await budget.limit_for("unknown.test") < 1000

    async def test_authority_raises_the_budget(self, redis):
        budget = SiteBudget(redis)
        await budget.set_authority("big.test", 0.5)
        assert await budget.limit_for("big.test") > await budget.limit_for("small.test")

    async def test_exhausted_is_read_only(self, redis):
        """It must not consume budget, or peeking would be a side effect and the
        ordering fix in Frontier.add_many would not work."""
        budget = SiteBudget(redis)
        for _ in range(5):
            await budget.exhausted("example.com")
        assert await budget.spent("example.com") == 0

    async def test_spend_charges(self, redis):
        budget = SiteBudget(redis)
        await budget.spend("example.com", 10)
        assert await budget.spent("example.com") == 10

    async def test_becomes_exhausted(self, redis):
        budget = SiteBudget(redis, base=10)
        limit = await budget.limit_for("example.com")
        await budget.spend("example.com", limit)
        assert await budget.exhausted("example.com") is True


class TestPathPrefixes:
    def test_prefixes_are_cumulative(self):
        got = path_prefixes("https://example.com/a/b/c")
        assert got == ["example.com/a", "example.com/a/b", "example.com/a/b/c"]

    def test_numeric_segments_collapsed_to_a_pattern(self):
        """Demoting the pattern is the point; demoting individual URLs is
        whack-a-mole against a generator."""
        a = path_prefixes("https://t.test/calendar/2026/03")
        b = path_prefixes("https://t.test/calendar/2027/11")
        assert a == b

    def test_hex_ids_collapsed(self):
        a = path_prefixes("https://t.test/item/8f2a91c4de99")
        b = path_prefixes("https://t.test/item/aa11bb22cc33")
        assert a == b

    def test_depth_capped(self):
        assert len(path_prefixes("https://t.test/" + "/".join("abcdefgh"))) <= 4

    def test_root_path_has_no_prefixes(self):
        assert path_prefixes("https://example.com/") == []


class TestTrapDetector:
    async def test_generator_detected(self, redis):
        """Many URLs, almost no distinct content = a generator, not a section."""
        d = TrapDetector(redis, min_urls=50, max_distinct_ratio=0.05)
        verdict = None
        for i in range(120):
            verdict = await d.observe(f"https://t.test/calendar/{i}", "sha256:same")
        assert verdict.is_trap is True
        assert verdict.prefix == "t.test/calendar"

    async def test_diverse_content_is_not_a_trap(self, redis):
        d = TrapDetector(redis, min_urls=50, max_distinct_ratio=0.05)
        verdict = None
        for i in range(120):
            verdict = await d.observe(f"https://news.test/story/{i}", f"sha256:{i}")
        assert verdict.is_trap is False

    async def test_below_the_url_threshold_nothing_fires(self, redis):
        d = TrapDetector(redis, min_urls=1000, max_distinct_ratio=0.05)
        verdict = None
        for i in range(30):
            verdict = await d.observe(f"https://t.test/cal/{i}", "sha256:same")
        assert verdict.is_trap is False

    async def test_demotion_covers_sibling_urls(self, redis):
        d = TrapDetector(redis, min_urls=50, max_distinct_ratio=0.05)
        for i in range(120):
            await d.observe(f"https://t.test/calendar/{i}", "sha256:same")
        assert await d.is_demoted("https://t.test/calendar/99999") is True

    async def test_unrelated_paths_unaffected(self, redis):
        d = TrapDetector(redis, min_urls=50, max_distinct_ratio=0.05)
        for i in range(120):
            await d.observe(f"https://t.test/calendar/{i}", "sha256:same")
        assert await d.is_demoted("https://t.test/articles/real-story") is False

    async def test_manual_demote_and_undemote(self, redis):
        d = TrapDetector(redis)
        await d.demote("t.test/spam")
        assert await d.is_demoted("https://t.test/spam/x") is True
        await d.undemote("t.test/spam")
        assert await d.is_demoted("https://t.test/spam/x") is False

    async def test_root_url_is_never_demoted(self, redis):
        d = TrapDetector(redis)
        assert await d.is_demoted("https://t.test/") is False
