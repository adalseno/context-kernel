#!/usr/bin/env python3
"""Genera docs/ck-tour.gif — la demo "the two doors" del context-kernel.

NON è una registrazione: è un rendering deterministico (Pillow, stdlib+PIL) di
una sessione terminale in stile typewriter — così il GIF vive nel repo, si
rigenera a comando e non dipende da un cast asciinema esterno.

Uso:  python3 docs/ck_demo_gif.py [output.gif]
Requisiti: Pillow. Font: Menlo (macOS) con fallback a un mono di sistema.
"""
from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFont

# --- palette (GitHub dark) --------------------------------------------------
BG = (13, 17, 23)
BAR = (22, 27, 34)
FG = (201, 209, 217)
DIM = (139, 148, 158)
BLUE = (88, 166, 255)
GREEN = (63, 185, 80)
RED = (248, 81, 73)
YELLOW = (210, 153, 34)
CYAN = (57, 197, 207)

W, H = 760, 520
PAD = 22
LINE_H = 26
FONT_SIZE = 17
CURSOR = "█"

MONO_CANDIDATES = [
    ("/System/Library/Fonts/Menlo.ttc", 0),
    ("/System/Library/Fonts/Supplemental/Andale Mono.ttf", 0),
    ("/Library/Fonts/Andale Mono.ttf", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 0),
]


def _load_font():
    for path, idx in MONO_CANDIDATES:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, FONT_SIZE, index=idx)
            except Exception:                      # noqa: BLE001
                continue
    return ImageFont.load_default()


FONT = _load_font()


def _seg_color(line: str):
    """Colora il token di stato iniziale; il resto FG. Ritorna [(txt,color)]."""
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]
    for token, col in (("[ok]", GREEN), ("[ko]", RED), ("[warn]", YELLOW),
                       ("[--]", DIM)):
        if stripped.startswith(token):
            return [(indent + token, col), (stripped[len(token):], FG)]
    if stripped.startswith("VERDICT"):
        return [(line, GREEN)]
    if stripped.startswith("→"):             # freccia routing
        return [(line, CYAN)]
    if line.startswith(">"):
        return [(">", DIM), (line[1:], BLUE)]
    if line.startswith("$"):
        return [("$", GREEN), (line[1:], FG)]
    if "context-kernel — doctor" in line or line.startswith("="):
        return [(line, DIM)]
    return [(line, FG)]


def _frame(lines, cursor=True):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # barra finestra + tre pallini
    d.rectangle([0, 0, W, 34], fill=BAR)
    for i, c in enumerate((RED, YELLOW, GREEN)):
        d.ellipse([PAD + i * 22 - 6, 11, PAD + i * 22 + 6, 23], fill=c)
    d.text((W // 2 - 70, 9), "context-kernel", font=FONT, fill=DIM)
    y = 48
    for i, line in enumerate(lines):
        x = PAD
        last = i == len(lines) - 1
        for txt, col in _seg_color(line):
            d.text((x, y), txt, font=FONT, fill=col)
            x += int(d.textlength(txt, font=FONT))
        if last and cursor:
            d.text((x, y), CURSOR, font=FONT, fill=FG)
        y += LINE_H
    return img


# --- copione (screen states + durata ms) ------------------------------------
DOOR1 = [
    "context-kernel — doctor",
    "========================================",
    "  [ok]   Python 3.10.7",
    "  [ok]   hooks registered (hooks.json)",
    "  [ok]   core scripts present (6)",
    "  [ok]   /ck-* commands present (8)",
    "  [ok]   natural language (kernel-ops)",
    "  [ok]   canary green",
    "  [ok]   A/B: queue empty",
    "  VERDICT: all clear ✓",
]
DOOR2 = [
    "  → kernel-ops: savings + canary + A/B",
    "  saved 1,214,963 tokens (−61%)",
    "  1358 compressions · canary green",
    "  A/B queue empty",
]

frames: list[Image.Image] = []
durs: list[int] = []


def push(lines, ms, cursor=True):
    frames.append(_frame(lines, cursor))
    durs.append(ms)


def type_line(base, prompt, hold_after=700):
    """Digita `prompt` char per char sopra `base`."""
    for k in range(len(prompt) + 1):
        push(base + [prompt[:k]], 45)
    push(base + [prompt], hold_after)


# Scena 1 — porta digitata
b = ["$ claude"]
push(b, 500)
type_line(b, "> /ck-doctor", hold_after=350)
shown = b + ["> /ck-doctor"]
for ln in DOOR1:
    shown = shown + [ln]
    push(shown, 200, cursor=False)
push(shown, 1500, cursor=False)

# Scena 2 — porta a parole (schermo pulito)
b2 = []
push(b2, 300)
type_line(b2, "> come va il context-kernel?", hold_after=450)
shown2 = ["> come va il context-kernel?"]
for ln in DOOR2:
    shown2 = shown2 + [ln]
    push(shown2, 260, cursor=False)
push(shown2, 2000, cursor=False)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "ck-tour.gif")
    # Frame PIENI (optimize=False) + disposal=2: ogni frame rimpiazza tutto lo
    # schermo — niente bleed fra le due scene, resa identica in ogni player.
    frames[0].save(
        out, save_all=True, append_images=frames[1:], duration=durs,
        loop=0, optimize=False, disposal=2)
    kb = os.path.getsize(out) / 1024
    print(f"scritto {out}  ({len(frames)} frame, {kb:.0f} KB)")


if __name__ == "__main__":
    main()
