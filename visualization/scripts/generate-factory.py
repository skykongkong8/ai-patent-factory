#!/usr/bin/env python3
"""Generate transparent game sprites from the existing Patkong drawing primitives.

Only public brand artwork is used. No image or private workflow files are read.
Run from any directory; output is deterministic, with no third-party packages.
"""
import importlib.util
import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'visualization/client/public/assets/themes/patkong-factory'
spec = importlib.util.spec_from_file_location('mascot', ROOT / 'assets/mascot/generate_mascot.py')
mascot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mascot)


def png(path, width, height, pixels):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    rows = b''.join(b'\0' + pixels[y * width * 4:(y + 1) * width * 4] for y in range(height))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
                     + chunk(b'IDAT', zlib.compress(rows, 9)) + chunk(b'IEND', b''))


def canvas(w, h):
    pixels = bytearray(w * h * 4)
    def rect(x, y, rw, rh, color):
        rgba = bytes.fromhex(color) + b'\xff'
        for yy in range(max(0, y), min(h, y + rh)):
            for xx in range(max(0, x), min(w, x + rw)):
                pixels[(yy * w + xx) * 4:(yy * w + xx + 1) * 4] = rgba
    return pixels, rect


def generate():
    OUT.mkdir(parents=True, exist_ok=True)
    colors = {'blue': '6fd8ff', 'yellow': 'ffc94b', 'red': 'ff9d8a', 'black': 'a9b8d0', 'purple': 'cba9f5'}
    for color, accent in colors.items():
        pixels = bytearray(1152 * 384 * 4)
        for frame in range(12):
            cv = mascot.Grid()
            mascot.draw_robot(cv, [0, 1, 6, 2, 3, 4, 28, 29, 30, 31, 32, 33][frame])
            step = round(math.sin((frame % 6) * math.pi / 3) * 2) if frame >= 6 else 0
            cv.capsule(35, 43, 30 + step, 54, 3, mascot.OUT)
            cv.capsule(35, 43, 30 + step, 54, 2, mascot.BODY_S)
            cv.capsule(64, 43, 69 - step, 54, 3, mascot.OUT)
            cv.capsule(64, 43, 69 - step, 54, 2, mascot.BODY_S)
            cv.rect(39 + step, 75, 46 + step, 78, mascot.OUT)
            cv.rect(54 - step, 75, 61 - step, 78, mascot.OUT)
            for y in range(82):
                for x in range(24, 76):
                    index = cv.px[y * mascot.W + x]
                    if index == 0:
                        continue
                    rgb = bytes.fromhex(accent) if index == mascot.MINT else bytes(mascot.PALETTE[index])
                    for dy in range(2):
                        for dx in range(2):
                            px = frame % 6 * 192 + (x - 24) * 2 + 44 + dx
                            py = frame // 6 * 192 + y * 2 + 18 + dy
                            offset = (py * 1152 + px) * 4
                            pixels[offset:offset + 4] = rgb + b'\xff'
        png(OUT / f'patkong-{color}.png', 1152, 384, pixels)

    floor, rect = canvas(64, 64)
    rect(0, 0, 64, 64, '17365e')
    rect(0, 0, 1, 64, '244a75'); rect(0, 0, 64, 1, '244a75')
    rect(31, 31, 3, 3, '315b86')
    png(OUT / 'floor.png', 64, 64, floor)

    ids = ['library', 'castle', 'watchtower', 'arena', 'alchemist', 'forge', 'chapel', 'tavern']
    for n, name in enumerate(ids):
        p, rect = canvas(256, 224)
        accent = list(colors.values())[n % 5]
        rect(14, 199, 230, 12, '102a4b')
        rect(20, 99, 216, 102, '0b1f3a'); rect(24, 103, 208, 94, '31506f')
        rect(24, 103, 208, 8, accent); rect(34, 119, 188, 64, '17365e')
        rect(32, 74, 192, 28, '0b1f3a'); rect(36, 78, 184, 20, '476786')
        rect(36, 78, 184, 4, '93a9bf')
        rect(64, 45, 128, 29, '0b1f3a'); rect(68, 49, 120, 21, '244a75')
        # Eight window/workbench motifs distinguish activity rooms at a glance.
        for i in range(4):
            x = 43 + i * 44
            rect(x, 126, 34, 44, '0b1f3a')
            rect(x + 3, 129, 28, 38, accent if i == n % 4 else 'a9e8d8')
            rect(x + 6, 137, 22, 3, '31506f'); rect(x + 6, 147, 22, 3, '31506f')
        rect(111, 166, 34, 32, '0b1f3a'); rect(115, 170, 26, 28, '5e7791')
        rect(20, 190, 216, 9, 'ffc94b')
        for x in range(24, 232, 18):
            rect(x, 190, 8, 9, '17365e')
        # Roof identity: books, bulb, inspection eye, terminal, gear, pen, archive, cup.
        if name == 'library':
            for i in range(4): rect(91 + i * 17, 27 - i % 2 * 6, 12, 36, accent)
        elif name == 'castle':
            rect(117, 13, 22, 28, 'ffe066'); rect(122, 41, 12, 12, '93a9bf')
            rect(108, 5, 4, 8, 'ffe066'); rect(145, 5, 4, 8, 'ffe066')
        elif name == 'watchtower':
            rect(97, 22, 62, 27, 'a9e8d8'); rect(119, 19, 18, 33, '17365e'); rect(124, 27, 8, 14, accent)
        elif name == 'arena':
            rect(99, 16, 58, 39, '0b1f3a'); rect(104, 21, 48, 29, '6fd8ff')
            rect(110, 29, 8, 3, '17365e'); rect(116, 32, 8, 3, '17365e'); rect(130, 40, 14, 3, '17365e')
        elif name == 'alchemist':
            rect(119, 13, 18, 42, accent); rect(107, 25, 42, 18, accent); rect(121, 27, 14, 14, '17365e')
        elif name == 'forge':
            rect(100, 31, 57, 13, 'ffc94b'); rect(145, 31, 12, 13, 'ff9d8a'); rect(95, 34, 6, 7, 'f3f6fa')
        elif name == 'chapel':
            for i in range(3):
                rect(102, 12 + i * 14, 52, 11, 'a9b8d0'); rect(122, 15 + i * 14, 11, 3, '17365e')
        else:
            rect(108, 19, 35, 32, 'ffc94b'); rect(143, 24, 13, 18, 'ffc94b'); rect(148, 28, 4, 10, '17365e')
        png(OUT / f'{name}.png', 256, 224, p)
    print('Generated Patkong sprites, eight factory buildings and blueprint floor.')


if __name__ == '__main__':
    generate()
