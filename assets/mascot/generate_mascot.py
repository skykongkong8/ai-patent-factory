#!/usr/bin/env python3
"""Generate Patkong (팥콩), the AI Patent Factory mascot.

Patkong is a bean-shaped factory bot working a document conveyor on a
blueprint backdrop: evidence rolls in, Patkong scans it, the idea bulb
lights up, and the page gets a red hash-carved seal before the belt
rolls on — the pipeline, the gates, and hash-binding in one loop.

Deterministic and CPython-stdlib-only (the GIF89a/LZW and PNG encoders
live in this file), so the committed artifacts regenerate byte-for-byte:

    python3 assets/mascot/generate_mascot.py

Outputs (next to this script by default):
    patkong.gif   36-frame looping animation, 512x384
    patkong.png   static poster frame, 512x384
"""

from __future__ import annotations

import argparse
import hashlib
import math
import struct
import zlib
from pathlib import Path

W, H = 128, 96          # logical pixel-art canvas
SCALE = 4               # nearest-neighbour upscale for the shipped files
FRAME_COUNT = 36
POSTER_FRAME = 28       # happy Patkong, lit bulb, sealed page

# --- palette (index -> RGB) -------------------------------------------------

PALETTE = [
    (0x17, 0x36, 0x5E),  # 0  BG        blueprint navy
    (0x1D, 0x42, 0x71),  # 1  GRID      faint blueprint grid
    (0x2E, 0x5C, 0x99),  # 2  SKETCH    wall doodles / corner ticks
    (0x10, 0x2A, 0x4B),  # 3  FLOOR
    (0x0B, 0x1F, 0x3A),  # 4  DARK      darkest lines / shadows
    (0x31, 0x50, 0x6F),  # 5  HOUS      conveyor housing
    (0x3E, 0x5F, 0x82),  # 6  HOUS_HI   housing top bevel
    (0x93, 0xA9, 0xBF),  # 7  BELT      belt band
    (0x5E, 0x77, 0x91),  # 8  BELT_D    belt tread ticks
    (0x46, 0x60, 0x7A),  # 9  BELT_S    shadow under the belt band
    (0x2B, 0x3B, 0x55),  # 10 OUT       robot outline / eyes / dark detail
    (0xF3, 0xF6, 0xFA),  # 11 BODY
    (0xC7, 0xD3, 0xE1),  # 12 BODY_S    body shading
    (0xA9, 0xE8, 0xD8),  # 13 MINT      belly panel
    (0x57, 0xBC, 0xA6),  # 14 MINT_D    panel border / glyph
    (0xFF, 0xC9, 0x4B),  # 15 HAT       safety hat
    (0xDD, 0x9F, 0x27),  # 16 HAT_D     hat shading
    (0xFF, 0x9D, 0x8A),  # 17 CHEEK
    (0xD6, 0x45, 0x3D),  # 18 SEAL      red dojang seal
    (0xF0, 0x8A, 0x7E),  # 19 SEAL_L    seal carving / stamp rubber line
    (0xD8, 0xDF, 0xE9),  # 20 PAPER_S   page edges / thickness
    (0x9A, 0xA8, 0xBB),  # 21 TEXT      page text lines
    (0xFF, 0xFF, 0xFF),  # 22 WHITE
    (0x9F, 0xAC, 0xBD),  # 23 BULB_OFF
    (0xFF, 0xE0, 0x66),  # 24 BULB_ON
    (0xFF, 0xF3, 0xB3),  # 25 GLOW
    (0x6F, 0xD8, 0xFF),  # 26 SCAN      evidence-scan beam
    (0xE8, 0x5D, 0x5D),  # 27 HANDLE    stamp handle
]
(BG, GRID, SKETCH, FLOOR, DARK, HOUS, HOUS_HI, BELT, BELT_D, BELT_S, OUT,
 BODY, BODY_S, MINT, MINT_D, HAT, HAT_D, CHEEK, SEAL, SEAL_L, PAPER_S,
 TEXT, WHITE, BULB_OFF, BULB_ON, GLOW, SCAN, HANDLE) = range(len(PALETTE))
PALETTE32 = PALETTE + [(0, 0, 0)] * (32 - len(PALETTE))

# --- timeline ---------------------------------------------------------------
# f0-f13  belt runs: sealed page exits left, fresh page arrives (7 px/frame)
# f14-f19 belt stops at Patkong: eye-beam scans the page top to bottom
# f20     the idea bulb switches on (beat)
# f21-f27 stamp raise -> slam -> lift: the hash seal is bound
# f28-f35 satisfaction bob while the bulb fades; loop closes seamlessly

BELT_FRAMES, BELT_SPEED = 14, 7
CYCLE = BELT_FRAMES * BELT_SPEED            # 98 px; page spacing = one cycle
PAGE_STOP_X = 70                            # page left edge when belt halts

BOB = ([0, 0, 1, 1] * 3 + [0, 0]            # idle bob while the belt runs
       + [1] * 6                            # lean in to scan
       + [0] + [-1, -1, -1]                 # straighten, stretch for the raise
       + [1, 1] + [0, 0]                    # squash on impact, recover
       + [1, 1, 0, 0, 1, 1, 0, 0])          # happy bob (ends at 0 = frame 0)
EYES = (["fwd"] * 6 + ["blink"] + ["fwd"] * 7 + ["scan"] * 6 + ["fwd"]
        + ["down"] * 3 + ["shut"] * 2 + ["down"] * 2 + ["happy"] * 6
        + ["fwd"] * 2)
MOUTH = ["smile"] * 24 + ["o"] * 2 + ["wide"] * 8 + ["smile"] * 2
BULB = (["off"] * 17 + ["on", "off", "on"] + ["ray"] * 5 + ["burst"]
        + ["ray"] * 5 + ["on", "dim"] + ["off"] * 3)
HAND_REST = (67, 49)
RIGHT_HAND = ([HAND_REST] * 21
              + [(72, 42), (76, 36), (78, 32), (78, 52), (78, 52), (76, 42)]
              + [HAND_REST] * 9)
SPARK = {24: 1, 25: 2, 26: 3}               # impact sparkle stage
DELAYS = ([8] * 14 + [8] * 6 + [14, 7, 7, 10, 5, 16, 8, 8] + [8] * 7 + [10])

for table in (BOB, EYES, MOUTH, BULB, RIGHT_HAND, DELAYS):
    assert len(table) == FRAME_COUNT

# --- tiny raster helpers ----------------------------------------------------


class Grid:
    """A W×H byte canvas; doubles as a 0/1 mask."""

    __slots__ = ("px",)

    def __init__(self, fill: int = 0) -> None:
        self.px = bytearray([fill]) * (W * H)

    def set(self, x: int, y: int, c: int) -> None:
        if 0 <= x < W and 0 <= y < H:
            self.px[y * W + x] = c

    def hline(self, x0: int, x1: int, y: int, c: int) -> None:
        for x in range(x0, x1 + 1):
            self.set(x, y, c)

    def vline(self, x: int, y0: int, y1: int, c: int) -> None:
        for y in range(y0, y1 + 1):
            self.set(x, y, c)

    def rect(self, x0: int, y0: int, x1: int, y1: int, c: int) -> None:
        for y in range(y0, y1 + 1):
            self.hline(x0, x1, y, c)

    def disc(self, cx: int, cy: int, r: int, c: int) -> None:
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if dx * dx + dy * dy <= r * r:
                    self.set(cx + dx, cy + dy, c)

    def capsule(self, x0: int, y0: int, x1: int, y1: int, r: int, c: int) -> None:
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for i in range(steps + 1):
            t = i / steps
            self.disc(int(x0 + (x1 - x0) * t + 0.5),
                      int(y0 + (y1 - y0) * t + 0.5), r, c)


def paint(cv: Grid, m: Grid, fill: int, outline: int | None = None) -> None:
    """Fill a mask onto the canvas, then trace a 1px inner outline."""
    for i, on in enumerate(m.px):
        if on:
            cv.px[i] = fill
    if outline is None:
        return
    for y in range(H):
        for x in range(W):
            if not m.px[y * W + x]:
                continue
            edge = x in (0, W - 1) or y in (0, H - 1)
            if not edge:
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if not m.px[(y + dy) * W + (x + dx)]:
                            edge = True
                            break
                    if edge:
                        break
            if edge:
                cv.px[y * W + x] = outline


def shade_right(cv: Grid, m: Grid, color: int) -> None:
    """1px inner shading down the right rim of a painted mask."""
    for y in range(H):
        xs = [x for x in range(W) if m.px[y * W + x]]
        if len(xs) >= 4:
            cv.set(xs[-2], y, color)


# --- scene painters ---------------------------------------------------------


def draw_background(cv: Grid) -> None:
    for x in range(0, W, 8):
        cv.vline(x, 0, H - 1, GRID)
    for y in range(0, H, 8):
        cv.hline(0, W - 1, y, GRID)
    # blueprint sheet border + top corner ticks
    cv.hline(2, 125, 2, GRID)
    cv.hline(2, 125, 93, GRID)
    cv.vline(2, 2, 93, GRID)
    cv.vline(125, 2, 93, GRID)
    for cx in (4, 123):
        cv.hline(min(cx, cx if cx == 4 else 120), max(cx, cx if cx == 4 else 123), 4, SKETCH)
        cv.vline(cx, 4, 7, SKETCH)
    cv.hline(4, 7, 4, SKETCH)
    cv.hline(120, 123, 4, SKETCH)
    # doodle: gear
    for k in range(24):
        a = k * math.tau / 24
        cv.set(int(16 + 6 * math.cos(a) + 0.5), int(16 + 6 * math.sin(a) + 0.5), SKETCH)
    for k in range(8):
        a = k * math.tau / 8
        cv.set(int(16 + 8 * math.cos(a) + 0.5), int(16 + 8 * math.sin(a) + 0.5), SKETCH)
    cv.set(16, 16, SKETCH)
    # doodle: sketched bulb
    for k in range(16):
        a = k * math.tau / 16
        cv.set(int(109 + 4 * math.cos(a) + 0.5), int(15 + 4 * math.sin(a) + 0.5), SKETCH)
    cv.hline(107, 111, 21, SKETCH)
    cv.set(109, 8, SKETCH)
    cv.set(102, 15, SKETCH)
    cv.set(116, 15, SKETCH)
    # doodle: dimension line + plus marks
    cv.hline(8, 26, 52, SKETCH)
    cv.vline(8, 50, 54, SKETCH)
    cv.vline(26, 50, 54, SKETCH)
    for px, py in ((88, 8), (24, 40)):
        cv.set(px, py, SKETCH)
        cv.set(px - 1, py, SKETCH)
        cv.set(px + 1, py, SKETCH)
        cv.set(px, py - 1, SKETCH)
        cv.set(px, py + 1, SKETCH)
    # floor
    cv.rect(0, 84, W - 1, H - 1, FLOOR)


def body_mask(bob: int) -> Grid:
    m = Grid()
    widths = [14, 18, 20, 22, 24, 24]
    for y in range(24, 67):
        if y <= 29:
            w = widths[y - 24]
        else:
            w = min(32, 24 + int((y - 29) * 8 / 29 + 0.5))
            w -= w % 2
        m.hline(50 - w // 2, 50 - w // 2 + w - 1, y + bob, 1)
    return m


def draw_robot(cv: Grid, f: int) -> None:
    bob = BOB[f]
    body = body_mask(bob)
    paint(cv, body, BODY, OUT)
    shade_right(cv, body, BODY_S)
    cv.hline(39, 60, 29 + bob, BODY_S)                     # hat brim shadow
    cv.set(41, 29 + bob, WHITE)
    cv.set(42, 29 + bob, WHITE)
    cv.set(41, 30 + bob, WHITE)
    # belly panel + tiny bulb glyph (brand echo)
    cv.rect(45, 50 + bob, 56, 60 + bob, MINT)
    cv.hline(45, 56, 49 + bob, MINT_D)
    cv.hline(45, 56, 61 + bob, MINT_D)
    cv.vline(44, 50 + bob, 60 + bob, MINT_D)
    cv.vline(57, 50 + bob, 60 + bob, MINT_D)
    for gy in (52, 54, 56):                                # report lines on screen
        cv.hline(47, 54, gy + bob, MINT_D)
    # safety hat: dome + centre ridge, separately outlined brim
    dome = Grid()
    for i, w in enumerate([10, 16, 20, 22, 24, 24, 26, 26]):
        dome.hline(50 - w // 2, 50 - w // 2 + w - 1, 18 + i + bob, 1)
    paint(cv, dome, HAT, OUT)
    shade_right(cv, dome, HAT_D)
    cv.rect(49, 19 + bob, 51, 22 + bob, BULB_ON)           # hard-hat ridge
    brim = Grid()
    brim.rect(33, 26 + bob, 66, 28 + bob, 1)
    paint(cv, brim, HAT, OUT)
    cv.set(43, 20 + bob, WHITE)
    cv.set(44, 20 + bob, WHITE)
    cv.set(43, 21 + bob, WHITE)
    draw_bulb(cv, f, bob)
    draw_face(cv, f, bob)


def draw_bulb(cv: Grid, f: int, bob: int) -> None:
    mode = BULB[f]
    cv.vline(50, 15 + bob, 17 + bob, BODY_S)               # antenna wire
    glass = Grid()                                         # pear-shaped glass
    glass.disc(50, 7 + bob, 4, 1)
    glass.rect(48, 10 + bob, 52, 12 + bob, 1)
    fill = {"off": BULB_OFF, "on": BULB_ON, "ray": BULB_ON,
            "burst": BULB_ON, "dim": GLOW}[mode]
    paint(cv, glass, fill, OUT)
    if mode == "off":
        for fx, fy in ((48, 8), (49, 7), (50, 8), (51, 7)):
            cv.set(fx, fy + bob, OUT)                      # cold filament
    else:
        cv.rect(48, 5 + bob, 51, 7 + bob, WHITE)           # hot core
    if mode in ("ray", "burst"):
        for rx, ry in ((50, 1), (45, 2), (55, 2), (43, 7), (57, 7)):
            cv.set(rx, ry + bob, GLOW)
    if mode == "burst":
        for rx, ry in ((50, 0), (41, 7), (59, 7), (44, 1), (56, 1)):
            cv.set(rx, ry + bob, GLOW)
    cv.rect(48, 13 + bob, 52, 14 + bob, BODY_S)            # screw base
    cv.hline(48, 52, 14 + bob, BELT_D)


def draw_face(cv: Grid, f: int, bob: int) -> None:
    eyes, mouth = EYES[f], MOUTH[f]
    for ex in (44, 54):
        if eyes == "fwd":
            cv.rect(ex, 33 + bob, ex + 1, 34 + bob, OUT)
        elif eyes in ("blink", "shut"):
            cv.rect(ex, 34 + bob, ex + 1, 34 + bob, OUT)
        elif eyes == "down":
            cv.rect(ex + 1, 34 + bob, ex + 2, 35 + bob, OUT)
        elif eyes == "scan":
            cv.rect(ex + 1, 34 + bob, ex + 2, 35 + bob, SCAN)
        elif eyes == "happy":
            cv.set(ex - 1, 34 + bob, OUT)
            cv.set(ex, 33 + bob, OUT)
            cv.set(ex + 1, 33 + bob, OUT)
            cv.set(ex + 2, 34 + bob, OUT)
    cv.rect(41, 36 + bob, 42, 36 + bob, CHEEK)
    cv.rect(57, 36 + bob, 58, 36 + bob, CHEEK)
    if mouth == "smile":
        cv.hline(48, 51, 38 + bob, OUT)
        cv.set(47, 37 + bob, OUT)
        cv.set(52, 37 + bob, OUT)
    elif mouth == "wide":
        cv.hline(46, 53, 39 + bob, OUT)
        cv.set(45, 38 + bob, OUT)
        cv.set(54, 38 + bob, OUT)
    else:  # effort "o"
        cv.rect(49, 38 + bob, 50, 39 + bob, OUT)


def draw_conveyor(cv: Grid, f: int) -> None:
    d = belt_travel(f)
    cv.rect(0, 63, W - 1, 65, BELT)
    toff = (2 - d) % 14
    for i in range(-1, 11):
        x = i * 14 + toff
        cv.vline(x, 63, 65, BELT_D)
    cv.hline(0, W - 1, 66, BELT_S)
    cv.hline(0, W - 1, 67, HOUS_HI)
    cv.rect(0, 68, W - 1, 76, HOUS)
    cv.hline(4, 123, 72, DARK)                             # front slot line
    cv.hline(0, W - 1, 77, DARK)
    for lx in (16, 104):
        cv.rect(lx, 78, lx + 4, 83, HOUS)
        cv.vline(lx + 4, 78, 83, DARK)
        cv.hline(lx - 2, lx + 6, 84, DARK)                 # leg shadow


PAGE_SKEW = {56: 3, 57: 2, 58: 2, 59: 1, 60: 1, 61: 0, 62: 0}


def draw_page(cv: Grid, xl: int, sealed: bool) -> None:
    if xl < -20 or xl > 130:
        return
    for y, off in PAGE_SKEW.items():
        a, b = xl + off, xl + off + 15
        for x in range(a, b + 1):
            if y == 62 or x in (a, b):
                cv.set(x, y, PAPER_S)
            else:
                cv.set(x, y, WHITE)
    for ty in (58, 60):
        off = PAGE_SKEW[ty]
        cv.hline(xl + off + 2, xl + off + 9, ty, TEXT)
    if sealed:                                             # square dojang, '#' carved
        cv.rect(xl + 6, 57, xl + 10, 61, SEAL)
        cv.vline(xl + 7, 57, 61, WHITE)
        cv.vline(xl + 9, 57, 61, WHITE)
        cv.set(xl + 8, 58, WHITE)
        cv.set(xl + 8, 60, WHITE)


def draw_left_arm(cv: Grid, f: int) -> None:
    bob = BOB[f]
    m = Grid()
    m.capsule(34, 49 + bob, 30, 59 + bob, 2, 1)
    paint(cv, m, BODY, OUT)


def draw_right_arm(cv: Grid, f: int) -> None:
    bob = BOB[f]
    hx, hy = RIGHT_HAND[f]
    hy += bob
    arm = Grid()
    arm.capsule(63, 46 + bob, hx, hy, 2, 1)
    paint(cv, arm, BODY, OUT)
    tool = Grid()                                          # the stamp
    tool.rect(hx - 4, hy - 8, hx + 4, hy - 6, 1)           # knob bar
    tool.rect(hx - 2, hy - 5, hx + 2, hy + 2, 1)           # neck
    tool.rect(hx - 6, hy + 3, hx + 6, hy + 6, 1)           # base
    paint(cv, tool, HANDLE, OUT)
    cv.rect(hx - 6, hy + 4, hx + 6, hy + 6, OUT)           # rubber block
    cv.hline(hx - 5, hx + 5, hy + 3, SEAL_L)
    hand = Grid()
    hand.disc(hx, hy, 3, 1)
    paint(cv, hand, BODY, OUT)


def draw_fx(cv: Grid, f: int) -> None:
    bob = BOB[f]
    if 14 <= f <= 19:                                      # evidence scan
        x0, y0, x1, y1 = 58, 38 + bob, 77, 57
        n = max(x1 - x0, y1 - y0)
        for i in range(n + 1):
            t = i / n
            x = int(x0 + (x1 - x0) * t + 0.5)
            y = int(y0 + (y1 - y0) * t + 0.5)
            cv.set(x, y, WHITE if (i + f) % 4 == 0 else SCAN)
            cv.set(x, y + 1, SCAN)
        row = 56 + (f - 14)
        off = PAGE_SKEW[row]
        cv.hline(PAGE_STOP_X + off + 1, PAGE_STOP_X + off + 14, row, SCAN)
        cv.set(PAGE_STOP_X + off + 14, row, WHITE)
    stage = SPARK.get(f)
    points = ((70, 45), (86, 46), (78, 43))
    if stage == 1:
        for sx, sy in points:
            cv.set(sx, sy, WHITE)
        for mx in (73, 83):                                # slam motion lines
            cv.vline(mx, 43, 46, WHITE)
        cv.set(71, 56, WHITE)                              # impact puffs
        cv.set(90, 56, WHITE)
    elif stage == 2:
        for sx, sy in points:
            cv.set(sx - 1, sy, WHITE)
            cv.set(sx + 1, sy, WHITE)
            cv.set(sx, sy - 1, WHITE)
            cv.set(sx, sy + 1, WHITE)
            cv.set(sx, sy, GLOW)
    elif stage == 3:
        cv.set(70, 45, GLOW)
        cv.set(86, 46, GLOW)


def belt_travel(f: int) -> int:
    return BELT_SPEED * (f + 1) if f < BELT_FRAMES else CYCLE


def render_frame(f: int) -> bytearray:
    cv = Grid(BG)
    draw_background(cv)
    draw_robot(cv, f)
    draw_conveyor(cv, f)
    d = belt_travel(f)
    draw_page(cv, PAGE_STOP_X - d, True)                   # sealed page leaving
    draw_page(cv, PAGE_STOP_X + CYCLE - d, f >= 24)        # fresh page arriving
    draw_left_arm(cv, f)
    draw_right_arm(cv, f)
    draw_fx(cv, f)
    return cv.px


# --- encoders (GIF89a + PNG, stdlib only) -----------------------------------


def lzw_encode(data: bytes, mcs: int) -> bytes:
    # giflib ordering: the width check runs AFTER each code is written, when
    # the encoder's next-slot counter equals the decoder's — bumping anywhere
    # else desynchronises the two by one code.
    clear, eoi = 1 << mcs, (1 << mcs) + 1
    out = bytearray()
    buf = nbits = 0
    size = mcs + 1
    max1 = 1 << size
    nxt = eoi + 1

    def write(code: int) -> None:
        nonlocal buf, nbits, size, max1
        buf |= code << nbits
        nbits += size
        while nbits >= 8:
            out.append(buf & 0xFF)
            buf >>= 8
            nbits -= 8
        if nxt >= max1 and size < 12:
            size += 1
            max1 = 1 << size

    table = {bytes((i,)): i for i in range(clear)}
    write(clear)
    w = b""
    for ch in data:
        wc = w + bytes((ch,))
        if wc in table:
            w = wc
            continue
        write(table[w])
        if nxt >= 4095:
            write(clear)
            table = {bytes((i,)): i for i in range(clear)}
            nxt = eoi + 1
            size = mcs + 1
            max1 = 1 << size
        else:
            table[wc] = nxt
            nxt += 1
        w = wc[-1:]
    if w:
        write(table[w])
    write(eoi)
    if nbits:
        out.append(buf & 0xFF)
    return bytes(out)


def scale_indices(px: bytearray, s: int) -> bytearray:
    out = bytearray(W * s * H * s)
    for y in range(H):
        row = bytearray()
        for b in px[y * W:(y + 1) * W]:
            row += bytes((b,)) * s
        for k in range(s):
            base = (y * s + k) * W * s
            out[base:base + W * s] = row
    return out


def write_gif(path: Path, frames: list[bytearray], s: int) -> None:
    sw, sh = W * s, H * s
    o = bytearray()
    o += b"GIF89a"
    o += struct.pack("<HH", sw, sh)
    o += bytes((0xF4, 0x00, 0x00))                         # 32-colour GCT
    for r, g, b in PALETTE32:
        o += bytes((r, g, b))
    o += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00"    # loop forever
    for fr, delay in zip(frames, DELAYS):
        o += b"\x21\xF9\x04\x04" + struct.pack("<H", delay) + b"\x00\x00"
        o += b"\x2C" + struct.pack("<HHHH", 0, 0, sw, sh) + b"\x00"
        o.append(5)                                        # LZW min code size
        data = lzw_encode(bytes(scale_indices(fr, s)), 5)
        for i in range(0, len(data), 255):
            chunk = data[i:i + 255]
            o.append(len(chunk))
            o += chunk
        o.append(0)
    o.append(0x3B)
    path.write_bytes(bytes(o))


def to_rgb(idx: bytearray, n: int) -> bytearray:
    lut = [bytes(c) for c in PALETTE32]
    out = bytearray()
    for i in range(n):
        out += lut[idx[i]]
    return out


def write_png(path: Path, w: int, h: int, rgb: bytearray) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgb[y * w * 3:(y + 1) * w * 3]
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
                     + chunk(b"IEND", b""))


def write_sheet(path: Path, frames: list[bytearray], s: int = 2, cols: int = 6) -> None:
    gap, gc = 3, bytes((0x0B, 0x1F, 0x3A))
    rows = (len(frames) + cols - 1) // cols
    fw, fh = W * s, H * s
    sw = cols * fw + gap * (cols + 1)
    sh = rows * fh + gap * (rows + 1)
    buf = bytearray(gc * (sw * sh))
    for n, fr in enumerate(frames):
        r, c = divmod(n, cols)
        ox, oy = gap + c * (fw + gap), gap + r * (fh + gap)
        rgb = to_rgb(scale_indices(fr, s), fw * fh)
        for y in range(fh):
            dst = ((oy + y) * sw + ox) * 3
            buf[dst:dst + fw * 3] = rgb[y * fw * 3:(y + 1) * fw * 3]
    write_png(path, sw, sh, buf)


# --- verification -----------------------------------------------------------


def check_gif(path: Path) -> None:
    b = path.read_bytes()
    assert b[:6] == b"GIF89a", "bad signature"
    sw, sh = struct.unpack("<HH", b[6:10])
    assert b[10] & 0x80, "missing global colour table"
    p = 13 + 3 * (2 << (b[10] & 7))
    frames, delays, loop = 0, [], False
    while True:
        t = b[p]
        if t == 0x3B:
            break
        if t == 0x21:
            label = b[p + 1]
            p += 2
            if label == 0xF9:
                delays.append(struct.unpack("<H", b[p + 2:p + 4])[0])
            if label == 0xFF and b[p + 1:p + 12] == b"NETSCAPE2.0":
                loop = True
            while b[p]:
                p += b[p] + 1
            p += 1
        elif t == 0x2C:
            frames += 1
            p += 11                                        # descriptor + LZW mcs byte
            while b[p]:
                p += b[p] + 1
            p += 1
        else:
            raise ValueError(f"unexpected block 0x{t:02x} at offset {p}")
    assert frames == FRAME_COUNT, f"expected {FRAME_COUNT} frames, found {frames}"
    assert delays == DELAYS, "frame delays do not match the timeline"
    assert loop, "missing NETSCAPE loop extension"
    total = sum(delays) / 100
    print(f"  {path.name}: {sw}x{sh}, {frames} frames, {total:.2f}s loop, "
          f"{len(b):,} bytes, sha256={hashlib.sha256(b).hexdigest()[:16]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent,
                    help="output directory (default: this script's directory)")
    ap.add_argument("--scale", type=int, default=SCALE)
    ap.add_argument("--debug", type=Path, default=None,
                    help="also write a frame contact sheet + key frames here")
    args = ap.parse_args()

    frames = [render_frame(f) for f in range(FRAME_COUNT)]
    args.out.mkdir(parents=True, exist_ok=True)

    gif_path = args.out / "patkong.gif"
    write_gif(gif_path, frames, args.scale)
    poster = scale_indices(frames[POSTER_FRAME], args.scale)
    png_path = args.out / "patkong.png"
    write_png(png_path, W * args.scale, H * args.scale,
              to_rgb(poster, W * args.scale * H * args.scale))

    print("patkong generated:")
    check_gif(gif_path)
    png_bytes = png_path.read_bytes()
    print(f"  {png_path.name}: poster frame {POSTER_FRAME}, {len(png_bytes):,} bytes, "
          f"sha256={hashlib.sha256(png_bytes).hexdigest()[:16]}")

    if args.debug:
        args.debug.mkdir(parents=True, exist_ok=True)
        write_sheet(args.debug / "sheet.png", frames)
        for k in (6, 17, 23, 25, 28):
            fr = scale_indices(frames[k], 4)
            write_png(args.debug / f"key_{k:02d}.png", W * 4, H * 4,
                      to_rgb(fr, W * 4 * H * 4))
        print(f"  debug frames written to {args.debug}")


if __name__ == "__main__":
    main()
