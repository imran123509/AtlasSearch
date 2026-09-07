from __future__ import annotations

import json
import random

import pytest

from atlas_indexer.index import (
    BM25,
    DeleteSet,
    DictionaryReader,
    DictionaryWriter,
    Index,
    InputDocument,
    Segment,
    SegmentCorrupt,
    TermInfo,
    merge_segments,
    search,
    search_exhaustive,
    should_merge,
)
from atlas_indexer.index.postings import NO_MORE
from atlas_indexer.index.segment import POSTINGS, TERMS

from test_index_search import assert_same_results, random_corpus


# ---------------------------------------------------------------------------
# Term dictionary
# ---------------------------------------------------------------------------

class TestDictionary:
    def _roundtrip(self, terms: list[str]) -> DictionaryReader:
        w = DictionaryWriter()
        for i, term in enumerate(terms):
            w.add(term, TermInfo(i + 1, (i + 1) * 2, i * 10, i * 5))
        return DictionaryReader(w.finish())

    def test_lookup(self):
        r = self._roundtrip(["alpha", "beta", "gamma"])
        info = r.get("beta")
        assert info is not None and info.doc_freq == 2 and info.postings_offset == 10

    def test_missing_term(self):
        assert self._roundtrip(["alpha", "gamma"]).get("beta") is None

    def test_before_first_and_after_last(self):
        r = self._roundtrip(["m", "n"])
        assert r.get("a") is None
        assert r.get("z") is None

    def test_front_coding_survives_shared_prefixes(self):
        """The compression trick: long shared prefixes stored once."""
        terms = ["retrieval", "retrieve", "retrieved", "retriever", "retrieving"]
        r = self._roundtrip(terms)
        for t in terms:
            assert r.get(t) is not None
        assert r.get("retriev") is None

    def test_many_terms_span_multiple_blocks(self):
        terms = sorted(f"term{i:05d}" for i in range(500))
        r = self._roundtrip(terms)
        assert len(r) == 500
        for t in random.sample(terms, 50):
            assert r.get(t) is not None

    def test_iteration_is_sorted_and_complete(self):
        terms = sorted(f"w{i:04d}" for i in range(200))
        r = self._roundtrip(terms)
        got = [t for t, _ in r.terms()]
        assert got == terms

    def test_unicode_terms(self):
        terms = sorted(["café", "日本語", "naïve", "über"])
        r = self._roundtrip(terms)
        for t in terms:
            assert r.get(t) is not None

    def test_positions_offset_preserved(self):
        """Without this a cursor reads the previous term's positions."""
        r = self._roundtrip(["a", "b", "c"])
        assert r.get("c").positions_offset == 10

    def test_empty_dictionary(self):
        assert DictionaryReader(DictionaryWriter().finish()).get("x") is None


# ---------------------------------------------------------------------------
# Delete set
# ---------------------------------------------------------------------------

class TestDeleteSet:
    def test_sparse_roundtrip(self):
        d = DeleteSet(10_000)
        d.delete_many([5, 77, 9000])
        got = DeleteSet.from_bytes(d.to_bytes())
        assert {5, 77, 9000} == {i for i in range(10_000) if got.is_deleted(i)}

    def test_dense_roundtrip(self):
        d = DeleteSet(1000)
        d.delete_many(range(0, 1000, 2))
        got = DeleteSet.from_bytes(d.to_bytes())
        assert all(got.is_deleted(i) for i in range(0, 1000, 2))
        assert not any(got.is_deleted(i) for i in range(1, 1000, 2))

    def test_sparse_encoding_is_smaller(self):
        sparse = DeleteSet(100_000)
        sparse.delete_many([1, 2, 3])
        dense = DeleteSet(100_000)
        dense.delete_many(range(0, 100_000, 2))
        assert len(sparse.to_bytes()) < len(dense.to_bytes()) / 100

    def test_delete_is_idempotent(self):
        d = DeleteSet(10)
        assert d.delete(3) is True
        assert d.delete(3) is False
        assert len(d) == 1

    def test_undelete(self):
        d = DeleteSet(10)
        d.delete(3)
        assert d.undelete(3) is True
        assert d.undelete(3) is False
        assert 3 not in d

    def test_live_count_and_density(self):
        d = DeleteSet(100)
        d.delete_many(range(25))
        assert d.live_count == 75
        assert d.density == pytest.approx(0.25)

    def test_live_ids(self):
        d = DeleteSet(5)
        d.delete_many([1, 3])
        assert list(d.live_ids()) == [0, 2, 4]

    def test_empty_bytes(self):
        assert len(DeleteSet.from_bytes(b"")) == 0


# ---------------------------------------------------------------------------
# Segment integrity
# ---------------------------------------------------------------------------

class TestSegmentIntegrity:
    @pytest.fixture
    def built(self, tmp_path):
        idx = Index(tmp_path / "idx")
        idx.add_documents(random_corpus(120, seed=1))
        return idx.segments[0]

    def test_manifest_records_stats(self, built):
        m = built.manifest
        assert m.doc_count == 120
        assert m.avg_doc_length > 0
        assert m.total_length == sum(d.length for d in built.docs)
        assert set(m.checksums) >= {TERMS, POSTINGS}

    def test_reopen(self, built):
        again = Segment(built.path)
        assert again.doc_count == built.doc_count
        assert again.manifest.checksums == built.manifest.checksums

    def test_corrupt_postings_detected(self, built):
        """Replication does not protect against this — a bad segment is bad in
        every replica, so the checksum is the only gate."""
        raw = bytearray((built.path / POSTINGS).read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        (built.path / POSTINGS).write_bytes(bytes(raw))
        with pytest.raises(SegmentCorrupt, match="checksum"):
            Segment(built.path)

    def test_corrupt_dictionary_detected(self, built):
        raw = bytearray((built.path / TERMS).read_bytes())
        raw[-1] ^= 0xFF
        (built.path / TERMS).write_bytes(bytes(raw))
        with pytest.raises(SegmentCorrupt, match="checksum"):
            Segment(built.path)

    def test_missing_manifest_rejected(self, built):
        """The manifest is written last, so a crash mid-write leaves a directory
        that is skipped rather than half-read."""
        (built.path / "manifest.json").unlink()
        with pytest.raises(SegmentCorrupt, match="no manifest"):
            Segment(built.path)

    def test_verification_can_be_skipped_explicitly(self, built):
        raw = bytearray((built.path / POSTINGS).read_bytes())
        raw[0] ^= 0xFF
        (built.path / POSTINGS).write_bytes(bytes(raw))
        Segment(built.path, verify=False)  # opt-in only; must not raise

    def test_analyzer_version_is_recorded(self, tmp_path):
        idx = Index(tmp_path / "idx")
        idx.add_documents(random_corpus(20, seed=1), analyzer_version="v7")
        assert idx.segments[0].manifest.analyzer_version == "v7"


# ---------------------------------------------------------------------------
# Tombstones at query time
# ---------------------------------------------------------------------------

class TestTombstones:
    @pytest.fixture
    def idx(self, tmp_path):
        index = Index(tmp_path / "idx")
        index.add_documents(random_corpus(300, seed=5))
        return index

    def test_deleted_document_disappears_from_results(self, idx):
        segment = idx.segments[0]
        hits = search(segment, ["black", "merger"], k=10)
        victim = hits[0].doc_id
        segment.delete(victim)
        assert victim not in {h.doc_id for h in search(segment, ["black", "merger"], k=10)}

    def test_deletion_persists_across_reopen(self, idx):
        segment = idx.segments[0]
        segment.delete(7)
        assert Segment(segment.path).is_deleted(7)

    def test_deletion_applies_at_query_time_not_merge_time(self, idx):
        """A legal removal cannot wait for the next merge, which may be a month
        away. The posting entries are still on disk; the filter is at read."""
        segment = idx.segments[0]
        before = len(list(segment.terms.terms()))
        segment.delete(3)
        assert len(list(segment.terms.terms())) == before  # postings untouched
        assert segment.is_deleted(3)

    def test_exhaustive_path_also_honours_tombstones(self, idx):
        segment = idx.segments[0]
        victim = search_exhaustive(segment, ["black"], k=5)[0].doc_id
        segment.delete(victim)
        assert victim not in {h.doc_id for h in search_exhaustive(segment, ["black"], k=5)}

    def test_delete_by_external_id(self, idx):
        assert idx.delete(idx.segments[0].docs[10].external_id) is True
        assert idx.segments[0].is_deleted(10)

    def test_delete_unknown_id(self, idx):
        assert idx.delete("no-such-document") is False

    def test_out_of_range_rejected(self, idx):
        with pytest.raises(IndexError):
            idx.segments[0].delete(999_999)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

class TestMerge:
    def _two_segments(self, tmp_path):
        idx = Index(tmp_path / "idx")
        # Deliberately different length profiles so the merged avgdl shifts.
        short = [
            InputDocument.from_tokens(f"s{i}", ["black", "merger"], static_rank=0.9 - i / 100)
            for i in range(60)
        ]
        long = [
            InputDocument.from_tokens(
                f"l{i}", ["black"] * 40 + ["merger"] * 20 + ["index"] * 30,
                static_rank=0.5 - i / 100,
            )
            for i in range(60)
        ]
        idx.add_documents(short, name="seg_a")
        idx.add_documents(long, name="seg_b")
        return idx

    def test_merge_changes_avgdl(self, tmp_path):
        idx = self._two_segments(tmp_path)
        a, b = idx.segments
        assert a.manifest.avg_doc_length != b.manifest.avg_doc_length
        idx.merge()
        merged = idx.segments[0].manifest.avg_doc_length
        assert min(a.manifest.avg_doc_length, b.manifest.avg_doc_length) < merged
        assert merged < max(a.manifest.avg_doc_length, b.manifest.avg_doc_length)

    def test_merge_recomputes_block_maxima(self, tmp_path):
        """The failure the doc singles out: stale skip data after a merge gives
        wrong results, silently.

        avgdl changes on merge, and it appears inside the saturation term. If it
        rises, saturation rises and a copied maximum becomes an underestimate —
        which prunes a winner with no error anywhere.
        """
        idx = self._two_segments(tmp_path)
        idx.merge()
        segment = idx.segments[0]
        scorer = segment.scorer
        avgdl = segment.stats.avg_doc_length

        checked = 0
        for term, _info in segment.terms.terms():
            cursor = segment.cursor(term)
            doc = cursor.doc()
            while doc != NO_MORE:
                actual = scorer.saturation(cursor.freq(), segment.doc_length(doc), avgdl)
                assert cursor.block_max() >= actual - 1e-6, (
                    f"stale maximum after merge: term={term!r} doc={doc}"
                )
                checked += 1
                doc = cursor.next_doc()
        assert checked > 100

    def test_search_still_correct_after_merge(self, tmp_path):
        idx = self._two_segments(tmp_path)
        idx.merge()
        segment = idx.segments[0]
        for terms in (["black"], ["merger"], ["black", "merger", "index"]):
            assert_same_results(
                search(segment, terms, k=20), search_exhaustive(segment, terms, k=20)
            )

    def test_merge_drops_tombstoned_documents(self, tmp_path):
        idx = self._two_segments(tmp_path)
        idx.segments[0].delete(0)
        idx.segments[0].delete(1)
        total = idx.doc_count
        stats = idx.merge()
        assert stats.docs_dropped == 2
        assert idx.segments[0].doc_count == total - 2

    def test_merge_preserves_static_rank_order(self, tmp_path):
        idx = self._two_segments(tmp_path)
        idx.merge()
        ranks = [d.static_rank for d in idx.segments[0].docs]
        assert ranks == sorted(ranks, reverse=True), "docID order is no longer quality order"

    def test_merge_preserves_all_live_documents(self, tmp_path):
        idx = self._two_segments(tmp_path)
        before = {d.external_id for s in idx.segments for d in s.docs}
        idx.merge()
        assert {d.external_id for d in idx.segments[0].docs} == before

    def test_merge_across_analyzer_versions_refused(self, tmp_path):
        idx = Index(tmp_path / "idx")
        idx.add_documents(random_corpus(20, seed=1), name="a", analyzer_version="v1")
        idx.add_documents(random_corpus(20, seed=2), name="b", analyzer_version="v2")
        with pytest.raises(ValueError, match="analyzer"):
            merge_segments(idx.segments, tmp_path / "out")

    def test_positions_survive_a_merge(self, tmp_path):
        idx = Index(tmp_path / "idx")
        idx.add_documents(
            [InputDocument.from_tokens("d0", ["a", "b", "a", "c", "a"], static_rank=1.0)],
            name="seg_a",
        )
        idx.add_documents(
            [InputDocument.from_tokens("d1", ["b", "a"], static_rank=0.5)], name="seg_b"
        )
        idx.merge()
        cursor = idx.segments[0].cursor("a")
        assert cursor.positions() == [0, 2, 4]

    def test_should_merge_triggers_on_segment_count(self, tmp_path):
        idx = Index(tmp_path / "idx")
        for i in range(10):
            idx.add_documents(random_corpus(5, seed=i), name=f"s{i}")
        assert should_merge(idx.segments, max_segments=8) is True

    def test_should_merge_triggers_on_dead_weight(self, tmp_path):
        idx = Index(tmp_path / "idx")
        idx.add_documents(random_corpus(100, seed=1))
        for i in range(40):
            idx.segments[0].delete(i)
        assert should_merge(idx.segments, delete_ratio=0.25) is True


# ---------------------------------------------------------------------------
# Multi-segment index
# ---------------------------------------------------------------------------

class TestIndex:
    def test_searches_across_segments(self, tmp_path):
        idx = Index(tmp_path / "idx")
        idx.add_documents(random_corpus(150, seed=1), name="a")
        idx.add_documents(random_corpus(150, seed=2), name="b")
        assert len(idx.segments) == 2
        hits = idx.search(["black", "merger"], k=10)
        assert len(hits) == 10
        assert len({h.external_id for h in hits}) == 10

    def test_global_stats_span_all_segments(self, tmp_path):
        """Segment-local IDF makes scores incomparable across segments, so the
        merged top-k comes out in the wrong order."""
        idx = Index(tmp_path / "idx")
        idx.add_documents(random_corpus(100, seed=1), name="a")
        idx.add_documents(random_corpus(200, seed=2), name="b")
        assert idx.stats.doc_count == 300
        assert idx.stats.doc_count != idx.segments[0].stats.doc_count

    def test_generation_publish_and_reload(self, tmp_path):
        idx = Index(tmp_path / "idx")
        idx.add_documents(random_corpus(50, seed=1))
        gen = idx.publish("gen-test-1")
        assert gen.segments == ["seg_0000"]

        reopened = Index(tmp_path / "idx")
        assert reopened.generation.id == "gen-test-1"
        assert reopened.doc_count == 50

    def test_generation_records_its_parent(self, tmp_path):
        idx = Index(tmp_path / "idx")
        idx.add_documents(random_corpus(20, seed=1))
        first = idx.publish("gen-1")
        idx.add_documents(random_corpus(20, seed=2), name="seg_0001")
        second = idx.publish("gen-2")
        assert second.parent == first.id

    def test_generation_json_is_stable(self, tmp_path):
        idx = Index(tmp_path / "idx")
        idx.add_documents(random_corpus(10, seed=1))
        idx.publish("gen-x")
        raw = json.loads((tmp_path / "idx" / "generation.json").read_text())
        assert raw["id"] == "gen-x" and raw["doc_count"] == 10

    def test_maybe_merge_is_a_noop_below_threshold(self, tmp_path):
        idx = Index(tmp_path / "idx")
        idx.add_documents(random_corpus(20, seed=1))
        assert idx.maybe_merge() is None
        assert len(idx.segments) == 1

    def test_empty_index(self, tmp_path):
        idx = Index(tmp_path / "empty")
        assert idx.doc_count == 0
        assert idx.search(["anything"], k=10) == []
