"""Generate PWA PNG icons with the Python standard library.

Run from the project root:
    python scripts/make_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "icons"


def _blend(dst, src, alpha: float):
    return (
        int(dst[0] * (1 - alpha) + src[0] * alpha),
        int(dst[1] * (1 - alpha) + src[1] * alpha),
        int(dst[2] * (1 - alpha) + src[2] * alpha),
    )


def _line_distance(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    qx, qy = x1 + t * dx, y1 + t * dy
    return ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5


def _in_rounded_rect(x: float, y: float, w: float, h: float, r: float) -> bool:
    if x < r and y < r:
        return (x - r) ** 2 + (y - r) ** 2 <= r * r
    if x > w - r and y < r:
        return (x - (w - r)) ** 2 + (y - r) ** 2 <= r * r
    if x < r and y > h - r:
        return (x - r) ** 2 + (y - (h - r)) ** 2 <= r * r
    if x > w - r and y > h - r:
        return (x - (w - r)) ** 2 + (y - (h - r)) ** 2 <= r * r
    return True


def make_icon(size: int) -> bytes:
    scale = 2
    width = size * scale
    pixels = [[(0, 0, 0, 0) for _ in range(width)] for _ in range(width)]
    radius = width * 0.22

    # Background gradient
    for y in range(width):
        for x in range(width):
            if not _in_rounded_rect(x + 0.5, y + 0.5, width, width, radius):
                continue
            t = (x / width) * 0.55 + (y / width) * 0.45
            if t < 0.55:
                local = t / 0.55
                color = _blend((96, 165, 250), (37, 99, 235), local)
            else:
                local = (t - 0.55) / 0.45
                color = _blend((37, 99, 235), (30, 64, 175), local)
            pixels[y][x] = (*color, 255)

    # Parcel glyph, matching web/icons/icon.svg
    lines = [
        (0.230, 0.371, 0.500, 0.219),
        (0.500, 0.219, 0.770, 0.371),
        (0.770, 0.371, 0.770, 0.629),
        (0.770, 0.629, 0.500, 0.781),
        (0.500, 0.781, 0.230, 0.629),
        (0.230, 0.629, 0.230, 0.371),
        (0.230, 0.371, 0.500, 0.523),
        (0.500, 0.523, 0.770, 0.371),
        (0.500, 0.523, 0.500, 0.781),
    ]
    thickness = width * 0.035
    for y in range(width):
        for x in range(width):
            px, py = x + 0.5, y + 0.5
            for x1, y1, x2, y2 in lines:
                if _line_distance(px, py, x1 * width, y1 * width, x2 * width, y2 * width) <= thickness / 2:
                    current = pixels[y][x]
                    pixels[y][x] = (255, 255, 255, 245)
                    break

    # Downsample by simple averaging (2x2 blocks).
    out = []
    for y in range(size):
        row = []
        for x in range(size):
            total = [0, 0, 0, 0]
            for dy in range(scale):
                for dx in range(scale):
                    px = pixels[y * scale + dy][x * scale + dx]
                    for i in range(4):
                        total[i] += px[i]
            row.append(tuple(value // (scale * scale) for value in total))
        out.append(row)
    return _encode_png(out)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _encode_png(rows) -> bytes:
    height = len(rows)
    width = len(rows[0])
    raw = b"".join(b"\x00" + bytes(channel for pixel in row for channel in pixel) for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size, name in [(192, "icon-192.png"), (512, "icon-512.png"), (180, "apple-touch-icon.png")]:
        (OUT / name).write_bytes(make_icon(size))
        print("wrote", OUT / name)


if __name__ == "__main__":
    main()
