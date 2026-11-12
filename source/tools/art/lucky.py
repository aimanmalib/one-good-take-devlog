"""Lucky, the cardboard stunt actor (PRD 4.4, 4.6): an original character in a leather flying helmet, goggles, scarf and big boots.

Each body part is its own sprite with a pivot (final pixels) so the runtime can hang them on a plane hierarchy. Lucky faces right.
Parts stay ivory and sepia; the runtime multiplies the jacket by the take tone, so the take number on the chest carries the meaning
for colour-blind players.
"""
from __future__ import annotations

import math

from common import col, cut, size_of, stack


def head():
    s = size_of("lucky_head")
    ink, leather = col("ink"), col("leather")

    def face(p):
        p.ellipse((62, 72, 198, 222), fill=255)
        p.ellipse((186, 128, 216, 160), fill=255)  # nose

    def face_ink(p):
        p.ellipse((158, 124, 172, 142), fill=ink)  # eye
        p.ellipse((161, 127, 165, 131), fill=col("ivory"))
        p.arc((146, 106, 182, 126), 200, 330, fill=ink, width=4)  # brow
        p.arc((132, 150, 192, 196), 20, 110, fill=ink, width=5)  # grin
        p.line([(184, 166), (192, 160)], fill=ink, width=3)
        p.ellipse((150, 160, 172, 176), fill=col("sepia", 70))  # cheek

    def cap(p):
        p.d.chord(p._b((52, 38, 208, 196)), 180, 360, fill=255)
        p.rect((64, 110, 122, 200), fill=255, r=24)  # ear flap
        p.poly([(52, 118), (208, 118), (200, 132), (64, 132)], fill=255)

    def cap_ink(p):
        p.arc((70, 52, 190, 190), 200, 330, fill=col("edge", 160), width=3)  # seam
        for i in range(9):
            a = math.radians(200 + i * 14)
            x, y = 130 + 64 * math.cos(a), 121 + 70 * math.sin(a)
            p.ellipse((x - 2, y - 2, x + 2, y + 2), fill=col("ivory", 150))  # stitches
        p.ellipse((84, 150, 104, 170), outline=col("ivory", 190), width=3)  # flap buckle
        p.line([(96, 196), (150, 214)], fill=col("edge"), width=5)  # chin strap

    def goggles(p):
        p.rect((70, 84, 214, 104), fill=255, r=10)  # band
        p.ellipse((122, 62, 168, 108), fill=255)
        p.ellipse((166, 62, 212, 108), fill=255)

    def goggle_ink(p):
        for x0 in (122, 166):
            p.ellipse((x0 + 7, 69, x0 + 39, 101), fill=col("ivory_shade"), outline=ink, width=4)
            p.arc((x0 + 12, 74, x0 + 32, 94), 200, 260, fill=col("ivory", 230), width=4)  # glint

    return stack(
        cut(s, "head.face", face, "ivory", face_ink),
        cut(s, "head.cap", cap, "leather", cap_ink),
        cut(s, "head.goggles", goggles, "sepia", goggle_ink),
    ), {"pivot": [130, 214], "anchors": {"goggles": [190, 84]}}


def torso(number: str | None):
    s = size_of("lucky_torso")
    ink = col("ink")

    def body(p):
        p.poly([(30, 44), (60, 22), (132, 22), (162, 44), (150, 236), (42, 236)], fill=255)
        p.ellipse((22, 30, 70, 78), fill=255)  # shoulders
        p.ellipse((122, 30, 170, 78), fill=255)

    def body_ink(p):
        p.poly([(70, 22), (96, 52), (122, 22)], fill=col("ivory_shade"), outline=ink, width=3)  # collar
        p.line([(96, 52), (78, 196)], fill=col("edge", 140), width=3)  # placket
        for x, y in ((92, 72), (80, 176)):
            p.ellipse((x - 7, y - 7, x + 7, y + 7), fill=ink)
        p.rect((40, 198, 152, 222), fill=col("leather"), r=4)  # belt
        p.rect((84, 196, 108, 224), outline=col("ivory_shade"), width=4, r=3)

    def badge(p):
        p.ellipse((80, 82, 156, 158), fill=255)

    def badge_ink(p):
        p.ellipse((85, 87, 151, 153), outline=col("ink"), width=4)
        if number:
            p.text((118, 124), number, 50, fill=col("ink"))
        else:
            p.text((118, 122), "?", 42, fill=col("sepia"))

    return stack(cut(s, "torso.body", body, "ivory", body_ink),
                 cut(s, f"torso.badge{number}", badge, "ivory", badge_ink)), {
        "pivot": [96, 228], "tint": "take", "anchors": {"neck": [96, 26], "shoulder": [64, 50], "badge": [118, 120]}}


def scarf():
    s = size_of("lucky_scarf")

    def shape(p):
        top = [(236 - i * 11, 30 - i * 0.3 + 10 * math.sin(i * 0.7)) for i in range(20)]
        bot = [(236 - i * 11, 84 - i * 0.9 + 10 * math.sin(i * 0.7 + 0.6)) for i in range(20)]
        p.poly(top + [(8, 40), (2, 100), (18, 74)] + bot[::-1], fill=255)
        p.ellipse((208, 22, 254, 84), fill=255)  # knot

    def stripes(p):
        for x in (196, 150, 104, 58):
            p.poly([(x, 10), (x + 16, 10), (x + 12, 110), (x - 4, 110)], fill=col("sepia", 170))
        p.arc((216, 32, 248, 74), 60, 300, fill=col("ink"), width=3)
        for i in range(5):
            p.line([(30 - i * 4, 54 + i * 6), (16 - i * 4, 60 + i * 6)], fill=col("edge", 170), width=2)  # fringe

    return cut(s, "scarf", shape, "ivory", stripes), {"pivot": [232, 54]}


def arm_upper():
    s = size_of("lucky_arm_upper")

    def shape(p):
        p.rect((12, 4, 52, 124), fill=255, r=20)

    def seam(p):
        p.line([(32, 20), (32, 106)], fill=col("edge", 90), width=2)

    return cut(s, "arm_upper", shape, "ivory", seam), {"pivot": [32, 22], "tint": "take", "anchors": {"elbow": [32, 106]}}


def arm_lower():
    s = size_of("lucky_arm_lower")

    def sleeve(p):
        p.rect((20, 4, 60, 104), fill=255, r=18)

    def cuff(p):
        p.line([(22, 96), (58, 96)], fill=col("edge", 140), width=3)

    def glove(p):
        p.ellipse((12, 92, 70, 154), fill=255)
        p.ellipse((52, 104, 76, 132), fill=255)  # thumb

    def glove_ink(p):
        for y in (114, 126, 138):
            p.arc((18, y - 14, 50, y + 6), 20, 90, fill=col("ivory", 120), width=3)

    return stack(cut(s, "arm_lower.sleeve", sleeve, "ivory", cuff),
                 cut(s, "arm_lower.glove", glove, "leather", glove_ink)), {
        "pivot": [40, 22], "tint": "take", "anchors": {"hand": [42, 124]}}


def leg_upper():
    s = size_of("lucky_leg_upper")

    def shape(p):  # jodhpur thigh: flares out at the hip, narrows to the knee
        p.poly([(10, 14), (70, 14), (74, 60), (58, 136), (22, 136), (6, 60)], fill=255)
        p.ellipse((8, 2, 72, 40), fill=255)

    def crease(p):
        p.arc((24, 30, 64, 120), 250, 290, fill=col("edge", 110), width=3)

    return cut(s, "leg_upper", shape, "sepia", crease), {"pivot": [40, 20], "anchors": {"knee": [40, 126]}}


def leg_lower():
    s = size_of("lucky_leg_lower")

    def shaft(p):
        p.rect((24, 4, 74, 132), fill=255, r=14)
        p.poly([(18, 112), (86, 104), (118, 128), (122, 160), (18, 160)], fill=255)  # big boot, toe to the right
        p.ellipse((84, 112, 126, 162), fill=255)

    def boot_ink(p):
        p.rect((20, 152, 124, 164), fill=col("edge"), r=4)  # sole
        for y in (30, 52, 74, 96):
            p.line([(40, y), (58, y + 8)], fill=col("ivory", 160), width=3)  # laces
            p.line([(58, y), (40, y + 8)], fill=col("ivory", 160), width=3)
        p.arc((88, 118, 118, 150), 210, 300, fill=col("ivory", 120), width=4)  # toe shine

    return cut(s, "leg_lower", shaft, "leather", boot_ink), {"pivot": [48, 20]}


def sprites() -> dict:
    out = {
        "lucky_head": head(),
        "lucky_torso": torso(None),
        "lucky_scarf": scarf(),
        "lucky_arm_upper": arm_upper(),
        "lucky_arm_lower": arm_lower(),
        "lucky_leg_upper": leg_upper(),
        "lucky_leg_lower": leg_lower(),
    }
    for n in ("1", "2", "3", "4"):
        out[f"lucky_torso_{n}"] = torso(n)
    return {name: (img, "lucky_torso" if name.startswith("lucky_torso") else name, meta)
            for name, (img, meta) in out.items()}
