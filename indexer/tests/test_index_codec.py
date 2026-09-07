from __future__ import annotations

import random
import struct

import pytest

from atlas_indexer.index.codec import (
    bits_needed,
    delta_decode,
    delta_encode,
    pack,
    packed_size,
    read_f32,
    read_varint,
    read_varints,
    round_up_f32,
    unpack,
    write_f32,
    write_varint,
    write_varints,
)


class TestVarint:
    @pytest.mark.parametrize("value", [0, 1, 127, 128, 300, 16383, 16384, 2**32, 2**62])
    def test_roundtrip(self, value):
        out = bytearray()
        write_varint(out, value)
        assert read_varint(bytes(out), 0) == (value, len(out))

    def test_small_values_are_one_byte(self):
        out = bytearray()
        write_varint(out, 127)
        assert len(out) == 1

    def test_sequence_roundtrip(self):
        values = [random.randint(0, 2**40) for _ in range(200)]
        out = bytearray()
        write_varints(out, values)
        assert read_varints(bytes(out), 0, len(values))[0] == values

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            write_varint(bytearray(), -1)

    def test_corrupt_stream_rejected(self):
        with pytest.raises(ValueError, match="corrupt"):
            read_varint(b"\xff" * 20, 0)


class TestDelta:
    def test_roundtrip(self):
        values = sorted(random.sample(range(100_000), 500))
        assert delta_decode(delta_encode(values)) == values

    def test_base_carries_across_blocks(self):
        """Cross-block continuity is what keeps gaps small at block borders."""
        values = [1000, 1005, 1009]
        gaps = delta_encode(values, base=997)
        assert gaps == [3, 5, 4]
        assert delta_decode(gaps, base=997) == values

    def test_descending_input_rejected(self):
        with pytest.raises(ValueError, match="ascend"):
            delta_encode([5, 3])

    def test_empty(self):
        assert delta_encode([]) == []
        assert delta_decode([]) == []


class TestBitpacking:
    @pytest.mark.parametrize("width", range(0, 18))
    def test_roundtrip_at_every_width(self, width):
        limit = (1 << width) if width else 1
        values = [random.randrange(limit) for _ in range(128)]
        packed = pack(values, width)
        assert len(packed) == packed_size(128, width)
        assert unpack(packed, 0, 128, width)[0] == values

    def test_zero_width_costs_nothing(self):
        """A block of all-identical frequencies should occupy no bytes at all."""
        assert pack([0] * 128, 0) == b""
        assert unpack(b"", 0, 128, 0)[0] == [0] * 128

    def test_bits_needed(self):
        assert bits_needed([0, 0, 0]) == 0
        assert bits_needed([1]) == 1
        assert bits_needed([255]) == 8
        assert bits_needed([256]) == 9
        assert bits_needed([]) == 0

    def test_overflow_rejected(self):
        with pytest.raises(ValueError, match="does not fit"):
            pack([4], 2)

    def test_truncated_buffer_rejected(self):
        with pytest.raises(ValueError, match="truncated"):
            unpack(b"\x00", 0, 128, 8)

    def test_realistic_freqs_are_tiny(self):
        """Frequencies are mostly 1-3; biased by -1 they need ~2 bits, not 8."""
        freqs = [random.choice([1, 1, 1, 2, 2, 3]) for _ in range(128)]
        biased = [f - 1 for f in freqs]
        assert len(pack(biased, bits_needed(biased))) <= 32  # vs 128 bytes raw


class TestFloats:
    def test_roundtrip(self):
        out = bytearray()
        write_f32(out, 6.25)
        assert read_f32(bytes(out), 0) == (6.25, 4)

    def test_round_up_never_goes_below_the_input(self):
        """Block maxima are computed in f64 and stored as f32.

        Truncating can round DOWN, turning a true upper bound into a value just
        under the real maximum — and a bound that is too low silently prunes
        documents that should have won.
        """
        for _ in range(2000):
            value = random.uniform(0, 10)
            assert round_up_f32(value) >= value

    def test_round_up_is_representable_as_f32(self):
        for _ in range(500):
            value = round_up_f32(random.uniform(0, 10))
            assert struct.unpack("<f", struct.pack("<f", value))[0] == value

    def test_exact_values_unchanged(self):
        assert round_up_f32(0.5) == 0.5
        assert round_up_f32(0.0) == 0.0
