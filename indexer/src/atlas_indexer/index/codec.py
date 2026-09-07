"""Encoding primitives for posting lists.

    | Data       | Encoding                    | Why                                |
    | docIDs     | delta (d-gaps) + bitpacked  | gaps are small                     |
    | freqs      | bitpacked, per-block width  | mostly 1-3; a full byte is waste   |
    | positions  | delta + varint              | rare, so favour size over speed    |

The Target uses SIMD-BP128 for the bitpacked streams. This is the same layout —
fixed-width packing over a block of 128 — implemented scalar. Swapping in a
vectorised codec changes `pack`/`unpack` and nothing above them.
"""

from __future__ import annotations

import struct

BLOCK_SIZE = 128


# ---------------------------------------------------------------------------
# varint
# ---------------------------------------------------------------------------

def write_varint(out: bytearray, value: int) -> None:
    if value < 0:
        raise ValueError(f"varint is unsigned, got {value}")
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def read_varint(buf: bytes | memoryview, pos: int) -> tuple[int, int]:
    """Return (value, new_pos)."""
    shift = 0
    result = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long — stream is corrupt")


def write_varints(out: bytearray, values: list[int]) -> None:
    for v in values:
        write_varint(out, v)


def read_varints(buf: bytes | memoryview, pos: int, count: int) -> tuple[list[int], int]:
    out = []
    for _ in range(count):
        v, pos = read_varint(buf, pos)
        out.append(v)
    return out, pos


# ---------------------------------------------------------------------------
# delta / d-gap
# ---------------------------------------------------------------------------

def delta_encode(values: list[int], *, base: int = 0) -> list[int]:
    """Ascending values -> gaps. `base` is the previous block's last value."""
    out = []
    prev = base
    for v in values:
        if v < prev:
            raise ValueError(f"delta_encode requires ascending input: {v} < {prev}")
        out.append(v - prev)
        prev = v
    return out


def delta_decode(gaps: list[int], *, base: int = 0) -> list[int]:
    out = []
    acc = base
    for g in gaps:
        acc += g
        out.append(acc)
    return out


# ---------------------------------------------------------------------------
# bitpacking
# ---------------------------------------------------------------------------

def bits_needed(values: list[int]) -> int:
    """Width in bits of the largest value. Zero-width means all values are 0."""
    hi = max(values, default=0)
    return hi.bit_length()


def pack(values: list[int], width: int) -> bytes:
    """Pack `values` at fixed `width` bits each, LSB-first within a 64-bit word.

    Width 0 is legal and emits nothing: a block of identical d-gaps of 0 (or all
    frequencies of 1, once the -1 bias is applied) costs zero bytes.
    """
    if width == 0:
        return b""
    if width > 64:
        raise ValueError(f"width {width} exceeds 64 bits")

    out = bytearray()
    acc = 0
    acc_bits = 0
    limit = 1 << width
    for v in values:
        if v >= limit:
            raise ValueError(f"value {v} does not fit in {width} bits")
        acc |= v << acc_bits
        acc_bits += width
        while acc_bits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            acc_bits -= 8
    if acc_bits:
        out.append(acc & 0xFF)
    return bytes(out)


def unpack(buf: bytes | memoryview, pos: int, count: int, width: int) -> tuple[list[int], int]:
    """Inverse of `pack`. Returns (values, new_pos)."""
    if width == 0:
        return [0] * count, pos
    total_bits = count * width
    nbytes = (total_bits + 7) // 8
    chunk = bytes(buf[pos : pos + nbytes])
    if len(chunk) < nbytes:
        raise ValueError("truncated bitpacked block")

    acc = int.from_bytes(chunk, "little")
    mask = (1 << width) - 1
    out = [(acc >> (i * width)) & mask for i in range(count)]
    return out, pos + nbytes


def packed_size(count: int, width: int) -> int:
    return 0 if width == 0 else (count * width + 7) // 8


# ---------------------------------------------------------------------------
# floats (block maxima)
# ---------------------------------------------------------------------------

def write_f32(out: bytearray, value: float) -> None:
    out.extend(struct.pack("<f", value))


def read_f32(buf: bytes | memoryview, pos: int) -> tuple[float, int]:
    return struct.unpack_from("<f", buf, pos)[0], pos + 4


def round_up_f32(value: float) -> float:
    """Round a float32 up to the next representable value.

    Block maxima are stored as float32 but computed in float64. Truncating to
    float32 can round *down*, turning a true upper bound into a value fractionally
    below the real maximum — and a bound that is too low silently prunes documents
    that should have won. Nudging up costs nothing and keeps the bound safe.
    """
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return value
    as32 = struct.unpack("<f", struct.pack("<f", value))[0]
    if as32 >= value:
        return as32
    # Step one ULP up.
    bits = struct.unpack("<I", struct.pack("<f", as32))[0]
    bits = bits + 1 if as32 >= 0 else bits - 1
    return struct.unpack("<f", struct.pack("<I", bits))[0]
