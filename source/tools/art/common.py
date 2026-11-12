"""Shared painting kit for the cardboard look (PRD 4.6): style values, a scaled pen, cut-out pieces with wavy dark edges.

Everything is deterministic: every random field comes from numpy's PCG64 seeded with the style seed plus a sprite name hash.
Painting happens at `supersample` times the final size and `finish()` scales down once, which is the only anti-aliasing step.
"""
from __future__ import annotations

import json
import math
import zlib
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

REPO = Path(__file__).resolve().parents[2]
STYLE_FILE = Path(__file__).resolve().parent / "style.json"


@lru_cache(maxsize=1)
def style() -> dict:
    return json.loads(STYLE_FILE.read_text(encoding="utf-8"))


def col(name: str, alpha: int = 255) -> tuple[int, int, int, int]:
    """Palette colour by name (or a take tone by its number as text)."""
    pal = style()["palette"]
    rgb = pal[name] if name in pal else style()["take_tones"][name]["rgb"]
    return (*rgb, alpha)


def rng(name: str, salt: int = 0) -> np.random.Generator:
    return np.random.default_rng([style()["seed"], zlib.crc32(name.encode()), salt])


def size_of(key: str) -> tuple[int, int]:
    w, h = style()["sizes"][key]
    return int(w), int(h)


@lru_cache(maxsize=32)
def font(px: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(REPO / style()["font"]["file"]), px)


class Pen:
    """ImageDraw wrapper in final-pixel units; every coordinate is multiplied by the supersample factor."""

    def __init__(self, img: Image.Image):
        self.img = img
        self.k = style()["supersample"]
        self.d = ImageDraw.Draw(img)

    def _p(self, pts):
        return [(x * self.k, y * self.k) for x, y in pts]

    def _b(self, box):
        return [v * self.k for v in box]

    def ellipse(self, box, fill=None, outline=None, width=1):
        self.d.ellipse(self._b(box), fill=fill, outline=outline, width=round(width * self.k))

    def rect(self, box, fill=None, outline=None, width=1, r=0):
        self.d.rounded_rectangle(self._b(box), radius=r * self.k, fill=fill, outline=outline,
                                 width=round(width * self.k))

    def poly(self, pts, fill=None, outline=None, width=1):
        self.d.polygon(self._p(pts), fill=fill, outline=outline, width=round(width * self.k))

    def line(self, pts, fill, width=1):
        self.d.line(self._p(pts), fill=fill, width=round(width * self.k), joint="curve")
        r = width * self.k / 2
        for x, y in (self._p(pts)[0], self._p(pts)[-1]):
            self.d.ellipse([x - r, y - r, x + r, y + r], fill=fill)

    def arc(self, box, start, end, fill, width=1):
        self.d.arc(self._b(box), start, end, fill=fill, width=round(width * self.k))

    def text(self, xy, s, px, fill, anchor="mm", spacing=0):
        f = font(round(px * self.k))
        if spacing:
            self._spaced(xy, s, f, fill, anchor, spacing)
        else:
            self.d.text((xy[0] * self.k, xy[1] * self.k), s, font=f, fill=fill, anchor=anchor)

    def _spaced(self, xy, s, f, fill, anchor, spacing):
        gap = spacing * self.k
        widths = [f.getlength(c) for c in s]
        total = sum(widths) + gap * (len(s) - 1)
        x = xy[0] * self.k - total / 2
        for c, w in zip(s, widths):
            self.d.text((x, xy[1] * self.k), c, font=f, fill=fill, anchor="l" + anchor[1])
            x += w + gap


def canvas(size: tuple[int, int], fill=(0, 0, 0, 0)) -> Image.Image:
    k = style()["supersample"]
    mode = "L" if isinstance(fill, int) else "RGBA"
    return Image.new(mode, (size[0] * k, size[1] * k), fill)


def smooth_noise(gen: np.random.Generator, shape: tuple[int, int], cells: int) -> np.ndarray:
    """Value noise in [-1, 1]: a coarse random grid scaled up bicubically (tileable enough for our sprites)."""
    h, w = shape
    cw = max(2, cells)
    ch = max(2, round(cells * h / max(w, 1)))
    grid = gen.uniform(-1, 1, (ch, cw)).astype(np.float32)
    img = Image.fromarray(((grid + 1) * 127.5).astype(np.uint8), "L").resize((w, h), Image.BICUBIC)
    return np.asarray(img, dtype=np.float32) / 127.5 - 1


def wobble(mask: Image.Image, gen: np.random.Generator) -> Image.Image:
    """Displace a mask by a smooth field so straight cuts become hand-cut wavy edges."""
    cb = style()["cardboard"]
    k = style()["supersample"]
    a = np.asarray(mask)
    h, w = a.shape
    cells = max(3, round(cb["wobble_cells"] * max(w, h) / (256 * k)))
    amp = cb["wobble_px"] * k
    dx = smooth_noise(gen, (h, w), cells) * amp
    dy = smooth_noise(gen, (h, w), cells) * amp
    yy, xx = np.mgrid[0:h, 0:w]
    sx = np.clip(np.rint(xx + dx), 0, w - 1).astype(np.intp)
    sy = np.clip(np.rint(yy + dy), 0, h - 1).astype(np.intp)
    return Image.fromarray(a[sy, sx], "L")


def board(size_px: tuple[int, int], base, gen: np.random.Generator) -> np.ndarray:
    """Cardboard surface (float RGB): base colour, soft blotches, fine vertical fibres."""
    cb = style()["cardboard"]
    w, h = size_px
    blot = smooth_noise(gen, (h, w), cb["blotch_cells"]) * cb["blotch_amp"]
    fibre = gen.normal(0, 1, (1, w)).astype(np.float32) * cb["fibre_amp"] * 0.6
    speck = gen.normal(0, 1, (h, w)).astype(np.float32) * cb["fibre_amp"] * 0.4
    shade = (blot + fibre + speck)[..., None]
    return np.clip(np.asarray(base[:3], dtype=np.float32)[None, None, :] + shade, 0, 255)


def cut(size: tuple[int, int], name: str, shape, fill="ivory", details=None, edge=None) -> Image.Image:
    """One cardboard piece at supersample scale.

    `shape(pen)` paints the silhouette (white) on an L mask; `details(pen)` paints ink and paint on top, clipped to the piece.
    The piece gets a wavy dark rim and a little rim shading so it reads as card with thickness.
    """
    cb = style()["cardboard"]
    k = style()["supersample"]
    gen = rng(name)
    full = canvas(size, 0)
    shape(Pen(full))
    edge_w = (cb["edge_px"] if edge is None else edge) * k
    box = _grown_bbox(full, edge_w + 2 * cb["wobble_px"] * k + 6 * k)
    out_full = canvas(size)
    if box is None:
        return out_full
    mask = wobble(full.crop(box), gen)
    outer = _dilate(mask, edge_w)
    surf = board(mask.size, col(fill) if isinstance(fill, str) else fill, gen)
    rim = np.asarray(mask.filter(ImageFilter.GaussianBlur(3 * k)), dtype=np.float32) / 255
    surf *= (1 - cb["rim_shade"] * (1 - rim))[..., None]
    body = Image.fromarray(surf.astype(np.uint8), "RGB").convert("RGBA")
    body.putalpha(mask)
    out = Image.new("RGBA", mask.size, col("edge"))
    out.putalpha(outer)
    out.alpha_composite(body)
    if details:
        layer = canvas(size)
        details(Pen(layer))
        layer = layer.crop(box)
        la = np.asarray(layer.getchannel("A"), dtype=np.float32) * (np.asarray(mask, dtype=np.float32) / 255)
        layer.putalpha(Image.fromarray(la.astype(np.uint8), "L"))
        out.alpha_composite(layer)
    out_full.paste(out, box[:2])
    return out_full


def _grown_bbox(mask: Image.Image, pad: float):
    """Bounding box of the painted silhouette plus room for rim, wobble and blur; None when nothing was painted."""
    bb = mask.getbbox()
    if bb is None:
        return None
    p = int(math.ceil(pad))
    return (max(0, bb[0] - p), max(0, bb[1] - p), min(mask.width, bb[2] + p), min(mask.height, bb[3] + p))


def _dilate(mask: Image.Image, px: float) -> Image.Image:
    """Grow a mask by about `px` with a soft outer fade (box blur + gain): much faster than a rank filter, same look."""
    if px <= 0:
        return mask
    r = max(1, round(px * 1.3))
    return mask.filter(ImageFilter.BoxBlur(r)).point(lambda v: min(255, v * 8))


def stack(*layers: Image.Image) -> Image.Image:
    out = layers[0].copy()
    for layer in layers[1:]:
        out.alpha_composite(layer)
    return out


def ink(size: tuple[int, int], draw) -> Image.Image:
    """Plain ink layer (no card): pictograms, chalk marks, overlays."""
    img = canvas(size)
    draw(Pen(img))
    return img


def finish(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    out = img.resize(size, Image.LANCZOS)
    a = np.asarray(out.getchannel("A"))
    if a.min() == 255:
        return out
    # fully transparent pixels carry the rim colour, so engine filtering at the cut edge never bleeds a stray colour
    arr = np.asarray(out).copy()
    arr[a == 0] = col("edge", 0)
    return Image.fromarray(arr, "RGBA")
