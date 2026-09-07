"""Block-max posting lists.

On-disk layout for one term:

    varint doc_freq
    varint total_tf
    varint block_count
    varint skip_table_bytes
    ┌── skip table ─────────────────────────────────────────────┐
    │ per block:  last_doc_id (delta)                           │
    │             max_saturation (f32)   <- the key to everything│
    │             block_offset (delta)                          │
    │             doc_count                                     │
    │             pos_offset (delta)                            │
    └───────────────────────────────────────────────────────────┘
    ┌── blocks ─────────────────────────────────────────────────┐
    │ per block:  u8 docid_width, packed d-gaps                 │
    │             u8 freq_width,  packed (freq - 1)             │
    └───────────────────────────────────────────────────────────┘

The skip table is small, contiguous and read in full when the term is opened.
Blocks are decoded only when a cursor actually lands in one — which is what lets
Block-Max WAND skip a block "without decoding a single posting".

Positions live in a **separate stream**. Most queries never touch them; putting
them inline would force decoding 11 TB to answer queries that need 8 TB.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .codec import (
    BLOCK_SIZE,
    delta_decode,
    delta_encode,
    bits_needed,
    pack,
    read_f32,
    read_varint,
    round_up_f32,
    unpack,
    write_f32,
    write_varint,
)

NO_MORE = 1 << 62  # sentinel: cursor exhausted


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _Block:
    doc_ids: list[int] = field(default_factory=list)
    freqs: list[int] = field(default_factory=list)
    positions: list[list[int]] = field(default_factory=list)
    max_saturation: float = 0.0


class PostingsWriter:
    """Accumulates one term's postings, then serialises them.

    Documents must arrive in ascending docID order. Because docIDs are assigned
    in static-rank order at build time (see writer.py), ascending docID *is*
    descending quality — which is what makes early termination effective.
    """

    def __init__(self, *, block_size: int = BLOCK_SIZE) -> None:
        self.block_size = block_size
        self._blocks: list[_Block] = []
        self._current = _Block()
        self._last_doc = -1
        self.doc_freq = 0
        self.total_tf = 0

    def add(
        self,
        doc_id: int,
        freq: int,
        *,
        saturation: float,
        positions: list[int] | None = None,
    ) -> None:
        if doc_id <= self._last_doc:
            raise ValueError(f"postings must ascend: {doc_id} after {self._last_doc}")
        if freq < 1:
            raise ValueError("frequency must be >= 1")

        self._last_doc = doc_id
        self.doc_freq += 1
        self.total_tf += freq

        cur = self._current
        cur.doc_ids.append(doc_id)
        cur.freqs.append(freq)
        cur.positions.append(sorted(positions) if positions else [])
        if saturation > cur.max_saturation:
            cur.max_saturation = saturation

        if len(cur.doc_ids) >= self.block_size:
            self._blocks.append(cur)
            self._current = _Block()

    def finish(self) -> tuple[bytes, bytes]:
        """Return (postings_bytes, positions_bytes)."""
        if self._current.doc_ids:
            self._blocks.append(self._current)
            self._current = _Block()
        if not self._blocks:
            return b"", b""

        positions_out = bytearray()
        blocks_out = bytearray()
        skip_out = bytearray()

        prev_last_doc = 0
        prev_block_off = 0
        prev_pos_off = 0

        for block in self._blocks:
            block_off = len(blocks_out)
            pos_off = len(positions_out)

            # --- docIDs: d-gaps against the previous block's last docID -------
            # Cross-block continuity is what keeps gaps small at block borders;
            # restarting from 0 each block would waste bits on the first entry.
            gaps = delta_encode(block.doc_ids, base=prev_last_doc)
            width = bits_needed(gaps)
            blocks_out.append(width)
            blocks_out.extend(pack(gaps, width))

            # --- frequencies: bias by -1, they are always >= 1 ---------------
            biased = [f - 1 for f in block.freqs]
            fwidth = bits_needed(biased)
            blocks_out.append(fwidth)
            blocks_out.extend(pack(biased, fwidth))

            # --- positions, in their own stream ------------------------------
            for pos_list in block.positions:
                write_varint(positions_out, len(pos_list))
                prev = 0
                for p in pos_list:
                    write_varint(positions_out, p - prev)
                    prev = p

            # --- skip entry ---------------------------------------------------
            last_doc = block.doc_ids[-1]
            write_varint(skip_out, last_doc - prev_last_doc)
            # Maxima are computed in f64 and stored as f32. Plain truncation can
            # round DOWN, turning a true upper bound into a value fractionally
            # below the real maximum — which silently prunes a winner. Nudge up.
            write_f32(skip_out, round_up_f32(block.max_saturation))
            write_varint(skip_out, block_off - prev_block_off)
            write_varint(skip_out, len(block.doc_ids))
            write_varint(skip_out, pos_off - prev_pos_off)

            prev_last_doc = last_doc
            prev_block_off = block_off
            prev_pos_off = pos_off

        out = bytearray()
        write_varint(out, self.doc_freq)
        write_varint(out, self.total_tf)
        write_varint(out, len(self._blocks))
        write_varint(out, len(skip_out))
        out.extend(skip_out)
        out.extend(blocks_out)
        return bytes(out), bytes(positions_out)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SkipEntry:
    last_doc: int
    max_saturation: float
    block_offset: int
    doc_count: int
    pos_offset: int


class PostingCursor:
    """Block-skipping cursor over one term's postings.

    The API is shaped for Block-Max WAND: `block_max()` and `block_last()` are
    answerable from the skip table alone, so a caller can decide to skip a block
    before any of its postings are touched.
    """

    def __init__(
        self,
        buf: memoryview,
        offset: int,
        positions: memoryview | None = None,
        positions_base: int = 0,
    ) -> None:
        self._buf = buf
        self._positions = positions
        # Skip-table position offsets are relative to this term's slice of
        # the segment-wide stream.
        self._positions_base = positions_base

        pos = offset
        self.doc_freq, pos = read_varint(buf, pos)
        self.total_tf, pos = read_varint(buf, pos)
        block_count, pos = read_varint(buf, pos)
        skip_bytes, pos = read_varint(buf, pos)

        self.skips: list[SkipEntry] = []
        last_doc = 0
        block_off = 0
        pos_off = 0
        p = pos
        for _ in range(block_count):
            d, p = read_varint(buf, p)
            last_doc += d
            mx, p = read_f32(buf, p)
            bo, p = read_varint(buf, p)
            block_off += bo
            n, p = read_varint(buf, p)
            po, p = read_varint(buf, p)
            pos_off += po
            self.skips.append(SkipEntry(last_doc, mx, block_off, n, pos_off))

        self._blocks_base = pos + skip_bytes
        self._block_index = -1
        self._doc_ids: list[int] = []
        self._freqs: list[int] = []
        self._i = 0
        self._doc = -1
        if block_count:
            self._load_block(0)
            self._doc = self._doc_ids[0] if self._doc_ids else NO_MORE
        else:
            self._doc = NO_MORE

    # -- block-level (no decoding) ------------------------------------------

    @property
    def block_count(self) -> int:
        return len(self.skips)

    def block_max(self) -> float:
        """Upper bound on saturation for any document in the current block."""
        if 0 <= self._block_index < len(self.skips):
            return self.skips[self._block_index].max_saturation
        return 0.0

    def block_last(self) -> int:
        if 0 <= self._block_index < len(self.skips):
            return self.skips[self._block_index].last_doc
        return NO_MORE

    def _block_containing(self, target: int) -> int:
        """Index of the first block whose last docID is >= `target`. Pure read."""
        i = self._block_index if self._block_index >= 0 else 0
        while i < len(self.skips) and self.skips[i].last_doc < target:
            i += 1
        return i

    def block_max_at(self, target: int) -> float:
        """Bound for the block that would contain `target`, **without moving**.

        Separate from `advance_block` on purpose. Block-Max WAND inspects bounds
        for terms that are still positioned behind the pivot; if inspecting also
        advanced the document cursor, those terms' remaining postings would be
        skipped and results would go missing.
        """
        i = self._block_containing(target)
        return self.skips[i].max_saturation if i < len(self.skips) else 0.0

    def block_last_at(self, target: int) -> int:
        """Last docID of the block that would contain `target`. Pure read."""
        i = self._block_containing(target)
        return self.skips[i].last_doc if i < len(self.skips) else NO_MORE

    def advance_block(self, target: int) -> None:
        """Position the *block* cursor so `block_last() >= target`.

        Reads only the skip table. This is the operation that makes skipping
        cheap: a block whose bound cannot beat the threshold is stepped over
        without any of its postings being decoded.
        """
        i = max(self._block_index, 0)
        while i < len(self.skips) and self.skips[i].last_doc < target:
            i += 1
        if i >= len(self.skips):
            self._block_index = len(self.skips)
            self._doc = NO_MORE
            return
        if i != self._block_index:
            self._load_block(i)
            self._i = 0
            self._doc = self._doc_ids[0]

    # -- document-level -----------------------------------------------------

    def doc(self) -> int:
        return self._doc

    def freq(self) -> int:
        if self._doc is NO_MORE or self._i >= len(self._freqs):
            return 0
        return self._freqs[self._i]

    def next_doc(self) -> int:
        if self._doc == NO_MORE:
            return NO_MORE
        self._i += 1
        if self._i < len(self._doc_ids):
            self._doc = self._doc_ids[self._i]
            return self._doc
        nxt = self._block_index + 1
        if nxt >= len(self.skips):
            self._block_index = len(self.skips)
            self._doc = NO_MORE
            return NO_MORE
        self._load_block(nxt)
        self._i = 0
        self._doc = self._doc_ids[0]
        return self._doc

    def advance(self, target: int) -> int:
        """Move to the first document >= `target`."""
        if self._doc == NO_MORE:
            return NO_MORE
        if self._doc >= target:
            return self._doc
        self.advance_block(target)
        if self._doc == NO_MORE:
            return NO_MORE
        while self._i < len(self._doc_ids) and self._doc_ids[self._i] < target:
            self._i += 1
        if self._i < len(self._doc_ids):
            self._doc = self._doc_ids[self._i]
            return self._doc
        return self.next_doc()

    def positions(self) -> list[int]:
        """Decode positions for the current document — the rare path."""
        if self._positions is None or self._doc == NO_MORE:
            return []
        skip = self.skips[self._block_index]
        p = self._positions_base + skip.pos_offset
        for j in range(skip.doc_count):
            count, p = read_varint(self._positions, p)
            if j == self._i:
                out = []
                acc = 0
                for _ in range(count):
                    d, p = read_varint(self._positions, p)
                    acc += d
                    out.append(acc)
                return out
            for _ in range(count):
                _, p = read_varint(self._positions, p)
        return []

    # -- internals ----------------------------------------------------------

    def _load_block(self, index: int) -> None:
        skip = self.skips[index]
        base = self._blocks_base + skip.block_offset
        prev_last = self.skips[index - 1].last_doc if index > 0 else 0

        width = self._buf[base]
        p = base + 1
        gaps, p = unpack(self._buf, p, skip.doc_count, width)
        self._doc_ids = delta_decode(gaps, base=prev_last)

        fwidth = self._buf[p]
        p += 1
        biased, p = unpack(self._buf, p, skip.doc_count, fwidth)
        self._freqs = [b + 1 for b in biased]

        self._block_index = index
