"""Disk-backed, pause-aware PCM concatenation with pydub conversion semantics.

Only a bounded PCM block is resident. A format increase converts the accumulated
disk prefix once; equal-format appends never copy that prefix. Resampler state
crosses block boundaries but resets at the same segment boundaries as pydub.
"""
from __future__ import annotations

import os
from pathlib import Path
import struct

BLOCK = 256 * 1024


class WaveInput:
    def __init__(self, path):
        self.path = Path(path)
        with self.path.open("rb") as reader:
            header = reader.read(12)
            if len(header) != 12 or header[:4] not in (b"RIFF", b"RF64") or header[8:] != b"WAVE":
                raise ValueError("Not a PCM WAV")
            fmt = None
            large_size = None
            while chunk := reader.read(8):
                if len(chunk) != 8:
                    raise ValueError("Truncated WAV chunk")
                name, size = struct.unpack("<4sI", chunk)
                if name == b"ds64":
                    if size < 28:
                        raise ValueError("Invalid RF64 sizes")
                    values = reader.read(28)
                    large_size = struct.unpack("<QQQI", values)[1]
                    reader.seek(size - 28 + size % 2, 1)
                elif name == b"fmt ":
                    data = reader.read(min(size, 40))
                    if len(data) < 16:
                        raise ValueError("Invalid WAV format")
                    tag, channels, rate, _, alignment, bits = struct.unpack("<HHIIHH", data[:16])
                    if tag == 65534 and len(data) >= 40:
                        tag = struct.unpack("<H", data[24:26])[0]
                    if tag != 1 or bits not in (8, 16, 24, 32) or not channels or not rate or alignment != channels * (bits // 8):
                        raise ValueError("Unsupported PCM WAV format")
                    fmt = (channels, rate, bits // 8)
                    reader.seek(size - len(data) + size % 2, 1)
                elif name == b"data":
                    if fmt is None:
                        raise ValueError("WAV data precedes format")
                    self.disk_format = fmt
                    self.format = (*fmt[:2], 4 if fmt[2] == 3 else fmt[2])
                    self.offset = reader.tell()
                    self.size = large_size if size == 0xffffffff else size
                    if self.size is None or self.size % (fmt[0] * fmt[2]) or self.offset + self.size > os.fstat(reader.fileno()).st_size:
                        raise ValueError("Truncated PCM WAV data")
                    return
                else:
                    reader.seek(size + size % 2, 1)
        raise ValueError("WAV has no PCM data")

    def chunks(self):
        import audioop
        width = self.disk_format[2]
        frame = self.disk_format[0] * width
        block = max(frame, BLOCK // frame * frame)
        remaining = self.size
        with self.path.open("rb") as reader:
            reader.seek(self.offset)
            while remaining:
                data = reader.read(min(remaining, block))
                if not data or len(data) % frame:
                    raise ValueError("PCM input changed while merging")
                remaining -= len(data)
                if width == 1:
                    data = audioop.bias(data, 1, -128)
                elif width == 3:
                    # pydub inserts sign padding as the low byte of each sample.
                    out = bytearray(len(data) // 3 * 4)
                    for i in range(0, len(data), 3):
                        j = i // 3 * 4
                        out[j] = 255 if data[i + 2] > 127 else 0
                        out[j + 1:j + 4] = data[i:i + 3]
                    data = bytes(out)
                yield data


def convert(chunks, source, target):
    import audioop
    channels, rate, width = source
    target_channels, target_rate, target_width = target
    state = None
    for data in chunks:
        if channels != target_channels:
            if channels != 1:
                raise ValueError("Only mono-to-multi channel conversion is supported")
            if target_channels == 2:
                data = audioop.tostereo(data, width, 1, 1)
            else:
                data = b"".join(data[i:i + width] * target_channels for i in range(0, len(data), width))
        if rate != target_rate:
            data, state = audioop.ratecv(data, width, target_channels, rate, target_rate, state)
        if width != target_width:
            data = audioop.lin2lin(data, width, target_width)
        if data:
            yield data


def silence(milliseconds):
    remaining = int(11025 * (milliseconds / 1000.0)) * 2
    while remaining:
        count = min(remaining, BLOCK)
        yield b"\0" * count
        remaining -= count


def raw_chunks(path, frame):
    with Path(path).open("rb") as reader:
        while data := reader.read(max(frame, BLOCK // frame * frame)):
            yield data


def write_wave(pcm_path, output_path, fmt):
    """Emit classic WAV or RF64, streaming the closed PCM spool."""
    channels, rate, width = fmt
    size = Path(pcm_path).stat().st_size
    frame = channels * width
    # RIFF permits an odd data length, followed by one uncounted padding byte.
    padding = size % 2
    rf64 = size + 36 + padding > 0xffffffff
    with Path(output_path).open("wb") as output:
        if rf64:
            output.write(struct.pack("<4sI4s4sIQQQI", b"RF64", 0xffffffff, b"WAVE", b"ds64", 28,
                                     size + 72 + padding, size, size // frame, 0))
        else:
            output.write(struct.pack("<4sI4s", b"RIFF", size + 36 + padding, b"WAVE"))
        output.write(struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, channels, rate,
                                 rate * frame, frame, width * 8))
        output.write(struct.pack("<4sI", b"data", 0xffffffff if rf64 else size))
        for data in raw_chunks(pcm_path, frame):
            if width == 1:
                import audioop
                data = audioop.bias(data, 1, 128)
            output.write(data)
        if padding:
            output.write(b"\0")
    return round(1000 * (size / frame) / rate)


class StreamingMerge:
    def __init__(self, output_path):
        self.output_path = Path(output_path)
        self.pcm_path = self.output_path.with_suffix(self.output_path.suffix + ".pcm")
        self.format = None
        self.pcm_path.write_bytes(b"")

    def append(self, source: WaveInput, gap_ms=None):
        fmt = source.format
        if self.format is None:
            self.format = fmt
            chunks = source.chunks()
        else:
            if gap_ms is None or gap_ms < 0:
                raise ValueError("A nonnegative boundary gap is required")
            silence_fmt = (1, 11025, 2)
            tail_fmt = tuple(max(a, b) for a, b in zip(fmt, silence_fmt))
            target = tuple(max(a, b) for a, b in zip(self.format, tail_fmt))
            if target != self.format:
                replacement = self.pcm_path.with_suffix(".resampled")
                try:
                    with replacement.open("wb") as writer:
                        for data in convert(raw_chunks(self.pcm_path, self.format[0] * self.format[2]), self.format, target):
                            writer.write(data)
                    os.replace(replacement, self.pcm_path)
                finally:
                    replacement.unlink(missing_ok=True)
                self.format = target
            def tail():
                yield from convert(silence(gap_ms), silence_fmt, tail_fmt)
                yield from convert(source.chunks(), fmt, tail_fmt)
            chunks = convert(tail(), tail_fmt, target)
        with self.pcm_path.open("ab") as writer:
            for data in chunks:
                writer.write(data)

    def finish(self):
        if self.format is None:
            raise ValueError("No readable audio")
        return write_wave(self.pcm_path, self.output_path, self.format)

    def close(self):
        self.pcm_path.unlink(missing_ok=True)
