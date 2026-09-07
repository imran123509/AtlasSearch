from __future__ import annotations

import random

import pytest

from atlas_indexer.index.postings import NO_MORE, PostingCursor, PostingsWriter


def build(entries, *, block_size=8):
    """entries: list of (doc_id, freq, saturation, positions)."""
    w = PostingsWriter(block_size=block_size)
    for doc_id, freq, sat, positions in entries:
        w.add(doc_id, freq, saturation=sat, positions=positions)
    postings, positions_buf = w.finish()
    return PostingCursor(memoryview(postings), 0, memoryview(positions_buf)), w


def simple(doc_ids, *, block_size=8, freq=1, sat=1.0):
    return build([(d, freq, sat, [0]) for d in doc_ids], block_size=block_size)


class TestRoundtrip:
    def test_single_block(self):
        cursor, w = simple([1, 5, 9])
        assert w.doc_freq == 3
        got = []
        doc = cursor.doc()
        while doc != NO_MORE:
            got.append((doc, cursor.freq()))
            doc = cursor.next_doc()
        assert got == [(1, 1), (5, 1), (9, 1)]

    def test_multiple_blocks(self):
        ids = list(range(0, 200, 3))
        cursor, _ = simple(ids, block_size=8)
        assert cursor.block_count == (len(ids) + 7) // 8
        got = []
        doc = cursor.doc()
        while doc != NO_MORE:
            got.append(doc)
            doc = cursor.next_doc()
        assert got == ids

    def test_frequencies_preserved(self):
        entries = [(i, random.randint(1, 40), 1.0, [0]) for i in range(50)]
        cursor, _ = build(entries, block_size=8)
        got = []
        doc = cursor.doc()
        while doc != NO_MORE:
            got.append((doc, cursor.freq()))
            doc = cursor.next_doc()
        assert got == [(d, f) for d, f, _, _ in entries]

    def test_large_docids(self):
        ids = sorted(random.sample(range(10_000_000), 300))
        cursor, _ = simple(ids, block_size=16)
        got = []
        doc = cursor.doc()
        while doc != NO_MORE:
            got.append(doc)
            doc = cursor.next_doc()
        assert got == ids

    def test_empty_list(self):
        w = PostingsWriter()
        assert w.finish() == (b"", b"")

    def test_ascending_order_enforced(self):
        w = PostingsWriter()
        w.add(5, 1, saturation=1.0)
        with pytest.raises(ValueError, match="ascend"):
            w.add(3, 1, saturation=1.0)

    def test_zero_frequency_rejected(self):
        with pytest.raises(ValueError, match=">= 1"):
            PostingsWriter().add(1, 0, saturation=1.0)


class TestSkipTable:
    def test_block_maxima_recorded(self):
        entries = [(i, 1, float(i % 5), [0]) for i in range(24)]
        cursor, _ = build(entries, block_size=8)
        # Each block of 8 spans i%5 values covering 0..4, so max is 4.
        assert all(s.max_saturation >= 4.0 for s in cursor.skips)

    def test_block_last_docids_recorded(self):
        cursor, _ = simple(list(range(0, 32)), block_size=8)
        assert [s.last_doc for s in cursor.skips] == [7, 15, 23, 31]

    def test_block_max_reflects_current_block(self):
        entries = [(i, 1, 1.0 if i < 8 else 9.0, [0]) for i in range(16)]
        cursor, _ = build(entries, block_size=8)
        assert cursor.block_max() == pytest.approx(1.0, abs=1e-5)
        cursor.advance(8)
        assert cursor.block_max() == pytest.approx(9.0, abs=1e-5)

    def test_advance_block_reads_only_the_skip_table(self):
        """The operation that makes skipping cheap."""
        cursor, _ = simple(list(range(0, 400)), block_size=8)
        cursor.advance_block(350)
        assert cursor.block_last() >= 350


class TestAdvance:
    def test_advance_to_exact_doc(self):
        cursor, _ = simple([10, 20, 30, 40, 50], block_size=2)
        assert cursor.advance(30) == 30

    def test_advance_to_gap_lands_on_next(self):
        cursor, _ = simple([10, 20, 30, 40], block_size=2)
        assert cursor.advance(25) == 30

    def test_advance_past_end_exhausts(self):
        cursor, _ = simple([10, 20, 30], block_size=2)
        assert cursor.advance(999) == NO_MORE

    def test_advance_is_idempotent(self):
        cursor, _ = simple([10, 20, 30], block_size=2)
        assert cursor.advance(20) == 20
        assert cursor.advance(20) == 20

    def test_advance_backwards_is_a_noop(self):
        cursor, _ = simple([10, 20, 30], block_size=2)
        cursor.advance(30)
        assert cursor.advance(10) == 30

    def test_advance_across_many_blocks(self):
        ids = list(range(0, 1000, 7))
        cursor, _ = simple(ids, block_size=8)
        for target in (0, 100, 501, 700, 994):
            c, _ = simple(ids, block_size=8)
            expected = next((d for d in ids if d >= target), NO_MORE)
            assert c.advance(target) == expected

    def test_random_advance_sequence_matches_linear_scan(self):
        ids = sorted(random.sample(range(50_000), 800))
        cursor, _ = simple(ids, block_size=32)
        target = 0
        for _ in range(200):
            target += random.randint(1, 300)
            expected = next((d for d in ids if d >= target), NO_MORE)
            got = cursor.advance(target)
            assert got == expected
            if got == NO_MORE:
                break
            target = got


class TestPositions:
    def test_positions_roundtrip(self):
        entries = [
            (0, 3, 1.0, [1, 5, 9]),
            (1, 1, 1.0, [42]),
            (2, 2, 1.0, [0, 7]),
        ]
        cursor, _ = build(entries, block_size=8)
        got = []
        doc = cursor.doc()
        while doc != NO_MORE:
            got.append(cursor.positions())
            doc = cursor.next_doc()
        assert got == [[1, 5, 9], [42], [0, 7]]

    def test_positions_across_block_boundaries(self):
        entries = [(i, 2, 1.0, [i, i + 100]) for i in range(20)]
        cursor, _ = build(entries, block_size=4)
        cursor.advance(17)
        assert cursor.positions() == [17, 117]

    def test_positions_absent_when_stream_not_supplied(self):
        w = PostingsWriter()
        w.add(0, 1, saturation=1.0, positions=[3])
        postings, _ = w.finish()
        cursor = PostingCursor(memoryview(postings), 0, None)
        assert cursor.positions() == []

    def test_positions_are_sorted(self):
        cursor, _ = build([(0, 3, 1.0, [9, 1, 5])], block_size=4)
        assert cursor.positions() == [1, 5, 9]
