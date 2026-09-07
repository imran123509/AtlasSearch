"""Term dictionary: term -> posting-list offset.

Front-coded in blocks of 16, with a sparse index holding only each block's first
term. Lookup is a binary search over the sparse index followed by a short linear
scan inside one block.

Front coding exploits the fact that a sorted term list shares long prefixes
("retrieval", "retrieve", "retrieved"): each term after the first in a block
stores only how many leading bytes it shares with its predecessor plus the
remaining suffix. On real vocabularies this is a ~60% saving over storing terms
whole, which is what keeps ~2x10^9 terms inside 0.4 TB.

The Target uses an FST, which additionally supports prefix and fuzzy enumeration.
This layout supports exact lookup and ordered iteration, which is what the
retrieval path needs.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from .codec import read_varint, write_varint

DICT_BLOCK = 16


@dataclass(slots=True)
class TermInfo:
    doc_freq: int
    total_tf: int
    postings_offset: int
    # Where this term's slice of the segment-wide position stream begins. The
    # per-block offsets in the skip table are relative to this, so without it a
    # cursor reads the previous term's positions.
    positions_offset: int = 0


class DictionaryWriter:
    def __init__(self, *, block_size: int = DICT_BLOCK) -> None:
        self.block_size = block_size
        self._terms: list[tuple[bytes, TermInfo]] = []

    def add(self, term: str, info: TermInfo) -> None:
        self._terms.append((term.encode("utf-8"), info))

    def finish(self) -> bytes:
        # Sorting by UTF-8 bytes (not by str) so on-disk order matches the
        # comparisons the reader will make.
        self._terms.sort(key=lambda kv: kv[0])

        blocks = bytearray()
        index: list[tuple[bytes, int]] = []

        for start in range(0, len(self._terms), self.block_size):
            chunk = self._terms[start : start + self.block_size]
            index.append((chunk[0][0], len(blocks)))
            write_varint(blocks, len(chunk))

            prev = b""
            for term, info in chunk:
                shared = 0
                if prev:
                    limit = min(len(prev), len(term))
                    while shared < limit and prev[shared] == term[shared]:
                        shared += 1
                suffix = term[shared:]
                write_varint(blocks, shared)
                write_varint(blocks, len(suffix))
                blocks.extend(suffix)
                write_varint(blocks, info.doc_freq)
                write_varint(blocks, info.total_tf)
                write_varint(blocks, info.postings_offset)
                write_varint(blocks, info.positions_offset)
                prev = term

        out = bytearray()
        write_varint(out, len(self._terms))
        write_varint(out, len(index))
        for term, offset in index:
            write_varint(out, len(term))
            out.extend(term)
            write_varint(out, offset)
        write_varint(out, len(blocks))
        out.extend(blocks)
        return bytes(out)


class DictionaryReader:
    def __init__(self, buf: bytes | memoryview) -> None:
        self._buf = memoryview(buf) if not isinstance(buf, memoryview) else buf
        pos = 0
        self.term_count, pos = read_varint(self._buf, pos)
        index_count, pos = read_varint(self._buf, pos)

        self._index_terms: list[bytes] = []
        self._index_offsets: list[int] = []
        for _ in range(index_count):
            n, pos = read_varint(self._buf, pos)
            self._index_terms.append(bytes(self._buf[pos : pos + n]))
            pos += n
            off, pos = read_varint(self._buf, pos)
            self._index_offsets.append(off)

        blocks_len, pos = read_varint(self._buf, pos)
        self._blocks_base = pos
        self._blocks_len = blocks_len

    def __len__(self) -> int:
        return self.term_count

    def get(self, term: str) -> TermInfo | None:
        key = term.encode("utf-8")
        if not self._index_terms:
            return None
        # bisect_right - 1 gives the last block whose first term is <= key.
        i = bisect.bisect_right(self._index_terms, key) - 1
        if i < 0:
            return None
        for candidate, info in self._iter_block(self._index_offsets[i]):
            if candidate == key:
                return info
            if candidate > key:
                return None  # sorted; we have passed it
        return None

    def __contains__(self, term: str) -> bool:
        return self.get(term) is not None

    def terms(self):
        """Iterate every (term, TermInfo) in sorted order — used by merge."""
        for offset in self._index_offsets:
            for term, info in self._iter_block(offset):
                yield term.decode("utf-8"), info

    def _iter_block(self, offset: int):
        pos = self._blocks_base + offset
        count, pos = read_varint(self._buf, pos)
        prev = b""
        for _ in range(count):
            shared, pos = read_varint(self._buf, pos)
            suffix_len, pos = read_varint(self._buf, pos)
            suffix = bytes(self._buf[pos : pos + suffix_len])
            pos += suffix_len
            term = prev[:shared] + suffix
            df, pos = read_varint(self._buf, pos)
            tf, pos = read_varint(self._buf, pos)
            off, pos = read_varint(self._buf, pos)
            pos_off, pos = read_varint(self._buf, pos)
            yield term, TermInfo(df, tf, off, pos_off)
            prev = term
