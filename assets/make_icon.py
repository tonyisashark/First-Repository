"""Generate ``assets/icon.ico`` with no third-party dependencies.

Draws a simple thermometer glyph on a blue background and writes a multi-size
Windows ``.ico`` (16/32/48 px). Run directly to (re)generate the icon:

    python assets/make_icon.py
"""

from __future__ import annotations

import os
import struct

BG = (15, 98, 146)       # blue
WHITE = (245, 245, 245)
RED = (220, 50, 47)


def _color_at(x: int, y: int, size: int):
    u = (x + 0.5) / size
    v = (y + 0.5) / size
    # Thermometer bulb (bottom).
    dx, dy = u - 0.5, v - 0.80
    if dx * dx + dy * dy <= 0.155 ** 2:
        return RED
    # Thermometer tube (vertical), white above the mercury, red below.
    if abs(u - 0.5) <= 0.085 and 0.16 <= v <= 0.82:
        return RED if v >= 0.52 else WHITE
    return BG


def _xor_bitmap(size: int) -> bytes:
    # 32-bit BGRA, bottom-up rows.
    rows = []
    for y in range(size - 1, -1, -1):
        row = bytearray()
        for x in range(size):
            r, g, b = _color_at(x, y, size)
            row += bytes((b, g, r, 255))
        rows.append(bytes(row))
    return b"".join(rows)


def _bmp_for_icon(size: int) -> bytes:
    xor = _xor_bitmap(size)
    and_stride = ((size + 31) // 32) * 4   # 1bpp mask, rows padded to 32 bits
    and_mask = b"\x00" * (and_stride * size)  # all opaque (alpha drives transparency)
    header = struct.pack(
        "<IiiHHIIiiII",
        40,            # biSize
        size,          # biWidth
        size * 2,      # biHeight (XOR + AND)
        1,             # biPlanes
        32,            # biBitCount
        0,             # biCompression (BI_RGB)
        len(xor) + len(and_mask),
        0, 0, 0, 0,
    )
    return header + xor + and_mask


def build_ico(path: str, sizes=(16, 32, 48)) -> None:
    images = [_bmp_for_icon(s) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(sizes))  # ICONDIR
    offset = 6 + 16 * len(sizes)
    entries, blob = b"", b""
    for size, img in zip(sizes, images):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(img), offset)
        blob += img
        offset += len(img)
    with open(path, "wb") as fh:
        fh.write(out + entries + blob)


if __name__ == "__main__":
    target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    build_ico(target)
    print(f"Wrote {target}")
