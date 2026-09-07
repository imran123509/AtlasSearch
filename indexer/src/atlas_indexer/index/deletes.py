"""Delete bitmap — tombstones applied at query time.

Segments are immutable, so a deletion cannot rewrite the posting lists it appears
in. Instead the docID is marked here and filtered during retrieval; the posting
entries physically disappear at the next merge.

**Applied at query time, not merge time.** That distinction is a legal
requirement rather than an optimisation: a right-to-be-forgotten or DMCA removal
has to take effect in minutes, and the next full merge may be a month away.
See features/FAILURE-HANDLING.md.

Storage adapts to density, because deletions are usually sparse but occasionally
are not:

  * sparse (< 5%)  -> sorted d-gaps, varint encoded
  * dense          -> a raw bitmap

which is a two-container version of what a roaring bitmap does properly.
"""

from __future__ import annotations

from .codec import read_varint, write_varint

_SPARSE = 0
_DENSE = 1
_SPARSE_THRESHOLD = 0.05


class DeleteSet:
    def __init__(self, doc_count: int = 0) -> None:
        self.doc_count = doc_count
        self._deleted: set[int] = set()

    # -- mutation -----------------------------------------------------------

    def delete(self, doc_id: int) -> bool:
        """Mark a docID deleted. Returns False if it was already marked."""
        if doc_id in self._deleted:
            return False
        self._deleted.add(doc_id)
        return True

    def delete_many(self, doc_ids) -> int:
        before = len(self._deleted)
        self._deleted.update(doc_ids)
        return len(self._deleted) - before

    def undelete(self, doc_id: int) -> bool:
        """Un-mark a docID. Returns True if it had been deleted."""
        if doc_id in self._deleted:
            self._deleted.discard(doc_id)
            return True
        return False

    # -- query --------------------------------------------------------------

    def __contains__(self, doc_id: int) -> bool:
        return doc_id in self._deleted

    def is_deleted(self, doc_id: int) -> bool:
        return doc_id in self._deleted

    def __len__(self) -> int:
        return len(self._deleted)

    @property
    def live_count(self) -> int:
        return max(0, self.doc_count - len(self._deleted))

    @property
    def density(self) -> float:
        return len(self._deleted) / self.doc_count if self.doc_count else 0.0

    def live_ids(self):
        for doc_id in range(self.doc_count):
            if doc_id not in self._deleted:
                yield doc_id

    # -- serialisation ------------------------------------------------------

    def to_bytes(self) -> bytes:
        out = bytearray()
        write_varint(out, self.doc_count)

        if self.density < _SPARSE_THRESHOLD:
            out.append(_SPARSE)
            ids = sorted(self._deleted)
            write_varint(out, len(ids))
            prev = 0
            for doc_id in ids:
                write_varint(out, doc_id - prev)
                prev = doc_id
            return bytes(out)

        out.append(_DENSE)
        nbytes = (self.doc_count + 7) // 8
        bitmap = bytearray(nbytes)
        for doc_id in self._deleted:
            bitmap[doc_id >> 3] |= 1 << (doc_id & 7)
        write_varint(out, nbytes)
        out.extend(bitmap)
        return bytes(out)

    @classmethod
    def from_bytes(cls, buf: bytes | memoryview) -> DeleteSet:
        if not buf:
            return cls(0)
        pos = 0
        doc_count, pos = read_varint(buf, pos)
        obj = cls(doc_count)
        kind = buf[pos]
        pos += 1

        if kind == _SPARSE:
            count, pos = read_varint(buf, pos)
            acc = 0
            for _ in range(count):
                gap, pos = read_varint(buf, pos)
                acc += gap
                obj._deleted.add(acc)
            return obj

        nbytes, pos = read_varint(buf, pos)
        bitmap = buf[pos : pos + nbytes]
        for byte_index in range(nbytes):
            byte = bitmap[byte_index]
            if not byte:
                continue
            for bit in range(8):
                if byte & (1 << bit):
                    obj._deleted.add(byte_index * 8 + bit)
        return obj
