"""Generate assets/icon.ico (64x64, 32-bit BGRA) with no dependencies.

Draws a rising equity curve on a dark rounded tile -- enough to make the
packaged exe recognisable without shipping binary assets in the repo.
Run: python assets/make_icon.py
"""

from __future__ import annotations

import struct
from pathlib import Path

SIZE = 64
RADIUS = 12
BG = (24, 32, 40, 255)        # RGBA
LINE = (108, 211, 134, 255)
DOT = (235, 235, 235, 255)

# rising, slightly jagged equity curve as (x, y) anchors, y from the top
ANCHORS = [(6, 50), (16, 44), (24, 47), (34, 34), (42, 37), (50, 22), (58, 12)]


def in_tile(x: int, y: int) -> bool:
    r = RADIUS
    cx = min(max(x, r), SIZE - 1 - r)
    cy = min(max(y, r), SIZE - 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r or (
        r <= x <= SIZE - 1 - r or r <= y <= SIZE - 1 - r)


def curve_y(x: float) -> float:
    for (x0, y0), (x1, y1) in zip(ANCHORS, ANCHORS[1:]):
        if x0 <= x <= x1:
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return ANCHORS[0][1] if x < ANCHORS[0][0] else ANCHORS[-1][1]


def pixel(x: int, y: int) -> tuple:
    if not in_tile(x, y):
        return (0, 0, 0, 0)
    fx, fy = ANCHORS[-1]
    if (x - fx) ** 2 + (y - fy) ** 2 <= 9:          # end dot
        return DOT
    if ANCHORS[0][0] <= x <= ANCHORS[-1][0] and abs(y - curve_y(x)) <= 1.6:
        return LINE
    return BG


def build_ico() -> bytes:
    rows = []
    for y in range(SIZE - 1, -1, -1):               # BMP rows are bottom-up
        row = bytearray()
        for x in range(SIZE):
            r, g, b, a = pixel(x, y)
            row += bytes((b, g, r, a))              # BGRA
        rows.append(bytes(row))
    xor_data = b"".join(rows)
    and_row = b"\x00" * (SIZE // 8)                 # alpha channel rules
    and_data = and_row * SIZE
    header = struct.pack("<IiiHHIIiiII", 40, SIZE, SIZE * 2, 1, 32, 0,
                         len(xor_data) + len(and_data), 0, 0, 0, 0)
    image = header + xor_data + and_data
    icondir = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", SIZE, SIZE, 0, 0, 1, 32, len(image), 22)
    return icondir + entry + image


def main() -> None:
    out = Path(__file__).parent / "icon.ico"
    out.write_bytes(build_ico())
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
