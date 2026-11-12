"""Level checks a to h (PRD 4.5), shared by `check.py` and the generator. Limits come from `Content/Data/rules.json`
("checks"). Every problem string starts with its check letter, e.g. "(c) 6 winning films ...".

(a) no take wins alone, except the scenes listed in `solo_win_allowed` (scene 1)
(b) the fewest cuts that win equals the stored `par`
(c) the number of winning films within the budget is in `wins_in_budget`
(d) from reel `dead_end_from_reel` on: at least one legal cut leads from a winnable film into a dead end
(e) every new hazard kind appears in a scene with no other new element (hazard kind, crates, prop), in reel order
(f) every take moves plausibly (motion rules in model.py) and the scene is structurally valid
(g) the scene lasts at most `max_scene_seconds`
(h) the stored solution wins and the solver confirms it (same cut count as the solver's par)
"""
from __future__ import annotations

import re

from model import Scene, load_rules, scene_errors
from solver import count_wins, dead_end_cuts, solo_winners, solve, verify_solution

ID_RE = re.compile(r"^r(\d+)s(\d+)$")


def scene_problems(scene: Scene, rules: dict | None = None) -> list[str]:
    rules = rules or load_rules()
    lim = rules["checks"]
    errs = [f"(f) {e}" for e in scene_errors(scene, rules)]
    if errs:
        return errs
    m = ID_RE.match(scene.id)
    if not m or int(m.group(1)) != scene.reel:
        errs.append(f"(e) id {scene.id!r} must be r<reel>s<number> with reel {scene.reel}")
    seconds = scene.ticks * rules["tick_seconds"]
    if seconds > lim["max_scene_seconds"]:
        errs.append(f"(g) scene lasts {seconds:g} s (max {lim['max_scene_seconds']})")
    film = solve(scene)
    if film is None:
        return errs + ["(b) no winning film within the budget"]
    if scene.par != len(film.cuts):
        errs.append(f"(b) stored par {scene.par}, fewest cuts that win is {len(film.cuts)}")
    solos = solo_winners(scene)
    if solos and scene.id not in lim["solo_win_allowed"]:
        errs.append(f"(a) take(s) {solos} win with no cut")
    wins = lim["wins_in_budget"]
    n = count_wins(scene, limit=wins["max"] + 1)
    if not wins["min"] <= n <= wins["max"]:
        shown = f"more than {wins['max']}" if n > wins["max"] else str(n)
        errs.append(f"(c) {shown} winning films within budget {scene.budget} ({wins['min']}..{wins['max']})")
    if scene.reel >= lim["dead_end_from_reel"] and not dead_end_cuts(scene):
        errs.append("(d) no legal cut leads into a dead end")
    errs += [f"(h) {e}" for e in verify_solution(scene)]
    return errs


def elements(scene: Scene) -> set[str]:
    """What a player may meet for the first time in a scene (check e)."""
    out = {hz.kind for hz in scene.hazards}
    if scene.crates:
        out.add("crate")
    if scene.prop is not None:
        out.add("prop")
    return out


def scene_order(scene: Scene) -> tuple[int, int, str]:
    m = ID_RE.match(scene.id)
    return (scene.reel, int(m.group(2)) if m else 0, scene.id)


def reel_problems(scenes: list[Scene], rules: dict | None = None) -> list[str]:
    """Checks across scenes: unique ids and check e in play order (reel, scene number)."""
    rules = rules or load_rules()
    kinds = set(rules["hazard_kinds"])
    errs: list[str] = []
    ids = [s.id for s in scenes]
    errs += [f"(e) duplicate scene id {i!r}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    seen: set[str] = set()
    for s in sorted(scenes, key=scene_order):
        new = elements(s) - seen
        if new & kinds and len(new) > 1:
            errs.append(f"(e) {s.id}: new hazard kind shown together with other new elements {sorted(new)}")
        seen |= new
    return errs
